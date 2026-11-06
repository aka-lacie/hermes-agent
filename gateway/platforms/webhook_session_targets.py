"""Per-route ``delivery_target`` for webhook sessions (unified-session plan §4.4, BRIEF3 contract).

A route may declare where each turn's final output goes instead of its ``deliver`` / ``deliver_extra``:

    delivery_target: {kind: session, session_id: <target session id>, attach: true|false}
    delivery_target: {kind: session, selector: "discord:dm:<chat_id>" | discord | bot-chat | ..., attach: ...}
    delivery_target: {kind: platform, platform: discord, chat_id: "...", thread_id: "..."?}
    delivery_target: {kind: none}

The value is stored (credential-free, in the shared contract's shape) in ``sessions.delivery_target`` of the
session the delivery runs in. ``kind=session``: the final output goes through the ONE dispatcher,
``gateway.delivery_targets.dispatch`` (:func:`deliver_to_session_target`): it resolves a selector to the
conversation's current session at that moment and admits a ``role="user"`` ``system_event`` (kind
``webhook``, ``sender_kind="webhook"``, ``sender_id`` = this webhook session) exactly once per turn output;
``attach: true`` is the opt-in that lists this session in the target's default delegate list (bound through
``sessions.attached_session_id``, never ``parent_session_id``). ``kind=platform`` is delivered by the
adapter, through the route's own cross-platform sender. ``kind=none`` delivers nothing beyond the session.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

KINDS = ("session", "platform", "none")


def validate_delivery_target(name: str, target: Any) -> None:
    """Config-time validation of a route ``delivery_target`` (None = not declared)."""
    if target is None:
        return
    kind = target.get("kind") if isinstance(target, dict) else None
    if kind not in KINDS:
        raise ValueError(f"[webhook] Route '{name}' delivery_target.kind must be one of {', '.join(KINDS)}")
    if kind == "session":
        if route_delivery_target({"delivery_target": target}) is None:
            raise ValueError(f"[webhook] Route '{name}' delivery_target kind=session needs exactly one of "
                             "session_id or selector, and attach true or false")
        return
    for field in {"platform": ("platform", "chat_id")}.get(kind, ()):
        if not (isinstance(target.get(field), (str, int)) and str(target[field]).strip()):
            raise ValueError(f"[webhook] Route '{name}' delivery_target kind={kind} needs {field}")


def route_delivery_target(route_config: dict) -> Optional[dict]:
    """The contract-shaped ``sessions.delivery_target`` value a route declares, else None."""
    target = route_config.get("delivery_target") if isinstance(route_config, dict) else None
    if not isinstance(target, dict):
        return None
    kind = target.get("kind")
    if kind == "session":
        from gateway.delivery_targets import DeliveryTargetError, parse_delivery_target
        shaped = {"kind": "session", "attach": target.get("attach", False)}
        for key in ("session_id", "selector", "wake_policy"):
            if target.get(key) not in (None, ""):
                shaped[key] = str(target[key]) if key == "session_id" else target[key]
        try:
            return parse_delivery_target(shaped)
        except DeliveryTargetError:
            return None
    if kind == "platform":
        shaped = {"kind": "platform", "platform": str(target["platform"]), "chat_id": str(target["chat_id"])}
        if target.get("thread_id") not in (None, ""):
            shaped["thread_id"] = str(target["thread_id"])
        return shaped
    return {"kind": "none"} if kind == "none" else None


def platform_destination(target: dict, profile: Optional[str]) -> dict:
    """A ``kind=platform`` target as the adapter's destination dict (``WebhookAdapter._deliver_to``)."""
    extra = {"chat_id": target["chat_id"]}
    if target.get("thread_id"):
        extra["thread_id"] = target["thread_id"]
    return {"deliver": target["platform"], "deliver_extra": extra, "profile": profile}


def webhook_result_event(session_id: str, content: str, *, route: Optional[str] = None,
                         admission_seq: Any = None):
    """The typed event of one webhook turn's final output, from webhook session *session_id*."""
    from gateway.delivery_targets import DeliveryEvent
    meta = {"session_id": session_id, **({"route": route} if route else {})}
    digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]
    turn = f"{admission_seq}:" if admission_seq is not None else ""
    return DeliveryEvent("webhook", content or "", "webhook", session_id, meta=meta,
                         source_event_id=f"webhook:{session_id}:{turn}{digest}")


def deliver_to_session_target(row: dict, content: str, *, route: Optional[str] = None):
    """Hand a ``kind=session`` turn output to the shared dispatcher (blocking: call it off the owner loop).
    *row*: ``webhook_delivery.retained_session_target``'s ``{id, delivery_target, db, admission_seq}``.
    Returns the :class:`gateway.delivery_targets.DeliveryReceipt`. The request id is the event's
    ``source_event_id`` (session, turn, output digest): a re-sent identical output is the same admission."""
    from gateway.delivery_targets import dispatch
    db = row["db"]
    source = db._read_one("SELECT * FROM sessions WHERE id = ?", (row["id"],)) or {"id": row["id"]}
    event = webhook_result_event(row["id"], content, route=route, admission_seq=row.get("admission_seq"))
    return dispatch(db, source, event, request_id=event.source_event_id, target=row["delivery_target"])
