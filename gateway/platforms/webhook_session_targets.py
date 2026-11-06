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
    """Canonical target, or None only when no target was declared.

    Legacy internal_turn is an immediate, unattached session delivery, never permission to
    send raw worker text. Call with rendered deliver_extra at ingress to freeze each reply leg.
    Malformed declarations raise, so callers cannot mistake invalid for direct delivery.
    """
    from gateway.delivery_targets import DeliveryTargetError, parse_delivery_target
    if not isinstance(route_config, dict):
        raise DeliveryTargetError("a webhook route must be an object")
    mode = route_config.get("delivery_mode", "direct")
    if mode not in ("direct", "internal_turn"):
        raise DeliveryTargetError("delivery_mode must be direct or internal_turn")
    target = route_config.get("delivery_target")
    if target is None and mode == "internal_turn":
        if route_config.get("deliver_only") or route_config.get("cron_job"):
            raise DeliveryTargetError("internal_turn requires an agent completion")
        target = legacy_session_target(route_config)
    if target is None:
        return None
    if not isinstance(target, dict):
        raise DeliveryTargetError("delivery_target must be an object")
    kind = target.get("kind")
    if kind == "session":
        shaped = {"kind": kind, "attach": target.get("attach", False)}
        for key in ("session_id", "selector", "wake_policy"):
            if key in target:
                shaped[key] = target[key]
        if mode == "internal_turn":
            shaped.setdefault("wake_policy", "immediate")
        return parse_delivery_target(shaped)
    if kind == "platform":
        if mode == "internal_turn":
            raise DeliveryTargetError("internal_turn requires a session target")
        return _platform_target(target)
    if mode == "internal_turn":
        raise DeliveryTargetError("internal_turn requires a session target")
    return parse_delivery_target(target)


def _platform_target(target: dict) -> dict:
    from gateway.delivery_targets import DeliveryTargetError
    shaped = {"kind": "platform"}
    for key in ("platform", "chat_id", "thread_id"):
        if key in target and not (key == "thread_id" and target[key] is None):
            value = target[key]
            if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
                raise DeliveryTargetError(f"platform target needs a valid {key}")
            shaped[key] = str(value)
    if not shaped.get("platform") or not shaped.get("chat_id"):
        raise DeliveryTargetError("platform target needs platform and chat_id")
    return shaped


def legacy_session_target(route_config: dict) -> dict:
    """Translate old deliver/deliver_extra into the canonical logical selector grammar."""
    from gateway.delivery_targets import DeliveryTargetError, parse_delivery_target
    from gateway.platforms.webhook import _is_known_platform
    platform = route_config.get("deliver")
    extra = route_config.get("deliver_extra", {})
    if not isinstance(platform, str) or not _is_known_platform(platform) or not isinstance(extra, dict):
        raise DeliveryTargetError("internal_turn requires a live gateway messaging platform")
    chat = extra.get("chat_id")
    thread = extra.get("message_thread_id") or extra.get("thread_id")
    for value in (chat, thread):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))
                                  or not str(value).strip() or any(c in str(value) for c in "{}\n\r")):
            raise DeliveryTargetError("internal_turn needs rendered chat/thread identifiers")
    if chat in (None, ""):
        if thread is not None:
            raise DeliveryTargetError("an internal_turn thread needs a chat_id")
        selector = f"home:{platform}"
    else:
        from gateway.config import Platform, load_gateway_config
        from gateway.session import SessionSource, build_session_key
        cfg = load_gateway_config()
        source = SessionSource(platform=Platform(platform), chat_id=str(chat),
                               thread_id=str(thread) if thread is not None else None,
                               chat_type=str(extra.get("chat_type") or "dm"), user_id=extra.get("user_id"))
        selector = build_session_key(source, group_sessions_per_user=cfg.group_sessions_per_user,
                                     thread_sessions_per_user=cfg.thread_sessions_per_user).split(":", 2)[2]
    return parse_delivery_target({"kind": "session", "selector": selector,
                                  "attach": False, "wake_policy": "immediate"})


def worker_instruction_role(runner: Any, source: Any) -> Optional[str]:
    """A handoff worker uses scoped subagent instructions; its receiving chat never does."""
    from gateway.config import Platform
    if source is None or getattr(source, "platform", None) != Platform.WEBHOOK:
        return None
    adapter = runner._adapter_for_source(source)
    name = str(getattr(source, "user_id", "") or "").removeprefix("webhook:")
    config = adapter._routes.get(name) or {}
    target = config.get("delivery_target") or {}
    if config.get("delivery_mode") != "internal_turn" and target.get("kind") != "session":
        return None
    from gateway.platforms.webhook_delivery import retained_destination
    delivery = retained_destination(adapter, source.chat_id)
    declared = route_delivery_target(delivery)
    return "subagent" if declared and declared["kind"] == "session" else None


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
