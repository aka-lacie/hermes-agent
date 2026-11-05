"""What a fake provider reads off the wire as the person's inputs.

Since Phase 2e a failed-turn / interrupt notice and a compaction summary are role="user" system_event
rows that the API copy merges into the neighbouring user turn as ``<system-reminder …>`` blocks, so one
wire user message can hold a lost request, the notice and the follower. Journey fakes key their
behaviour on the person's newest text, exactly as a model reads it: the text outside the blocks.
"""

from __future__ import annotations

import re

_BLOCK = re.compile(r"<system-reminder[^>]*>.*?</system-reminder>", re.DOTALL)


def person_inputs(content) -> list[str]:
    """The person's text segments of one wire user message, in order (harness blocks dropped)."""
    if not isinstance(content, str):
        return [content] if content else []
    return [part.strip() for part in _BLOCK.split(content) if part.strip()]


def latest_input(messages) -> str:
    """The newest person text of a request: the last segment of its last user message."""
    for message in reversed(messages):
        if message.get("role") == "user":
            segments = person_inputs(message.get("content", ""))
            return str(segments[-1]) if segments else ""
    return ""


def all_inputs(messages) -> list:
    """Every person text segment of a request's user messages, in order."""
    return [segment for m in messages if m.get("role") == "user" for segment in person_inputs(m.get("content"))]
