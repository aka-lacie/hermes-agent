"""Per-route ``delivery_target`` for webhook sessions (unified-session plan §4.4, BRIEF3 contract).

A route may declare where each turn's final output goes instead of its ``deliver`` / ``deliver_extra``:

    delivery_target: {kind: session, session_id: <target session id>, attach: true|false}
    delivery_target: {kind: platform, platform: discord, chat_id: "...", thread_id: "..."?}
    delivery_target: {kind: none}

The value is stored verbatim (credential-free) in ``sessions.delivery_target`` of the session the
delivery runs in. ``kind=session``: the final output is admitted into the target session as a
role="user" system_event by the shared dispatcher (Worker A's ``gateway.delivery_targets``,
``resolve_delivery_target(row)``), reached only through :func:`dispatch_session_target` so it can be
stubbed; ``attach: true`` is the opt-in that lists this session in the target's default delegate list.
``kind=platform`` is delivered here, through the route's own cross-platform sender. ``kind=none``
delivers nothing beyond the session itself.
"""
from __future__ import annotations

import inspect
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
    required = {"session": ("session_id",), "platform": ("platform", "chat_id")}.get(kind, ())
    for field in required:
        if not (isinstance(target.get(field), (str, int)) and str(target[field]).strip()):
            raise ValueError(f"[webhook] Route '{name}' delivery_target kind={kind} needs {field}")
    if kind == "session" and not isinstance(target.get("attach", False), bool):
        raise ValueError(f"[webhook] Route '{name}' delivery_target.attach must be true or false")


def route_delivery_target(route_config: dict) -> Optional[dict]:
    """The contract-shaped ``sessions.delivery_target`` value a route declares, else None."""
    target = route_config.get("delivery_target") if isinstance(route_config, dict) else None
    if not isinstance(target, dict):
        return None
    kind = target.get("kind")
    if kind == "session":
        return {"kind": "session", "session_id": str(target["session_id"]), "attach": target.get("attach") is True}
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


async def dispatch_session_target(row: dict, content: str) -> bool:
    """Hand a ``kind=session`` turn output to the shared dispatcher. The thin seam over Worker A's
    ``gateway.delivery_targets.resolve_delivery_target(row)``, assumed to return None (nothing to
    dispatch) or a callable taking the final text (sync or async). False when the dispatcher is not
    available or declined: the caller then logs the output instead of guessing a destination."""
    try:
        from gateway.delivery_targets import resolve_delivery_target
    except ImportError:
        logger.warning("[webhook] delivery_target kind=session for %s but no dispatcher is installed",
                       row.get("id"))
        return False
    deliver = resolve_delivery_target(row)
    if not callable(deliver):
        return False
    result = deliver(content)
    if inspect.isawaitable(result):
        await result
    return True
