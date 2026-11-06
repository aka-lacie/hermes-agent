"""Delegate children as ordinary ``sessions`` rows, driven through ``session_admissions`` (plan §4.1, §4.5).

A child is a sessions row (``lineage_kind='delegate'``, ``parent_session_id``, ``root_session_id``,
``depth``, a credential-free ``route``, ``role``, ``workspace``, ``fork_origin``, ``status`` and
``delivery_target = {kind: session, session_id: parent, attach: true}``). Every assignment, resume and steer
is an admission on the child's own FIFO (principal ``parent:<parent conversation>``); a run claims the head,
settles it when the turn ends, and the drain claims the next. There is no second lock: a resume arriving while
the child runs simply waits in the FIFO, and a resume and a webhook-style delivery to the same session
serialize on it.

This is conversation metadata, not an execution checkpoint. A dead process leaves its started admission to be
settled ``interrupted`` (never replayed); the conversation is idle and resumable. The old ``delegate_sessions``
table is no longer written or required (TODO(phase5): its rows are read only through the one-way shim in
``agent.session_directory._legacy_delegate`` until the 2f migration has run everywhere).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Optional

from agent import session_directory as directory


class DelegateSessionError(ValueError):
    """A model-actionable refusal, with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


PARENT_PRINCIPAL_PREFIX = "parent:"
_RUNNER_PREFIX = "delegate.runner.v1."
# First sighting of a started claim WITHOUT a runner stamp (see ``DelegateSessionStore._reconcile``).
_UNSTAMPED_PREFIX = "delegate.runner.unstamped.v1."
#: How long a legacy stampless started claim on a delegate child must stay stampless (measured from
#: the first time a reconcile saw it) before it is treated as abandoned. The claim and its stamp now
#: commit in one transaction, so a stampless started row is either a pre-fix claimer that crashed
#: between its two writes (it stays stampless forever) or a pre-fix claimer still running during a
#: mixed-version deploy (it publishes its stamp within milliseconds). The grace separates the two.
LEGACY_UNSTAMPED_GRACE_S = 600.0


def runtime_epoch(db) -> int:
    """The profile's current runtime epoch (the gateway's while one serves it). A store no gateway ever owned
    gets epoch 1 without bumping anyone's: in-process children never claim the profile ownership lock."""
    def write(conn):
        conn.execute("INSERT INTO runtime_epoch(singleton,epoch,instance_id) VALUES(1,1,?) ON CONFLICT(singleton) "
                     "DO NOTHING", (f"delegate-local:{os.getpid()}",))
        return conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()[0]
    with db._read_ctx() as conn:
        row = conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()
    return row[0] if row is not None else db._execute_write(write)


def parent_principal(parent_key: str) -> str:
    return PARENT_PRINCIPAL_PREFIX + parent_key


def _runner_stamp() -> str:
    from hermes_state_common import _proc_start_ticks
    from hermes_state_pidns import pid_namespace_id
    return json.dumps({"pid": os.getpid(), "pidns": pid_namespace_id(), "start_ticks": _proc_start_ticks(os.getpid())})


def _stamp_on(conn, admission_id: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (_RUNNER_PREFIX + admission_id,)).fetchone()
    return row[0] if row is not None else None


def _stamp_dead(raw: Optional[str]) -> bool:
    """The stamped runner is provably gone (the lease liveness rule: indeterminate is alive)."""
    from hermes_state_common import _lock_holder_provably_dead
    try:
        return raw is not None and _lock_holder_provably_dead(json.loads(raw))
    except (TypeError, ValueError):
        return False


def _settled_write(session_id: str, status: Optional[str], *, only_if: Optional[str] = None, expect_stamp=None,
                   check_stamp: bool = False):
    """The same-transaction effect of settling one of this runner's claims: revalidate the ownership
    verdict the caller reached outside the transaction (``check_stamp``: the stamp is still exactly
    ``expect_stamp``), drop the runner stamp and first-sighting marker, and move the conversation's
    status, so no later claim can interleave between settlement and its bookkeeping."""
    def write(conn, row):
        from hermes_state_runtime import RuntimeStoreError
        if check_stamp and _stamp_on(conn, row["admission_id"]) != expect_stamp:
            raise RuntimeStoreError("stale_generation", field="runner", detail="claim ownership changed")
        conn.execute("DELETE FROM state_meta WHERE key IN (?, ?)",
                     (_RUNNER_PREFIX + row["admission_id"], _UNSTAMPED_PREFIX + row["admission_id"]))
        if status is not None:
            conn.execute("UPDATE sessions SET status = ? WHERE id = ?" + (" AND status = ?" if only_if else ""),
                         (status, session_id, *((only_if,) if only_if else ())))
    return write


def encode_token(claim: dict) -> str:
    return f"{claim['admission_id']}:{claim['generation']}:{claim['owner_epoch']}"


def decode_token(token: Any) -> Optional[tuple[str, int, int]]:
    try:
        admission_id, generation, epoch = str(token).split(":")
        return admission_id, int(generation), int(epoch)
    except (TypeError, ValueError):
        return None


class DelegateSessionStore:
    """Child sessions of one profile database. Reads go through ``agent.session_directory`` (Freya's list
    rule, rollups, ACL); writes are sessions-column stamps and admissions on the child's FIFO."""

    def __init__(self, db):
        self.db = db

    # ── reads ────────────────────────────────────────────────────────────────
    def _reconcile(self, session_id: str) -> None:
        """Settle a started admission whose runner is provably dead (or an ``unknown`` one a restarted
        gateway left behind) as ``interrupted``: never replayed, the conversation goes idle.

        Ownership proof is the runner stamp, which commits in the claim's own transaction
        (:meth:`claim`): a started row with a stamp is recovered only when that stamp's process is
        provably dead. A started row WITHOUT a stamp is never this runner's live claim, so it is not
        recovered on sight: on a non-delegate session it belongs to that session's owner (a gateway
        turn of a ``parent_message``) and is never touched; on a delegate child it is a legacy pre-fix
        claim, recovered only after it stayed stampless for :data:`LEGACY_UNSTAMPED_GRACE_S`. Every
        recovery revalidates the verdict inside its settlement transaction."""
        from hermes_state_runtime import RuntimeStoreError, list_session_admissions
        delegate = None
        for row in list_session_admissions(self.db, session_id=session_id):
            if row["status"] not in ("started", "unknown"):
                continue
            with self.db._read_ctx() as conn:
                stamp = _stamp_on(conn, row["admission_id"])
            if row["status"] == "unknown":
                # A restarted gateway's verdict on a parent's input (unchanged rule): resolved, never replayed.
                if not str(row["principal_id"]).startswith(PARENT_PRINCIPAL_PREFIX):
                    continue
            elif stamp is not None:
                if not _stamp_dead(stamp):
                    continue  # a live run (this process or another) still holds the FIFO
            else:
                if delegate is None:
                    found = self.db._read_one("SELECT lineage_kind FROM sessions WHERE id = ?", (session_id,))
                    delegate = found is not None and found["lineage_kind"] == "delegate"
                if not delegate or not str(row["principal_id"]).startswith(PARENT_PRINCIPAL_PREFIX):
                    continue  # not a claim this runner made: its owner (or an operator) resolves it
                if row["status"] == "started" and not self._legacy_unstamped_expired(row["admission_id"]):
                    continue
            try:
                self._recover(session_id, row, stamp)
            except RuntimeStoreError:
                continue

    def _legacy_unstamped_expired(self, admission_id: str) -> bool:
        """Record the first sighting of a stampless started claim; True once it outlived the grace."""
        import time
        key = _UNSTAMPED_PREFIX + admission_id
        now = time.time()

        def write(conn):
            conn.execute("INSERT INTO state_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                         (key, json.dumps(now)))
            return json.loads(conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()[0])
        try:
            first_seen = float(self.db._execute_write(write))
        except (TypeError, ValueError):
            return False
        return now - first_seen >= LEGACY_UNSTAMPED_GRACE_S

    def _recover(self, session_id: str, row: dict, stamp: Optional[str]) -> None:
        from hermes_state_runtime import resolve_unknown_session_input, settle_session_input
        epoch = runtime_epoch(self.db)
        guard = _settled_write(session_id, "idle", only_if="running", expect_stamp=stamp, check_stamp=True)
        if row["status"] == "started" and row["owner_epoch"] == epoch:
            settle_session_input(self.db, epoch=epoch, admission_id=row["admission_id"],
                                 generation=row["generation"], outcome="interrupted", _terminal_write=guard)
            return
        if row["status"] == "started":
            # Claimed under an older epoch by a runner that is gone: the same verdict a gateway
            # restart's recovery gives it, for this one row.
            self.db._write_rowcount("UPDATE session_admissions SET status = 'unknown' WHERE admission_id = ? "
                                    "AND status = 'started' AND owner_epoch != ?", (row["admission_id"], epoch))
        resolve_unknown_session_input(self.db, epoch=epoch, admission_id=row["admission_id"],
                                      generation=row["generation"], _terminal_write=guard)

    def _owned_row(self, conn, caller: str, session_id: str):
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None or not directory.allowed(self.db, conn, caller, row):
            # Do not disclose whether this ID belongs to a different parent.
            raise DelegateSessionError("unavailable", "No retained child with that session_id belongs to this conversation.")
        return row

    def _caller(self, caller: str) -> str:
        if not self.db._read_one("SELECT 1 FROM sessions WHERE id = ?", (caller,)):
            raise DelegateSessionError("unavailable", "The parent conversation is unavailable.")
        return caller

    def resolve_ref(self, ref: str) -> str:
        """*ref* as a session id: a literal id, or (no such row) a logical selector the conversation holds now
        (``discord:dm:<id>``, ``webhook:<route>:<key>``, ``bot-chat``, ...; ``gateway.session_selectors``).
        An unresolvable selector stays as given (the ACL then refuses it like any unknown id)."""
        if not isinstance(ref, str) or self.db._read_one("SELECT 1 FROM sessions WHERE id = ?", (ref,)):
            return ref
        from gateway.session_selectors import is_selector, resolve_session_selector
        if not is_selector(ref):
            return ref
        return resolve_session_selector(self.db, ref) or ref

    def get(self, caller: str, session_id: str) -> dict:
        self._caller(caller)
        session_id = self.resolve_ref(session_id)
        with self.db._read_ctx() as conn:
            self._owned_row(conn, caller, session_id)
        self._reconcile(session_id)
        with self.db._read_ctx() as conn:
            row = self._owned_row(conn, caller, session_id)
            tip = conn.execute("SELECT end_reason FROM sessions WHERE id = ?",
                               (directory.chain(conn, session_id)[-1],)).fetchone()
            if tip is None or tip[0] == "compression":
                # The compression tip is gone: never fall back to a stale segment's history.
                raise DelegateSessionError("unavailable", "The child transcript is unavailable.")
            return _public(directory.record(conn, row))

    def list(self, caller: str, *, query=None, limit=20, scope="children") -> list[dict]:
        self._caller(caller)
        with self.db._read_ctx() as conn:
            rows = directory.list_sessions(self.db, conn, caller, scope=scope, query=query, limit=limit)
        for row in rows:
            if row["status"] == "running":
                self._reconcile(row["session_id"])
        if any(row["status"] == "running" for row in rows):
            with self.db._read_ctx() as conn:
                rows = directory.list_sessions(self.db, conn, caller, scope=scope, query=query, limit=limit)
        return [_public(row) for row in rows]

    def observe(self, caller: str, session_id: str, *, limit=None, max_chars=None) -> dict:
        """The compact observe view (``agent.session_views``): newest *limit* messages, children roll-up."""
        session_id = self.get(caller, session_id)["session_id"]
        with self.db._read_ctx() as conn:
            return directory.observe(self.db, conn, caller, session_id, limit=limit, max_chars=max_chars)

    # ── writes ───────────────────────────────────────────────────────────────
    def register(self, parent_session_id: str, session_id: str, *, model, provider, assignment, context="",
                 title=None, role="leaf", depth=1, workspace=None, forked_context=False, route=None,
                 group_id=None, retain=True, fork_policy="fork_context", route_descriptor=None) -> dict:
        """Stamp a persisted child row as a delegate session (never overwriting a different parent)."""
        from agent.session_routes import RouteError, validate_route
        from gateway.delivery_targets import delegate_target, encode_delivery_target
        if route is None and route_descriptor is not None:
            route = route_descriptor
        if route is not None:
            try:
                route = validate_route(route)
            except RouteError as exc:
                raise DelegateSessionError("invalid_route", str(exc)) from exc
        title = str(title or " ".join(str(assignment).split())[:80]).strip() or "Delegated task"
        brief = {"title": title, "initial_goal": assignment, "context": context or "", "retain": bool(retain)}
        fork = {"session_id": parent_session_id, "message_uid": None, "policy": fork_policy} if forked_context else None

        def write(conn):
            if not conn.execute("SELECT 1 FROM sessions WHERE id = ?", (parent_session_id,)).fetchone():
                raise DelegateSessionError("unavailable", "The parent conversation is unavailable.")
            child = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if child is None:
                raise DelegateSessionError("unavailable", "Persist the child session before registering it.")
            if child["lineage_kind"] == "delegate":
                raise DelegateSessionError("already_registered", "This child session is already registered.")
            conn.execute(
                "UPDATE sessions SET lineage_kind = 'delegate', parent_session_id = COALESCE(parent_session_id, ?), "
                "role = ?, workspace = ?, fork_origin = ?, route = ?, status = 'idle', delivery_target = ?, "
                "group_id = COALESCE(group_id, ?), "
                "model_config = json_set(COALESCE(model_config, '{}'), '$._delegate_brief', json(?)) WHERE id = ?",
                (parent_session_id, role, workspace, json.dumps(fork) if fork else None,
                 json.dumps(route, sort_keys=True) if route else None,
                 encode_delivery_target(delegate_target(parent_session_id)), group_id, json.dumps(brief), session_id))
            if route is None:
                # The bare model/provider a test double or an unrouted child ran on, still credential-free.
                conn.execute("UPDATE sessions SET route = ? WHERE id = ?", (json.dumps(
                    {"v": 1, "kind": "named", "provider": str(provider or "").strip().lower() or None,
                     "model": model}, sort_keys=True), session_id))
            return directory.record(conn, conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())

        return _public(self.db._execute_write(write))

    def _set_status(self, session_id: str, status: str, *, only_if: Optional[str] = None) -> None:
        sql = "UPDATE sessions SET status = ? WHERE id = ?" + (" AND status = ?" if only_if else "")
        self.db._write_rowcount(sql, (status, session_id, *((only_if,) if only_if else ())))

    def _clear_runner(self, admission_id: str) -> None:
        self.db._write_rowcount("DELETE FROM state_meta WHERE key = ?", (_RUNNER_PREFIX + admission_id,))

    def admit(self, caller: str, session_id: str, message: Any, *, intent: str = "queue", request_id=None,
              model=None, wake_policy=None, extra=None) -> dict:
        """Queue one assignment on the child's FIFO. Returns the admission plus ``position`` (queued ahead of
        it) and ``busy`` (a run holds the FIFO now)."""
        from hermes_state_runtime import RuntimeStoreError, admit_session_input, list_session_admissions
        if not isinstance(message, str) or not message.strip():
            raise DelegateSessionError("invalid_message", "A nonempty assignment message is required.")
        self._caller(caller)
        with self.db._read_ctx() as conn:
            row = self._owned_row(conn, caller, session_id)
            info = directory.record(conn, row)
            owner = directory.logical_root(self.db, conn, caller)
        if info["status"] == "closed":
            raise DelegateSessionError("closed", "This child is closed; start a new delegation.")
        payload = {"text": message, "sender_kind": "parent", "sender_id": f"session:{caller}"}
        if model:
            payload["model"] = str(model)
        if extra:
            payload.update(extra)
        try:
            admission = admit_session_input(
                self.db, epoch=runtime_epoch(self.db), principal_id=parent_principal(owner), session_id=session_id,
                request_id=request_id or uuid.uuid4().hex, payload=payload, intent=intent, wake_policy=wake_policy)
        except RuntimeStoreError as exc:
            if exc.reason == "queue_full":
                raise DelegateSessionError("queue_full", "This child already has the maximum queued assignments.") from exc
            raise
        pending = list_session_admissions(self.db, session_id=session_id)
        busy = any(r["status"] in ("started", "unknown") for r in pending)
        position = sum(1 for r in pending if r["status"] == "queued" and r["seq"] < admission["seq"])
        return {**admission, "busy": busy, "position": position}

    def claim(self, session_id: str, *, head=None) -> Optional[dict]:
        """Claim the child FIFO head when no run holds it: the started admission (``token`` for release), or
        None while busy/empty. The queued->started transition, this runner's identity stamp and the
        conversation's ``running`` status commit in ONE transaction: no reader (another claim, get,
        observe, list) can see this claim started without proof of its live owner."""
        from hermes_state_runtime import _ANY_HEAD, RuntimeStoreError, claim_session_input
        self._reconcile(session_id)
        stamp = _runner_stamp()

        def publish(conn, claimed):
            conn.execute("INSERT OR REPLACE INTO state_meta(key, value) VALUES(?, ?)",
                         (_RUNNER_PREFIX + claimed["admission_id"], stamp))
            conn.execute("UPDATE sessions SET status = 'running' WHERE id = ?", (session_id,))
        try:
            claimed = claim_session_input(self.db, epoch=runtime_epoch(self.db), session_id=session_id,
                                          head=_ANY_HEAD if head is None else head, _claimed_write=publish)
        except RuntimeStoreError as exc:
            if exc.reason == "unknown_execution":
                return None
            raise
        if not claimed:
            return None
        claimed["token"] = encode_token(claimed)
        return claimed

    def acquire(self, caller: str, session_id: str, message: Any, *, model=None, request_id=None, extra=None):
        """Admit and claim now, or refuse ``busy`` (the admission is withdrawn: callers that must run
        immediately). Queueing callers use :meth:`admit` + :meth:`claim`."""
        from hermes_state_runtime import cancel_session_input
        admission = self.admit(caller, session_id, message, model=model, request_id=request_id, extra=extra)
        claimed = self.claim(session_id, head=admission["admission_id"]) if not admission["busy"] else None
        if not claimed or claimed["admission_id"] != admission["admission_id"]:
            if claimed:
                self.release(session_id, claimed["token"], outcome="interrupted")
            cancel_session_input(self.db, epoch=runtime_epoch(self.db), admission_id=admission["admission_id"])
            raise DelegateSessionError("busy", "This child is already running; use steer or observe.")
        return self.get(caller, session_id), claimed["token"]

    def touch(self, session_id: str, token: Any) -> bool:
        """Activity is derived from the transcript; a live token is all a progress tick proves."""
        decoded = decode_token(token)
        if decoded is None:
            return False
        row = self.db._read_one("SELECT status FROM session_admissions WHERE admission_id = ?", (decoded[0],))
        return row is not None and row["status"] == "started"

    def release(self, session_id: str, token: Any, *, current_session_id=None, outcome: str = "completed",
                transfers=None, final_status: Optional[str] = None) -> bool:
        """Settle the run *token* names (exactly once; a stale or foreign token is refused). The conversation
        goes idle (``final_status='closed'`` for a non-retained child)."""
        from hermes_state_runtime import (
            RuntimeStoreError, get_session_admission, resolve_unknown_session_input, settle_session_input,
        )
        decoded = decode_token(token)
        if decoded is None:
            return False
        admission_id, generation, epoch = decoded
        row = get_session_admission(self.db, admission_id=admission_id)
        if row is None or row["target_session_id"] != session_id or row["generation"] != generation:
            return False
        if current_session_id is not None:
            with self.db._read_ctx() as conn:
                if current_session_id not in directory.chain(conn, session_id):
                    raise DelegateSessionError("invalid_tip", "The supplied child tip is outside its compression lineage.")
        if row["status"] == "terminal":
            return False
        # Stamp cleanup and the status move commit with the settlement itself: a claim taken right after
        # it can never have its ``running`` status overwritten by this (now stale) reservation's cleanup.
        done = _settled_write(session_id, final_status or "idle")
        try:
            if row["status"] == "unknown":
                resolve_unknown_session_input(self.db, epoch=runtime_epoch(self.db), admission_id=admission_id,
                                              generation=generation, _terminal_write=done)
            else:
                settle_session_input(self.db, epoch=epoch, admission_id=admission_id, generation=generation,
                                     outcome=outcome if outcome in ("completed", "interrupted", "failed") else "failed",
                                     _transfers=transfers, _terminal_write=done)
        except RuntimeStoreError as exc:
            if exc.reason not in ("stale_epoch", "stale_generation"):
                raise
            # Revalidate: only this reservation's own claim, now ``unknown`` (a restart's verdict), may still
            # be resolved by it; a claim recovered or settled by anyone else refuses the stale token.
            again = get_session_admission(self.db, admission_id=admission_id)
            if again is None or again["status"] != "unknown" or again["generation"] != generation:
                return False
            try:
                resolve_unknown_session_input(self.db, epoch=runtime_epoch(self.db), admission_id=admission_id,
                                              generation=generation, _terminal_write=done)
            except RuntimeStoreError as again_exc:
                if again_exc.reason not in ("stale_epoch", "stale_generation"):
                    raise
                return False
        self._clear_runner(admission_id)  # idempotent: an already-resolved replay ran no terminal write
        return True

    def close(self, caller: str, session_id: str) -> dict:
        info = self.get(caller, session_id)
        if info["status"] == "running":
            raise DelegateSessionError("busy", "Stop the running child before closing it.")
        from hermes_state_runtime import cancel_session_input, list_session_admissions
        for row in list_session_admissions(self.db, session_id=session_id):
            if row["status"] == "queued":
                cancel_session_input(self.db, epoch=runtime_epoch(self.db), admission_id=row["admission_id"])
        self._set_status(session_id, "closed")
        return {**info, "status": "closed"}

    def set_route_model(self, session_id: str, model: str) -> None:
        """A resume's model override becomes the stored route's model (same provider, same chain)."""
        self.db._write_rowcount("UPDATE sessions SET route = json_set(COALESCE(route, '{}'), '$.model', ?) WHERE id = ?",
                                (str(model), session_id))

    def running_claim(self, session_id: str) -> Optional[dict]:
        """The started admission holding the child's FIFO, if any."""
        from hermes_state_runtime import list_session_admissions
        return next((r for r in list_session_admissions(self.db, session_id=session_id) if r["status"] == "started"), None)


def _public(record: dict) -> dict:
    """The roster/compatibility view: legacy field names kept for the plugin's ``_roster_entry``."""
    out = dict(record)
    out["route_descriptor"] = record.get("route")
    return out
