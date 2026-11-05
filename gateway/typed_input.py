"""Typed live input for gateway turns (unified-session plan §4.8 Layer 2).

The gateway knows who wrote a turn's input, so it hands the agent loop typed parts
(``agent.message_parts``) plus row provenance instead of a pre-flattened string:

- a platform message from a person is one ``human`` part (``delivery="turn"``, or ``"steer"`` when it
  was sent while the agent was busy and delivered at a turn start) whose ``origin`` carries the
  platform, message id, channel id and send time. No triggering-message note, no timestamp head:
  the wire renderer formats the origin, and the system prompt says once how to use it.
- a self-injected notification (process completion, async subagent result, gateway notice) is one
  ``system_event`` part with ``sender_kind="system"``: it can never speak with the human's authority.

Everything here reads structured event/source fields; nothing parses message text.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.message_parts import human_turn_input, system_event_input

# Display kind of a self-injected turn -> the system event kind its typed row carries.
INTERNAL_EVENT_KINDS = {
    "async_delegation_complete": "subagent_complete",
    "process_complete": "process_complete",
    "internal_notification": "notification",
}


def _redactor(platform: Any, redact_pii: bool):
    """``(id_hasher, chat_hasher, sender_hasher)`` for the model-facing copy, or None when ids stay raw."""
    from gateway.session import _hash_chat_id, _hash_id, _hash_sender_id, _should_redact_pii

    try:
        if not _should_redact_pii(platform, bool(redact_pii)):
            return None
    except Exception:
        return None
    return _hash_id, _hash_chat_id, _hash_sender_id


def message_origin(event: Any, source: Any, sent_at: Optional[float], *, redact_pii: bool = False) -> dict:
    """``{platform, message_id, channel_id, sent_at}`` of a platform message (only the fields it has).
    ``channel_id`` is where the message lives (its thread when it has one): the id reply/react/pin need."""
    platform = getattr(getattr(source, "platform", None), "value", None) or str(getattr(source, "platform", "") or "")
    message_id = getattr(event, "message_id", None)
    channel_id = getattr(source, "thread_id", None) or getattr(source, "chat_id", None)
    origin: dict[str, Any] = {
        "platform": platform or None,
        "message_id": str(message_id) if message_id not in (None, "") else None,
        "channel_id": str(channel_id) if channel_id not in (None, "") else None,
        "sent_at": float(sent_at) if isinstance(sent_at, (int, float)) and not isinstance(sent_at, bool) else None,
    }
    hashers = _redactor(getattr(source, "platform", None), redact_pii)
    if hashers is not None:
        id_hash, chat_hash, _ = hashers
        if origin["message_id"]:
            origin["message_id"] = id_hash(origin["message_id"])
        if origin["channel_id"]:
            origin["channel_id"] = chat_hash(origin["channel_id"])
    return {key: value for key, value in origin.items() if value is not None}


def sender_id(source: Any, *, redact_pii: bool = False) -> Optional[str]:
    """``<platform>:<user id>`` of the person who sent it (hashed under PII redaction)."""
    platform = getattr(getattr(source, "platform", None), "value", None) or ""
    user_id = getattr(source, "user_id", None)
    if not platform or user_id in (None, ""):
        return None
    hashers = _redactor(getattr(source, "platform", None), redact_pii)
    return f"{platform}:{hashers[2](str(user_id)) if hashers else user_id}"


def event_sent_at(event: Any) -> Optional[float]:
    """The platform event time as epoch seconds (the gateway's own coercion), else None."""
    try:
        from gateway.message_timestamps import coerce_message_timestamp
        from hermes_time import get_timezone

        return coerce_message_timestamp(getattr(event, "timestamp", None), tz=get_timezone())
    except Exception:
        return None


def turn_typed_input(
    event: Any, source: Any, text: Any, sent_at: Optional[float], display_kind: Optional[str], *,
    redact_pii: bool = False,
) -> Optional[dict]:
    """The ``typed_input`` of one gateway turn whose persisted text is *text*, or None to keep the
    untyped path (local surfaces, synthetic goal/heartbeat/resume prompts, empty or multimodal input,
    kinds of self-injected event this module does not type)."""
    from gateway.config import Platform

    if not isinstance(text, str) or not text.strip() or getattr(source, "platform", None) == Platform.LOCAL:
        return None
    if getattr(event, "_heartbeat_session_id", None) or getattr(event, "inherit_channel_pin", False):
        return None
    metadata = getattr(event, "metadata", None) or {}
    if getattr(event, "internal", False):
        kind = INTERNAL_EVENT_KINDS.get(display_kind or "")
        if kind is None:
            return None
        meta = {"category": "diagnostic"} if metadata.get("notification_category") == "diagnostic" else {}
        message_id = getattr(event, "message_id", None)
        return system_event_input(
            kind, text, sender_kind="system", sender_id=f"system:{kind}", meta=meta, sent_at=sent_at,
            source_event_id=str(message_id) if message_id not in (None, "") else None)
    if display_kind is not None:
        return None
    delivery = "steer" if metadata.get("delivery") == "steer" else "turn"
    return human_turn_input(
        text, delivery=delivery, origin=message_origin(event, source, sent_at, redact_pii=redact_pii),
        sender_id=sender_id(source, redact_pii=redact_pii))


def steer_origin(event: Any, *, redact_pii: bool = False) -> dict:
    """The origin a mid-turn steer carries to ``AIAgent.steer(origin=...)``."""
    return message_origin(event, getattr(event, "source", None), event_sent_at(event), redact_pii=redact_pii)
