"""LEGACY SHIM — replay of UNTYPED gateway user rows. Delete in Phase 5 (unified-session plan §7).

Typed rows (``_parts``, Phase 2) never reach this module: their ``content`` is the plain view of their
parts and the send time rides in ``origin.sent_at`` for the wire renderer. What is left are rows the
2f migration did not type and the live turns that still go out untyped (decorated / multimodal gateway
turns, ``agent/turn_failure_copy``-era notes): their ``content`` may carry formats a later layer reads
back (Rule 2 exception, allowlisted by ``tests/ci/test_rule2_guard.py`` until Phase 5):

- a leading gateway timestamp head (``[Tue 2026-04-28 13:40:53 CEST]`` / legacy ISO), stripped so the
  replay renders exactly one, and whose embedded time wins over the row timestamp;
- leading gateway auto-continue notes (``[System note: Your previous turn …]``), which are never
  replayed.

The ``api_content`` sidecar of such a row is authoritative (Phase 0): it is exactly what was sent, so
it is kept as is and never validated by re-rendering and matching.
"""

from __future__ import annotations

from typing import Any, Optional

_AUTO_CONTINUE_NOTE_PREFIX = "[System note: Your previous turn"
_AUTO_CONTINUE_FALLBACK_PREFIX = "[System note: A new message"


def _legacy_is_auto_continue_note(content: Any) -> bool:
    return isinstance(content, str) and content.startswith((_AUTO_CONTINUE_NOTE_PREFIX, _AUTO_CONTINUE_FALLBACK_PREFIX))


def _legacy_strip_auto_continue_notes(content: str) -> str:
    text = content
    while _legacy_is_auto_continue_note(text):
        end = text.find("]")
        if end < 0:
            return ""
        text = text[end + 1:].lstrip()
    return text


def legacy_untyped_user_body(content: Any, tz=None) -> tuple[Any, Optional[float]]:
    """``(content, embedded_epoch)`` of an untyped user row for replay: the auto-continue notes behind
    a leading timestamp head are dropped (the embedded time is returned only when the text changed)."""
    if not isinstance(content, str):
        return content, None
    from gateway.message_timestamps import strip_leading_message_timestamps

    body, embedded = strip_leading_message_timestamps(content, tz=tz)
    clean = _legacy_strip_auto_continue_notes(body)
    if clean == body:
        return content, None
    return clean, embedded


def legacy_render_untyped_user(content: str, timestamp: Any, tz=None) -> str:
    """The untyped row's replay text with one leading timestamp head (``gateway.message_timestamps``)."""
    from gateway.message_timestamps import render_user_content_with_timestamp

    return render_user_content_with_timestamp(content, timestamp, tz=tz)
