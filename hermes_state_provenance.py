"""Message provenance for SessionDB: bind/restore of the ``messages`` provenance columns and the Rule 1
assistant-origin write gate.

Bind/restore pair with ``agent.message_parts``: the in-memory keys are the column names, except
``_parts`` <-> ``parts_json``. ``parts_json`` is written only while ``content`` is still the plain view
of the parts; a row whose content was rewritten around its parts is stored untyped (content wins), so
stored parts can never disagree with what FTS, the UI and legacy readers see.

**Rule 1.** A new ``role="assistant"`` row holds only tokens this session's model generated. The append
paths (``append_message`` and the inserts of ``append_messages_batch``) admit one only when the write
names an origin from ``agent.message_parts.ASSISTANT_ORIGINS`` (the ``origin=`` argument, or the dict's
``_assistant_origin``). Anything else is a non-model writer: under pytest it raises
:class:`AssistantOriginError` (rows written directly by test code are fixtures and pass), in production
it is logged and rewritten to a ``role="user"``, ``sender_kind="system"`` row holding one system_event
part. Row re-issues inside the store (replace, compaction, import, rewind) are not appends and are not
gated: they copy rows that were admitted when first written.
"""

from __future__ import annotations

import logging
import math
import os
import sys
from pathlib import PurePath
from typing import Any, Mapping, MutableMapping, Optional

from agent.message_parts import (
    ASSISTANT_ORIGIN, ASSISTANT_ORIGINS, MESSAGE_PARTS, PROVENANCE_FIELDS, encode_parts_json, decode_parts_json,
    has_parts, parts_consistent, set_parts, system_event)

logger = logging.getLogger("hermes_state")

# None: strict under pytest, reroute otherwise. Tests of the production behaviour set False.
ASSISTANT_GATE_STRICT: Optional[bool] = None
UNATTRIBUTED_ASSISTANT_KIND = "unattributed_assistant_write"
UNATTRIBUTED_ASSISTANT_SENDER = "system:unattributed_assistant_write"
# Pass-through writer APIs: they forward the caller's dict, so the caller (a production producer, or test code
# seeding a fixture) is who wrote it. The gateway transcript store is SessionDB's per-message front door.
_PASS_THROUGH_MODULES = frozenset({"hermes_state", __name__, "gateway.session", "gateway.session_transcript"})
# Assistant-only columns a rerouted row must not keep (a user row never carries model reasoning).
_ASSISTANT_ONLY_FIELDS = ("reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items",
                          "codex_message_items", "finish_reason")


class AssistantOriginError(ValueError):
    """An assistant row was appended without a model/copy origin (Rule 1)."""


# ── column bind / restore ─────────────────────────────────────────────────────


def _text_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _epoch_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def stored_parts_json(msg: Mapping[str, Any]) -> Optional[str]:
    """``parts_json`` for *msg*: its parts while ``content`` is their plain view, else NULL (logged)."""
    if not has_parts(msg):
        return None
    if not parts_consistent(msg):
        logger.warning("Message content was rewritten around its typed parts; storing the row untyped")
        return None
    try:
        return encode_parts_json(msg[MESSAGE_PARTS])
    except ValueError as exc:
        logger.warning("Dropping invalid typed parts on write: %s", exc)
        return None


def provenance_row_params(msg: Mapping[str, Any]) -> tuple:
    """Bind values for the provenance columns, in ``_INSERT_MESSAGE_SQL`` order: sender_kind, sender_id,
    sent_at, source_event_id, intent, part_kind, parts_json, turn_outcome."""
    return (_text_or_none(msg.get("sender_kind")), _text_or_none(msg.get("sender_id")),
            _epoch_or_none(msg.get("sent_at")),
            None if msg.get("source_event_id") is None else str(msg.get("source_event_id")),
            _text_or_none(msg.get("intent")), _text_or_none(msg.get("part_kind")),
            stored_parts_json(msg), _text_or_none(msg.get("turn_outcome")))


def restore_provenance_columns(row: Any, msg: MutableMapping[str, Any]) -> None:
    """Copy a fetched row's provenance onto its message dict (every projection, like ``message_uid``).
    A row fetched without the columns (an identity-only probe) leaves *msg* untouched."""
    keys = row.keys()
    for column in PROVENANCE_FIELDS:
        if column in keys and row[column] is not None:
            msg[column] = row[column]
    if "parts_json" in keys and row["parts_json"] is not None:
        parts = decode_parts_json(row["parts_json"])
        if parts is not None:
            msg[MESSAGE_PARTS] = parts


# ── durable turn tail ─────────────────────────────────────────────────────────

# The newest conversation row's role for the failed-turn closers, with a row that closes its turn (a
# harness notice carrying ``turn_outcome``, or a failed-turn boundary the 2f migration re-tagged as a user
# row) reported as ``agent.turn_failure_copy.CLOSED_TURN_TAIL``: closure is a column, never a role.
TAIL_ROLE_SQL = ("SELECT CASE WHEN turn_outcome IS NOT NULL OR display_kind = 'failed_turn' THEN 'closed' "
                 "ELSE role END FROM messages WHERE session_id = ? AND active = 1 "
                 "AND role NOT IN ('session_meta', 'system') ORDER BY id DESC LIMIT 1")


# ── Rule 1 gate ───────────────────────────────────────────────────────────────


def _strict() -> bool:
    if ASSISTANT_GATE_STRICT is not None:
        return ASSISTANT_GATE_STRICT
    return "PYTEST_CURRENT_TEST" in os.environ


def _is_test_file(filename: str) -> bool:
    if filename in ("<string>", "<stdin>"):
        return True  # strict mode only runs under pytest: code passed inline (``-c``) is a test's helper process
    path = PurePath(filename)
    return "tests" in path.parts[:-1] or path.name.startswith(("test_", "conftest"))


def _written_by_test_code() -> bool:
    """The nearest caller outside the store and its pass-through writer APIs is test code (a fixture row),
    not a production writer."""
    frame = sys._getframe(2)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if not (module in _PASS_THROUGH_MODULES or module.startswith("hermes_state_")):
            return _is_test_file(frame.f_code.co_filename)
        frame = frame.f_back
    return False


def _reroute_to_system_event(msg: MutableMapping[str, Any]) -> None:
    content = msg.get("content")
    if isinstance(content, list):
        text = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    else:
        text = content if isinstance(content, str) else ("" if content is None else str(content))
    for key in _ASSISTANT_ONLY_FIELDS + (ASSISTANT_ORIGIN,):
        msg.pop(key, None)
    msg["role"] = "user"
    msg.update(sender_kind="system", sender_id=UNATTRIBUTED_ASSISTANT_SENDER, part_kind=UNATTRIBUTED_ASSISTANT_KIND)
    set_parts(msg, [system_event(UNATTRIBUTED_ASSISTANT_KIND, text, meta={"rerouted_role": "assistant"})])


def gate_assistant_write(msg: MutableMapping[str, Any], origin: Optional[str] = None) -> None:
    """Admit or reroute one row about to be appended (mutates *msg* when rerouting)."""
    if msg.get("role") != "assistant":
        return
    effective = msg.get(ASSISTANT_ORIGIN) or origin
    if effective in ASSISTANT_ORIGINS:
        return
    if _strict():
        if _written_by_test_code():
            return
        raise AssistantOriginError(
            f"assistant row appended with origin {effective!r}; only {sorted(ASSISTANT_ORIGINS)} may write "
            "role='assistant' (non-model text is a role='user' system_event)")
    if msg.get("tool_calls"):
        # Rewriting a call-bearing row would orphan its tool results; a model path is missing its origin.
        logger.error("assistant row with tool calls appended without an origin; kept as assistant (Rule 1)")
        return
    logger.warning("non-model assistant write (origin %r) rerouted to a system_event user row (Rule 1)", effective)
    _reroute_to_system_event(msg)
