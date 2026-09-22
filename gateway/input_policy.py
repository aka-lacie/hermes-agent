"""Delivery priority is distinct from synthetic origin and internal_turn routing."""
from __future__ import annotations

from copy import deepcopy


def is_task_feedback(event) -> bool:
    return (getattr(event, "internal", False) is True
            and getattr(event, "task_feedback", False) is True
            and not (getattr(event, "metadata", None) or {}).get("internal_notification"))


def priority(event) -> int:
    if not getattr(event, "internal", False):
        return 0
    return 1 if is_task_feedback(event) else 2


def input_record(event, *, text=None, origin=None) -> dict:
    if priority(event) == 2:
        raise ValueError("internal_turn and other external events do not steer")
    origin = deepcopy(origin or {})
    if origin.get("platform") == "discord" and "chat_id" in origin:
        origin["channel_id"] = origin["chat_id"]
    if getattr(event, "timestamp", None):
        origin["timestamp"] = event.timestamp.isoformat()
    return {"kind": "task" if is_task_feedback(event) else "user",
            "text": event.text if text is None else text,
            "origin": origin,
            "event_id": str(getattr(event, "message_id", None) or "")}


def task_feedback_event(evt: dict) -> bool:
    """Only runtime-owned work producers opt in, never event text or plugin metadata."""
    if evt.get("type") == "async_delegation":
        return True
    return (evt.get("type") in {"completion", "watch_match"}
            and bool(evt.get("parent_session_id"))
            and str(evt.get("session_key") or "").startswith("agent:"))
