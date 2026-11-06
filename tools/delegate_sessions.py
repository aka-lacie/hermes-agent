"""Delegate children over the unified session runner (plan §4.5): spawn, resume and steer are admissions.

- **spawn** (a retained child): the child's sessions row is stamped as a delegate session
  (``tools.delegate_session_store.DelegateSessionStore.register``) with its credential-free route, and the
  assignment is admitted on the child's FIFO and claimed for the run the tool call starts.
- **resume** admits ``queue`` on the child. Idle: it is claimed and run now (synchronously or as a background
  unit, exactly like a spawn). Running: the entry waits in the FIFO; when the running claim settles, the
  drain claims it and runs it, and its result is delivered through the child's ``delivery_target``.
- **steer** admits ``steer``. Running in this process: the row is handed to the running claim (the atomic
  ``steering`` transfer) and the live agent receives it at its next tool boundary; a steer the closing turn
  can no longer take goes back to its FIFO place and runs as the next turn (lossless handoff). Idle: it is a
  normal queued turn.

Resume never re-enters ``delegate_task``: the run is built by ``tools.delegate_tool.launch_children`` with
the route re-resolved from ``sessions.route`` (``agent.session_routes.resolve_route``), so the fallback
chain and the route's reasoning level survive.

**Peer-only wakes are silent** (plan §4.4). A turn whose claimed admissions are all ``sender_kind="peer"``
events (a sibling's message through ``ctx.sessions.admit``; no parent assignment, steer or other event in
the claim) runs detached and emits NO completion: nothing is dispatched to ``delivery_target`` and no async
completion is queued. The child tells its parent only if it chooses to. A claim mixing peer events with a
parent assignment (held peer notices riding a parent turn) completes to the parent as usual. A parent steer
arriving while a silent peer turn runs is not handed into it: it runs as the child's next turn, which
completes normally, so a turn is always either peer-only (silent) or the parent's.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import threading
import time
import weakref
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from tools.registry import tool_error

logger = logging.getLogger(__name__)

# admission id -> (weak parent agent, dispatch context) for queued entries a later drain runs: the context of
# the tool call that admitted them carries the originating session's routing (gateway key, UI session).
_pending_launch: dict[str, tuple[Any, contextvars.Context]] = {}
_pending_lock = threading.Lock()
_draining: set[tuple[str, str]] = set()
# A wake that arrived while that session's drain was already running: the drain re-runs once it exits, so
# an admission landing between its last head check and its exit is never stranded (a lost wakeup).
_redrain: dict[tuple[str, str], Any] = {}
# Live parent agents of retained children in this process, by the parent's session id: who can build and run
# a child woken by a peer admission (the drain needs the parent's tools, identity and routing).
_live_parents: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()


def _real_store(db) -> bool:
    """The profile's own SessionDB (not a worker's scoped RPC store or a test double)."""
    from hermes_state import SessionDB
    return isinstance(getattr(db, "_db", db), SessionDB)


def _store(parent):
    from tools.delegate_session_store import DelegateSessionStore
    db = getattr(parent, "_session_db", None)
    if db is None:
        raise ValueError("Resumable delegation requires a persisted parent session.")
    return DelegateSessionStore(getattr(db, "_db", db))


def _weak(agent):
    try:
        return weakref.ref(agent)
    except TypeError:
        return lambda: agent


def remember_launch(admission_id: str, parent) -> None:
    with _pending_lock:
        _pending_launch[admission_id] = (_weak(parent), contextvars.copy_context())


def _launch_for(admission_id: str):
    with _pending_lock:
        found = _pending_launch.pop(admission_id, None)
    if found is None:
        return None, None
    return found[0](), found[1]


def note_parent(parent) -> None:
    """Remember *parent* as the live owner of its retained children (weakly)."""
    sid = getattr(parent, "session_id", None)
    if not sid:
        return
    try:
        with _pending_lock:
            _live_parents[str(sid)] = parent
    except TypeError:  # a non-weakref-able test double
        pass


def wake_delegate(db, session_id: str) -> bool:
    """Schedule the drain of retained child *session_id* (just admitted a peer message) when its parent agent
    lives in this process. Otherwise the entry waits in the FIFO for the parent's next resume (never run
    without the parent's tools and identity)."""
    store_db = getattr(db, "_db", db)
    row = store_db._read_one("SELECT parent_session_id FROM sessions WHERE id = ?", (session_id,))
    if row is None or not row["parent_session_id"]:
        return False
    from agent import session_directory as directory
    with store_db._read_ctx() as conn:
        parents = directory.lineage(store_db, conn, row["parent_session_id"])
    with _pending_lock:
        parent = next((_live_parents.get(pid) for pid in reversed(parents) if _live_parents.get(pid) is not None), None)
    if parent is None:
        logger.info("delegate %s: peer message queued; no live parent in this process to wake it", session_id)
        return False
    schedule_drain(Path(store_db.db_path).resolve(), session_id, parent)
    return True


def claim_sender_kinds(claim: Optional[dict]) -> list[str]:
    """The ``sender_kind`` of every input a claim carries (its head and the held notices handed to it); a
    plain assignment (no typed event) counts as its payload's sender (``parent``)."""
    from gateway.delivery_targets import event_events, held_events
    if not claim:
        return []
    payload = claim.get("payload") or {}
    events = event_events(payload)
    kinds = [str(e.get("sender_kind") or "") for e in events] if events else [str(payload.get("sender_kind") or "parent")]
    for row in claim.get("held") or []:
        kinds.extend(str(e.get("sender_kind") or "") for e in held_events(row.get("payload") or {}))
    return kinds


def is_peer_only(claim: Optional[dict]) -> bool:
    """True when everything *claim* carries is a peer message (the runner's silence rule, from structure only)."""
    kinds = claim_sender_kinds(claim)
    return bool(kinds) and all(kind == "peer" for kind in kinds)


# ── spawn / teardown hooks called by tools.delegate_tool ─────────────────────

def bind_child(parent, child, task, resume=None, *, extension, options, routing_cfg, retain=False, task_index=0):
    """Reserve a persisted conversation before dispatch, including queued children."""
    if not (resume is not None or retain or options.get("retain", False)):
        db = getattr(child, "_session_db", None)
        if _real_store(db) and isinstance(getattr(db, "db_path", None), (str, Path)):
            # Still an ordinary sessions row: stamped closed (never resumable) once it ends.
            child._delegate_unretained = True
            child._delegate_store_path = str(Path(db.db_path).resolve())
        return
    if getattr(child, "_delegate_profile_home", None) is not None:
        raise ValueError("Retained delegation cannot yet resume a profile route; choose a model/provider route.")
    note_parent(parent)
    child._delegate_review_skills = options.get("review_skills", False)
    child._delegate_assignment_formatter = extension.format_assignment if extension is not None else None
    from tools.delegate_session_store import DelegateSessionStore
    if resume is None:
        child._ensure_db_session()
    else:
        child._session_db_created = True
    db = child._session_db
    if not _real_store(db):
        raise ValueError("Retained delegation requires the profile session store.")
    store = DelegateSessionStore(db)
    if resume is None:
        from agent.session_routes import capture_agent_route
        store.register(
            parent.session_id, child.session_id,
            model=child.model, provider=getattr(child, "requested_provider", None) or child.provider,
            assignment=task["goal"], context=task.get("context") or "", title=options.get("title", task.get("title")),
            role=child._delegate_role, depth=child._delegate_depth,
            workspace=child._delegate_workspace, forked_context=child._delegate_forked_context,
            route=capture_agent_route(child, routing_cfg=routing_cfg, report=getattr(child, "_delegate_route", None)),
            group_id=getattr(child, "_delegation_id", None),
        )
        deleg = getattr(child, "_delegation_id", None)
        record, token = store.acquire(parent.session_id, child.session_id, task["goal"],
                                      request_id=f"spawn:{deleg}:{task_index}" if deleg else None)
        claim = None
    else:
        record, token, claim = resume["record"], resume["token"], resume.get("claim")
        db.reopen_session(child.session_id)
        resume["transferred"] = True
    if claim is not None:
        _write_held_events(db, child.session_id, claim)
        events = _claimed_events(claim)
        if events:
            from gateway.delivery_targets import event_typed_input
            child._delegate_typed_input = event_typed_input(events)
    if resume is not None:
        child._delegate_resume_history = db.get_resume_conversations(child.session_id)[0]
    child._delegate_resumable = True
    from agent.prompt_builder import clear_skills_system_prompt_cache
    clear_skills_system_prompt_cache(clear_snapshot=True)
    child._delegate_session_id = record["session_id"]
    child._approval_title = record["title"]
    child._delegate_resumed = resume is not None
    child._delegate_session_token = token
    child._delegate_steer_transfers = {}
    child._delegate_store_path = str(Path(db.db_path).resolve())
    child._delegate_assignment_at = record["last_activity_at"]
    progress = child.tool_progress_callback
    last_touch = time.monotonic()

    def touch_progress(*args, **kwargs):
        nonlocal last_touch
        now = time.monotonic()
        if now - last_touch >= 1:
            store.touch(record["session_id"], token)
            last_touch = now
        if progress:
            return progress(*args, **kwargs)

    if hasattr(progress, "_flush"):
        touch_progress._flush = progress._flush
    child.tool_progress_callback = touch_progress


def _claimed_events(claim: dict) -> list[dict]:
    from gateway.delivery_targets import event_events
    return event_events(claim.get("payload") or {})


def _write_held_events(db, session_id: str, claim: dict) -> None:
    from gateway.delivery_targets import write_held_events
    write_held_events(db, session_id, claim)


def note_steer_accepted(child, admission_id: str) -> None:
    """A steer transfer the live agent accepted; one still pending when the turn closes was never delivered."""
    transfers = getattr(child, "_delegate_steer_transfers", None)
    if transfers is None:
        return
    if getattr(child, "_pending_steer", None) is None:
        # Everything accepted before was already handed to a tool boundary.
        for key in transfers:
            transfers[key] = True
    transfers[admission_id] = None  # pending until a boundary takes it


def _turn_outcome(child) -> tuple[str, dict]:
    entry = getattr(child, "_delegate_turn_entry", None) or {}
    status = str(entry.get("status") or "failed")
    outcome = "completed" if status == "completed" else "interrupted" if status == "interrupted" else "failed"
    transfers = dict(getattr(child, "_delegate_steer_transfers", None) or {})
    missed = bool(entry.get("missed_steer"))
    resolved = {key: (False if missed and value is None else True) if value is None else value
                for key, value in transfers.items()}
    return outcome, resolved


def release_child(child):
    """Settle the child's claim only after the worker and resource cleanup finish; fence old callbacks. Then the
    FIFO moves on: a resume or steer queued while it ran is drained next."""
    if getattr(child, "_delegate_unretained", False):
        _stamp_unretained(child)
        return
    token = vars(child).pop("_delegate_session_token", None)
    if not token:
        return
    from hermes_state_registry import acquire, release
    from tools.delegate_session_store import DelegateSessionStore
    db = acquire(Path(child._delegate_store_path))
    try:
        outcome, transfers = _turn_outcome(child)
        DelegateSessionStore(db).release(child._delegate_session_id, token, current_session_id=child.session_id,
                                         outcome=outcome, transfers=transfers or None)
    finally:
        release(db)
    if not getattr(child, "_delegate_drain_owned", False):
        parent_ref = getattr(child, "_delegate_parent_ref", None)
        schedule_drain(child._delegate_store_path, child._delegate_session_id,
                       parent_ref() if callable(parent_ref) else None)


def _stamp_unretained(child) -> None:
    """A non-retained child is still an ordinary sessions row: closed, never in its parent's default list
    unless running, and never resumable."""
    parent = getattr(child, "_parent_session_id", None)
    path = getattr(child, "_delegate_store_path", None)
    if not path or not getattr(child, "session_id", None) or not parent:
        return
    from hermes_state_registry import acquire, release
    from gateway.delivery_targets import delegate_target, encode_delivery_target
    db = acquire(Path(path))  # the child's own handle is already released by its close()
    try:
        db._write_rowcount(
            "UPDATE sessions SET lineage_kind = COALESCE(lineage_kind, 'delegate'), status = 'closed', "
            "role = COALESCE(role, ?), delivery_target = COALESCE(delivery_target, ?) WHERE id = ? AND "
            "parent_session_id = ?", (getattr(child, "_delegate_role", None),
                                      encode_delivery_target(delegate_target(parent)), child.session_id, parent))
    except sqlite3.Error:
        logger.warning("could not stamp finished child %s as a closed delegate session", child.session_id,
                       exc_info=True)
    finally:
        release(db)


def assignment_message(child, message):
    """Only temporal context is added; the parent owns the assignment's instructions."""
    if getattr(child, "_delegate_resumable", False) is not True:
        return message
    formatter = getattr(child, "_delegate_assignment_formatter", None)
    if formatter is None:
        return message
    return formatter(message, session_id=child._delegate_session_id,
                     timestamp=datetime.now(UTC).isoformat(timespec="seconds"))


# ── the drain ────────────────────────────────────────────────────────────────

def schedule_drain(db_path, session_id: str, parent=None) -> bool:
    """Run the child's next queued turn(s) on a daemon thread, once per session at a time.

    Ownership protocol (lost-wakeup free): ``_draining`` marks the one live drain of a session and
    ``_redrain`` a wake that arrived while it ran; both change only under ``_pending_lock``. A producer
    either starts the drain or records the wake for the owner; the owner gives ownership up only in
    :func:`_finish_pass`, under the same lock, and only when no wake is recorded. So a wake recorded
    after the owner's last head check is always seen by that decision, and the owner runs another pass."""
    key = (str(db_path), session_id)
    with _pending_lock:
        if key in _draining:
            _redrain[key] = parent if parent is not None else _redrain.get(key)
            return False
        _draining.add(key)
    try:
        from agent.memory_provider import spawn_context_thread
        thread = spawn_context_thread(_drain, args=(str(db_path), session_id, parent),
                                      name=f"delegate-drain-{session_id[:12]}")
        thread.start()
    except BaseException:
        with _pending_lock:
            _draining.discard(key)
        raise
    return True


def _finish_pass(key: tuple[str, str]) -> tuple[bool, Any]:
    """The single exit decision of a drain pass, atomic with every producer's ``key in _draining``
    check: ``(True, parent)`` keeps ownership for another pass (a wake arrived since the pass began),
    ``(False, None)`` releases it."""
    with _pending_lock:
        if key in _redrain:
            return True, _redrain.pop(key)
        _draining.discard(key)
        return False, None


def _drain(db_path: str, session_id: str, fallback_parent) -> None:
    key = (db_path, session_id)
    parent = fallback_parent
    try:
        while True:
            try:
                _drain_pass(db_path, session_id, parent)
            except Exception:
                logger.exception("delegate drain for %s failed", session_id)
            # Every exit of a pass (empty head, busy FIFO, no parent, lost claim race, error) reaches this
            # one decision; a wake recorded while the pass ran is honoured here, in this same thread.
            again, again_parent = _finish_pass(key)
            if not again:
                return
            parent = again_parent or parent or fallback_parent
    except BaseException:
        with _pending_lock:
            _draining.discard(key)
            _redrain.pop(key, None)
        raise


def _drain_pass(db_path: str, session_id: str, parent_hint) -> None:
    """One pass over the child's FIFO: run claimable heads until it is empty, busy, or unrunnable here."""
    from hermes_state_registry import acquire, release
    from tools.delegate_session_store import DelegateSessionStore
    db = acquire(Path(db_path))
    try:
        store = DelegateSessionStore(db)
        from hermes_state_runtime import list_session_admissions
        from hermes_state_wake import next_claimable
        missed = False
        for _ in range(64):  # bounded: ADMISSION_QUEUE_MAX plus margin
            head = next_claimable(list_session_admissions(db, session_id=session_id))
            if head is None or store.running_claim(session_id) is not None:
                return
            parent, ctx = _launch_for(head["admission_id"])
            parent = parent or parent_hint
            if parent is None:
                # Nobody here can build the child (its parent is gone): the entry stays queued for the next
                # explicit resume, never run without its parent's tools and identity.
                logger.info("delegate %s: queued assignment %s waits for its parent", session_id, head["admission_id"])
                return
            claim = store.claim(session_id, head=head["admission_id"])
            if not claim:
                remember_launch(head["admission_id"], parent)
                if missed:
                    return  # still unclaimable (an unknown turn pauses the FIFO): its resolution wakes it
                # Busy (another claimer won) or the head moved: re-read once; the next check decides.
                missed = True
                continue
            missed = False
            (ctx or contextvars.copy_context()).run(_run_claimed, parent, store, session_id, claim)
        logger.warning("delegate %s: drain pass bound reached with work possibly left queued", session_id)
    finally:
        release(db)


def _run_claimed(parent, store, session_id: str, claim: dict) -> None:
    """Run one claimed queued turn without a waiting tool call; deliver its result through delivery_target."""
    from tools.delegate_tool import launch_resumed
    record = store.get(parent.session_id, session_id) if _reachable(store, parent, session_id) else None
    if record is None:
        store.release(session_id, claim["token"], outcome="interrupted")
        return
    if is_peer_only(claim):
        _run_silent(parent, store, record, claim)
        return
    top_level = getattr(parent, "_delegate_depth", 0) == 0
    reservation = {"record": record, "token": claim["token"], "claim": claim, "transferred": False,
                   "drain_owned": not top_level}
    try:
        raw = launch_resumed(parent, record, claim, reservation, background=top_level)
    finally:
        if not reservation["transferred"]:
            store.release(session_id, claim["token"], outcome="failed")
    if top_level:
        return  # a background unit: its completion travels the async pipeline, stamped with delivery_target
    _deliver_results(store.db, session_id, raw)


def _run_silent(parent, store, record: dict, claim: dict, *, drain_owned: bool = True) -> None:
    """Run a peer-only claim detached and synchronously, with no completion anywhere (plan §4.4)."""
    from tools.delegate_tool import launch_resumed
    session_id = record["session_id"]
    reservation = {"record": record, "token": claim["token"], "claim": claim, "transferred": False,
                   "drain_owned": drain_owned}
    try:
        launch_resumed(parent, record, claim, reservation, background=False, detached=True)
    finally:
        if not reservation["transferred"]:
            store.release(session_id, claim["token"], outcome="failed")
    logger.info("delegate %s: peer-only turn %s finished; no parent completion (silent wake)",
                session_id, claim["admission_id"])


def _reachable(store, parent, session_id: str) -> bool:
    from tools.delegate_session_store import DelegateSessionError
    try:
        store.get(parent.session_id, session_id)
        return True
    except (DelegateSessionError, LookupError):
        return False


def _deliver_results(db, session_id: str, raw: str) -> None:
    from gateway.delivery_targets import child_completion_event, dispatch
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        result = {"error": str(raw)}
    row = db._read_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    entries = result.get("results") or [{"status": "failed", "error": result.get("error") or "no result"}]
    for entry in entries:
        entry = dict(entry, session_id=entry.get("session_id") or session_id)
        receipt = dispatch(db, row, child_completion_event(row, entry, fold_key=row["group_id"] if row else None))
        if not receipt.delivered:
            logger.warning("delegate %s: completion not delivered (%s)", session_id, receipt.reason)


# ── model-facing control ─────────────────────────────────────────────────────

def control(action, parent, *, session_id=None, message=None, model=None, query=None, limit=None, background=False,
            scope="children"):
    from agent import session_views as views
    from tools.delegate_session_store import DelegateSessionError
    try:
        store = _store(parent)
        note_parent(parent)
        if action == "list":
            records = store.list(parent.session_id, query=query, limit=limit or 20, scope=scope or "children")
            return json.dumps({"action": action, "scope": scope or "children", "count": len(records),
                               "subagents": [views.list_row(r) for r in records]}, ensure_ascii=False)
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError(f"action='{action}' requires session_id from spawn or list.")
        if action == "observe":
            # The store's observe resolves the selector, reaches, reconciles and checks the tip itself.
            return json.dumps(store.observe(parent.session_id, session_id.strip(), limit=limit), ensure_ascii=False)
        record = store.get(parent.session_id, session_id.strip())
        session_id = record["session_id"]  # a selector names the session its conversation holds now
        if action == "close":
            record = store.close(parent.session_id, session_id)
            _cancel_review(parent, session_id)
            return json.dumps({"action": action, **views.list_row(record)}, ensure_ascii=False)
        if action == "resume":
            return resume(parent, store, record, message, model, background)
        if action == "steer":
            return steer(parent, store, record, message)
        return _live_control(action, parent, record, message)
    except (ValueError, DelegateSessionError, LookupError) as exc:
        return tool_error(str(exc))


def _cancel_review(parent, session_id):
    from agent.delegated_review import cancel_delegated_review
    cancel_delegated_review(session_id, db_path=parent._session_db.db_path)


def resume(parent, store, record, message, model, background):
    """Admit ``queue`` on the child; run now when idle, else it waits its turn in the FIFO."""
    from tools.delegate_tool import is_spawn_paused, launch_resumed
    if not isinstance(message, str) or not message.strip():
        raise ValueError("action='resume' requires a non-empty message describing the next assignment.")
    if is_spawn_paused():
        raise ValueError("Delegation spawning is paused; resume starts another run and is paused too.")
    if record.get("lineage_kind") != "delegate":
        return _resume_foreign(parent, store, record, message)
    if model is not None:
        from tools.delegate_tool import resumed_route
        resumed_route(parent, record, model)  # an invalid route must not replace the saved model
    admission = store.admit(parent.session_id, record["session_id"], message, model=model)
    if model is not None:
        store.set_route_model(record["session_id"], model)  # later resumes keep the chosen model
    if admission["busy"]:
        remember_launch(admission["admission_id"], parent)
        return json.dumps({"action": "resume", "session_id": record["session_id"], "status": "queued",
                           "admission_id": admission["admission_id"], "position": admission["position"],
                           "note": "The child is running; this assignment waits in its queue and its result "
                                   "arrives as a normal completion message."}, ensure_ascii=False)
    claim = store.claim(record["session_id"])
    for _ in range(32):  # ADMISSION_QUEUE_MAX: older peer-only turns ahead of ours run first, silently
        if not claim or claim["admission_id"] == admission["admission_id"] or not is_peer_only(claim):
            break
        _run_silent(parent, store, store.get(parent.session_id, record["session_id"]), claim, drain_owned=True)
        claim = store.claim(record["session_id"])
    if not claim:
        remember_launch(admission["admission_id"], parent)
        return json.dumps({"action": "resume", "session_id": record["session_id"], "status": "queued",
                           "admission_id": admission["admission_id"], "position": admission["position"]})
    if claim["admission_id"] != admission["admission_id"]:
        # An older entry heads the FIFO: run it first (FIFO order), ours follows through the drain.
        remember_launch(admission["admission_id"], parent)
    reservation = {"record": store.get(parent.session_id, record["session_id"]), "token": claim["token"],
                   "claim": claim, "transferred": False}
    try:
        _cancel_review(parent, record["session_id"])
        return launch_resumed(parent, reservation["record"], claim, reservation, background=background)
    finally:
        if not reservation["transferred"]:
            store.release(record["session_id"], claim["token"], outcome="failed")


def _resume_foreign(parent, store, record, message, action="resume"):
    """A non-delegate session the caller may address (a webhook/cron session delivering to it): the message is
    delivered to it as the parent's typed event, through the sink of whoever drains that session (the gateway
    owner, for keyed webhook sessions). Nothing is written when no owner in this process serves it."""
    from gateway.delivery_targets import DeliveryEvent, dispatch, session_target
    event = DeliveryEvent("parent_message", message, "parent", f"session:{parent.session_id}",
                          meta={"session_id": parent.session_id})
    receipt = dispatch(store.db, None, event, target=session_target(record["session_id"], attach=False))
    if not receipt.delivered:
        raise ValueError(f"That session is not served by this process ({receipt.reason}); nothing was queued.")
    return json.dumps({"action": action, "session_id": record["session_id"], "status": "queued",
                       "admission_id": receipt.admission_id, "sink": receipt.sink}, ensure_ascii=False)


def _live_record(parent, session_id):
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
    profile_db = str(Path(parent._session_db.db_path).resolve())
    with _active_subagents_lock:
        return next(((sid, live) for sid, live in _active_subagents.items()
                     if getattr(live.get("agent"), "_delegate_session_id", None) == session_id
                     and getattr(live.get("agent"), "_delegate_store_path", None) == profile_db), (None, None))


def steer(parent, store, record, message):
    """Admit ``steer``: handed to the running claim when the child runs here, else a queued turn."""
    from hermes_state_runtime import RuntimeStoreError, revert_steer_input, steer_session_input
    from tools.delegate_session_store import runtime_epoch
    if not isinstance(message, str) or not message.strip():
        raise ValueError("action='steer' requires a non-empty 'message' describing the course correction.")
    if record.get("lineage_kind") != "delegate":
        # A session that is not this process's child (an attached webhook/cron session) is driven by its own
        # owner: the steer reaches it as the parent's typed message through that owner's sink, never as a
        # write into a FIFO this runner does not drain.
        return _resume_foreign(parent, store, record, message.strip(), action="steer")
    sid = record["session_id"]
    admission = store.admit(parent.session_id, sid, message, intent="steer")
    running = store.running_claim(sid)
    if running is not None and is_peer_only(running):
        # The child is answering a sibling: a silent turn never takes a parent's input (the steer would get
        # no completion). It runs as the next turn instead, and that one completes to the parent.
        remember_launch(admission["admission_id"], parent)
        return json.dumps({"action": "steer", "session_id": sid, "status": "queued_next_turn",
                           "admission_id": admission["admission_id"],
                           "note": "The child is answering a peer message; this steer runs as its next turn."})
    live_id, live = _live_record(parent, sid)
    if running is not None and live is not None:
        epoch = runtime_epoch(store.db)
        try:
            steer_session_input(store.db, epoch=epoch, admission_id=admission["admission_id"],
                                generation=running["generation"])
        except RuntimeStoreError:
            running = None
        else:
            from tools.delegate_tool_registry import steer_subagent
            agent = live.get("agent")
            note_steer_accepted(agent, admission["admission_id"])
            if steer_subagent(live_id, message.strip()):
                return json.dumps({"action": "steer", "session_id": sid, "status": "queued",
                                   "admission_id": admission["admission_id"],
                                   "note": "Steering text queued for the running child's next tool result."})
            vars(agent).get("_delegate_steer_transfers", {}).pop(admission["admission_id"], None)
            revert_steer_input(store.db, epoch=epoch, admission_id=admission["admission_id"],
                               generation=running["generation"])
    if running is not None:
        remember_launch(admission["admission_id"], parent)
        return json.dumps({"action": "steer", "session_id": sid, "status": "queued_next_turn",
                           "admission_id": admission["admission_id"],
                           "note": "The child's turn is closing; this steer runs as its next turn (nothing lost)."})
    remember_launch(admission["admission_id"], parent)
    schedule_drain(Path(store.db.db_path).resolve(), sid, parent)
    return json.dumps({"action": "steer", "session_id": sid, "status": "queued_next_turn",
                       "admission_id": admission["admission_id"],
                       "note": "The child was idle; the steer runs as its next turn."})


def _live_control(action, parent, record, message):
    from tools.delegate_tool_registry import _handle_control_action
    live_id, _live = _live_record(parent, record["session_id"])
    if live_id is None:
        raise ValueError("This child is not accepting live control. Use resume for an idle child.")
    result = json.loads(_handle_control_action(action, live_id, message, parent))
    result.pop("subagent_id", None)
    result["session_id"] = record["session_id"]
    return json.dumps(result)
