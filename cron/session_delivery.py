"""Cron delivery through a job's ``delivery_target`` (gateway/delivery_targets.py, BRIEF3 contract).

A job may declare ``delivery_target``:

- ``{"kind": "session", "session_id": ... | "selector": ..., "attach": ..., "wake_policy": ...}``: the run's
  final output is admitted into that session (a selector: the session its conversation holds at delivery
  time) exactly like a child completion: one ``role="user"`` system event (``kind="cron"``,
  ``sender_kind="cron"``, ``sender_id="cron:<job id>"``), honouring the wake policy. No platform message is
  sent. The fire's session records the target (``scheduler_routes``), so ``attach: true`` lists it there.
- ``{"kind": "platform", "platform": ..., "chat_id": ..., "thread_id": ...}``: the output goes to that channel
  only. No mirror and no seeded continuation session: nothing is written into any other session's transcript.
- ``{"kind": "none"}``: nothing is delivered.

A job without the key keeps its ``deliver`` configuration unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def legacy_session_target(job: dict) -> Optional[dict]:
    """Translate a legacy internal-turn address to the canonical conversation selector."""
    from gateway.delivery_targets import session_target
    from cron.scheduler_delivery import _resolve_delivery_targets
    value = job.get("deliver") or ""
    attach = job.get("attach_to_session") is True
    parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
    if len(parts) != 1 or str(parts[0]).strip() in {"", "local", "all"}:
        raise ValueError("internal_turn requires one session conversation, not local or broadcast delivery.")
    token = str(parts[0]).strip()
    origin = job.get("origin") or {}
    if token == "origin" and origin.get("session_key"):
        return session_target(selector=origin["session_key"], attach=attach, wake_policy="immediate")
    if token == "origin" and origin.get("session_id"):
        return session_target(selector=f"session:{origin['session_id']}", attach=attach, wake_policy="immediate")
    if token == "bot-chat":
        return session_target(selector="bot-chat", attach=attach, wake_policy="immediate")
    if token.startswith(("bot-chat:", "room:")):
        raise ValueError("internal_turn requires a session selector; use delivery_target for this destination.")
    targets = _resolve_delivery_targets(job)
    if len(targets) != 1:
        return None
    target = targets[0]
    if target.get("_resolved_from") in {"origin_fallback", "home"}:
        return session_target(selector=f"home:{target['platform']}", attach=attach, wake_policy="immediate")
    from gateway.config import Platform, load_gateway_config
    from gateway.session import SessionSource, build_session_key
    cfg = load_gateway_config()
    source = SessionSource(
        platform=Platform(target["platform"]), chat_id=str(target["chat_id"]),
        thread_id=target.get("thread_id"), user_id=target.get("user_id") or origin.get("user_id"),
        chat_type=target.get("chat_type") or origin.get("chat_type") or ("thread" if target.get("thread_id") else "dm"),
        scope_id=origin.get("scope_id"))
    # Relative key selectors bind to the owning store's namespace, never the launch/default profile.
    selector = build_session_key(source, group_sessions_per_user=cfg.group_sessions_per_user,
                                 thread_sessions_per_user=cfg.thread_sessions_per_user).split(":", 2)[2]
    return session_target(selector=selector, attach=attach, wake_policy="immediate")


def normalize_job_delivery(job: dict) -> None:
    """Validate explicit targets and materialize internal-turn selectors at the write boundary."""
    from gateway.delivery_targets import parse_delivery_target
    if job.get("delivery_target") is not None:
        target = parse_delivery_target(job["delivery_target"])
        if job.get("delivery_mode") == "internal_turn" and target["kind"] != "session":
            raise ValueError("internal_turn requires a session delivery_target.")
        if target["kind"] == "session":
            target.setdefault("wake_policy", "immediate")
        job["delivery_target"] = target
    elif job.get("delivery_mode") == "internal_turn":
        target = legacy_session_target(job)
        if target is not None:
            job["delivery_target"] = target


def normalize_job_delivery_update(job: dict, updated: dict, updates: dict) -> None:
    """Re-address inferred selectors, while preserving an explicitly chosen canonical target."""
    if "delivery_target" not in updates and job.get("delivery_mode") == "internal_turn":
        try:
            inferred = legacy_session_target(job)
        except ValueError:
            inferred = None
        if job.get("delivery_target") == inferred:
            updated.pop("delivery_target", None)
    normalize_job_delivery(updated)


def job_delivery_target(job: dict) -> Optional[dict]:
    """The parsed target a job declares, or None when it keeps its ``deliver`` configuration."""
    raw = job.get("delivery_target")
    if raw is None:
        return legacy_session_target(job) if job.get("delivery_mode") == "internal_turn" else None
    from gateway.delivery_targets import DeliveryTargetError, parse_delivery_target
    try:
        target = parse_delivery_target(raw)
        if target["kind"] == "session" and job.get("delivery_mode") == "internal_turn":
            target.setdefault("wake_policy", "immediate")
        return target
    except DeliveryTargetError:
        logger.warning("cron job %s: malformed delivery_target; nothing is delivered", job.get("id"), exc_info=True)
        return {"kind": "none"}


def platform_only(job: dict) -> bool:
    """A ``kind=platform`` job: deliver to the channel, never mirror or seed a session."""
    if job.get("_delivery_target_platform_only"):
        return True
    target = job_delivery_target(job)
    return bool(target) and target["kind"] == "platform"


def platform_job(job: dict, target: dict) -> dict:
    """The job copy the ordinary scheduler delivery sends to exactly that channel, mirror off."""
    deliver = f"{target['platform']}:{target['chat_id']}" if target.get("chat_id") else target["platform"]
    if target.get("chat_id") and target.get("thread_id"):
        deliver += f":{target['thread_id']}"
    return {**job, "deliver": deliver, "attach_to_session": False, "failure_deliver": None}


def declared_delivery(job: dict, content: str, *, owner_handoff: bool = False):
    """``(True, error-or-None)`` when the job's delivery_target delivered (or deliberately delivered nothing);
    ``(False, job)`` to continue the ordinary scheduler delivery with *job* (rewritten for kind=platform).
    ``owner_handoff``: the caller holds no gateway adapters (:func:`deliver_to_session`)."""
    declared = job_delivery_target(job)
    if job.get("delivery_mode") == "internal_turn" and declared is not None and declared["kind"] != "session":
        return True, "internal-turn delivery requires a valid session delivery_target"
    if declared is None:
        if job.get("delivery_mode") == "internal_turn":
            return True, "internal-turn delivery requires a resolvable session conversation; no target resolved"
        return False, job
    if declared["kind"] == "none":
        return True, None
    if declared["kind"] == "session":
        return True, deliver_to_session(job, content, declared, owner_handoff=owner_handoff)
    return False, platform_job(job, declared)


_REWRITTEN_KEYS = frozenset({"deliver", "attach_to_session", "failure_deliver", "delivery_target", "delivery_mode"})


def _copy_delivery_bookkeeping(job: dict, rewritten: dict) -> None:
    for key, value in rewritten.items():
        if key not in _REWRITTEN_KEYS and key != "_delivery_target_platform_only":
            job[key] = value


def _queue_external_session_delivery(job: dict, content: str, adapters, for_failure: bool):
    """A script worker has no session-owner sink: let the live gateway perform the admission."""
    import os
    execution = os.environ.get("_HERMES_CRON_EXTERNAL_WORKER", "")
    if not execution or adapters is not None or execution != str(job.get("execution_id") or ""):
        return False, None
    target = job_delivery_target(job)
    if target is None or target["kind"] != "session":
        return False, None
    from cron.delivery_queue import enqueue_and_wait, get_status
    from cron.jobs import get_job
    error = enqueue_and_wait(execution, job, content, for_failure=for_failure)
    status = get_status(execution)
    if status and status["status"] == "suppressed":
        job["_notification_all_targets_suppressed"] = True
    job["last_delivery_queued"] = (get_job(job["id"]) or {}).get("last_delivery_queued")
    return True, error


def route_declared(job: dict, content: str, deliver, adapters=None, loop=None, *, for_failure: bool = False):
    """``(True, error-or-None)`` when the job's delivery_target settled the delivery: a session admission,
    nothing (kind=none), or one channel (``deliver`` re-run on a rewritten copy without the target, its
    bookkeeping copied back). ``(False, None)``: no target, the ordinary delivery continues."""
    # An explicit failure lane supersedes the normal session target, including local suppression.
    if for_failure and job.get("failure_deliver") and (job.get("delivery_target") is not None or job.get("delivery_mode") == "internal_turn"):
        rewritten = {k: v for k, v in job.items() if k != "delivery_target"}
        rewritten.update(deliver=job["failure_deliver"], failure_deliver=None, delivery_mode="direct")
        error = deliver(rewritten, content, adapters, loop, for_failure=False)
        _copy_delivery_bookkeeping(job, rewritten)
        return True, error
    try:
        handled, outcome = _queue_external_session_delivery(job, content, adapters, for_failure)
        if not handled:
            handled, outcome = declared_delivery(job, content, owner_handoff=adapters is None)
    except (ValueError, KeyError) as exc:
        return True, f"cron session delivery failed: {exc}"
    if handled:
        from cron.scheduler_delivery import _record_delivery_verification
        _record_delivery_verification(job, [])
        return True, outcome
    if outcome is job:
        return False, None
    rewritten = {k: v for k, v in outcome.items() if k != "delivery_target"}
    rewritten["_delivery_target_platform_only"] = True
    error = deliver(rewritten, content, adapters, loop, for_failure=for_failure)
    _copy_delivery_bookkeeping(job, rewritten)
    return True, error


def cron_event(job: dict, content: str):
    from gateway.delivery_targets import DeliveryEvent
    meta = {"job_id": str(job.get("id") or "")}
    for key in ("name", "execution_id"):
        if job.get(key):
            meta[key] = str(job[key])
    return DeliveryEvent("cron", content or "", "cron", f"cron:{job.get('id')}", meta=meta,
                         source_event_id=str(job.get("execution_id") or "") or None)


def deliver_to_session(job: dict, content: str, target: dict, *, db: Any = None,
                       owner_handoff: bool = False) -> Optional[str]:
    """Admit the run's output into the target session. None on success, else the error text.

    ``owner_handoff``: this process is no gateway (dashboard Run-now, ``hermes cron run``, a CLI session's
    cron tool, a headless tick), so a human chat only the gateway owner admits into answers ``no_owner``
    here. The event is then committed into that chat's own ``session_admissions`` FIFO as its typed
    automation row, for the owner to pick up (``gateway/session_automation_handoff.py``)."""
    from gateway.delivery_targets import dispatch
    own = db
    if own is None:
        from hermes_state_registry import acquire
        own = acquire()
    try:
        event = cron_event(job, content)
        request_id = f"cron:{job.get('id')}:{job.get('execution_id') or ''}".rstrip(":")
        receipt = dispatch(own, None, event, target=target, request_id=request_id)
        if owner_handoff and not receipt.delivered and receipt.reason == "no_owner" and receipt.target_session_id:
            from gateway.session_automation_handoff import admit_for_owner
            receipt = admit_for_owner(own, receipt.target_session_id, event, wake_policy=target.get("wake_policy"),
                                      request_id=request_id)
    finally:
        if db is None:
            from hermes_state_registry import release
            release(own)
    if receipt.delivered:
        return None
    return f"cron delivery to session {target.get('session_id') or target.get('selector')} failed: {receipt.reason}"
