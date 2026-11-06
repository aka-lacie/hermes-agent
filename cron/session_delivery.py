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


def job_delivery_target(job: dict) -> Optional[dict]:
    """The parsed target a job declares, or None when it keeps its ``deliver`` configuration."""
    raw = job.get("delivery_target")
    if raw is None:
        return None
    from gateway.delivery_targets import DeliveryTargetError, parse_delivery_target
    try:
        return parse_delivery_target(raw)
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


def declared_delivery(job: dict, content: str):
    """``(True, error-or-None)`` when the job's delivery_target delivered (or deliberately delivered nothing);
    ``(False, job)`` to continue the ordinary scheduler delivery with *job* (rewritten for kind=platform)."""
    declared = job_delivery_target(job)
    if declared is None:
        return False, job
    if declared["kind"] == "none":
        return True, None
    if declared["kind"] == "session":
        return True, deliver_to_session(job, content, declared)
    return False, platform_job(job, declared)


_REWRITTEN_KEYS = frozenset({"deliver", "attach_to_session", "failure_deliver", "delivery_target"})


def route_declared(job: dict, content: str, deliver, adapters=None, loop=None, *, for_failure: bool = False):
    """``(True, error-or-None)`` when the job's delivery_target settled the delivery: a session admission,
    nothing (kind=none), or one channel (``deliver`` re-run on a rewritten copy without the target, its
    bookkeeping copied back). ``(False, None)``: no target, the ordinary delivery continues."""
    handled, outcome = declared_delivery(job, content)
    if handled:
        from cron.scheduler_delivery import _record_delivery_verification
        _record_delivery_verification(job, [])
        return True, outcome
    if outcome is job:
        return False, None
    rewritten = {k: v for k, v in outcome.items() if k != "delivery_target"}
    rewritten["_delivery_target_platform_only"] = True
    error = deliver(rewritten, content, adapters, loop, for_failure=for_failure)
    for key, value in rewritten.items():
        if key not in _REWRITTEN_KEYS and key != "_delivery_target_platform_only":
            job[key] = value
    return True, error


def cron_event(job: dict, content: str):
    from gateway.delivery_targets import DeliveryEvent
    meta = {"job_id": str(job.get("id") or "")}
    for key in ("name", "execution_id"):
        if job.get(key):
            meta[key] = str(job[key])
    return DeliveryEvent("cron", content or "", "cron", f"cron:{job.get('id')}", meta=meta,
                         source_event_id=str(job.get("execution_id") or "") or None)


def deliver_to_session(job: dict, content: str, target: dict, *, db: Any = None) -> Optional[str]:
    """Admit the run's output into the target session. None on success, else the error text."""
    from gateway.delivery_targets import dispatch
    own = db
    if own is None:
        from hermes_state_registry import acquire
        own = acquire()
    try:
        receipt = dispatch(own, None, cron_event(job, content), target=target,
                           request_id=f"cron:{job.get('id')}:{job.get('execution_id') or ''}".rstrip(":"))
    finally:
        if db is None:
            from hermes_state_registry import release
            release(own)
    if receipt.delivered:
        return None
    return f"cron delivery to session {target.get('session_id') or target.get('selector')} failed: {receipt.reason}"
