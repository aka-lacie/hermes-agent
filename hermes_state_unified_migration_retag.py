"""Phase 2f §4.7 re-tag: legacy ``role="assistant"`` rows the model never generated.

Rule 1 says an assistant row holds only tokens this session's model generated. Before Phase 2 several
harness paths wrote their own copy as assistant rows (failed-turn notices, the interrupt alternation
placeholder, "Operation interrupted" finals, tool-guardrail halts, compaction summaries) and
``hermes send`` mirrored script text as assistant. This module classifies those rows from columns
and the producers' exact fixed text (dev2 ``agent/turn_failure_copy.py``,
``agent/agent_runtime_helpers_placeholders.py``, ``agent/conversation_loop.py``,
``run_agent._toolguard_controlled_halt_response``, the compressor prefix) and re-tags each one as a
``role="user"``, ``sender_kind="system"`` row holding one system_event part. A row is a candidate
only when nothing model-side was stored on it (no tool calls, reasoning, provider items or token
count): a genuine model row is never touched. ``hermes send`` mirrors carry no marker at all, so
they are re-tagged only by explicit row id (forensic evidence), never by guess.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from agent.message_parts import system_event
from hermes_state_unified_migration_envelopes import COMPACTION_END, COMPACTION_PREFIXES

FAILED_TURN_NOTICES = {
    "Your request was not processed. Send it again if you still want me to carry it out.": "failed",
    "This turn did not complete. Some actions may already have run; verify their effects before resending.":
        "partial",
    "An automated delivery was interrupted and was not run again. Some actions may already have run; verify "
    "their effects.": "automation_interrupted",
}
INTERRUPT_PLACEHOLDER = "[interrupt: no assistant output for this turn]"
_WAITING_INTERRUPT = re.compile(r"Operation interrupted: waiting for model response \((\d+(?:\.\d+)?)s elapsed\)\.")
_BARE_INTERRUPT = "Operation interrupted."
_GUARDRAIL_HALT = re.compile(
    r"I stopped retrying because I kept running (.+?) (\d+) times without making progress\. The last result above "
    r"shows what blocked it\. Tell me how you'd like to proceed, or send `continue` and I'll try a different "
    r"approach\.")
# Columns only a model response fills. Any of them set -> the row is (at least partly) model output.
MODEL_COLUMNS = ("tool_calls", "reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items",
                 "codex_message_items", "gemini_content", "provider_data", "token_count")


@dataclass
class Retag:
    rule: str
    kind: str
    sender_id: str
    text: str
    meta: dict
    clear_api_content: bool = False

    def part(self) -> dict:
        return system_event(self.kind, self.text, meta=self.meta)


def has_model_columns(row: Mapping[str, Any]) -> bool:
    return any(row.get(column) not in (None, "", "[]") for column in MODEL_COLUMNS)


def _display_meta(row: Mapping[str, Any]) -> dict:
    try:
        meta = json.loads(row.get("display_metadata") or "{}")
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _failed_turn(row: Mapping[str, Any], content: str) -> Optional[Retag]:
    outcome = FAILED_TURN_NOTICES.get(content.strip())
    if outcome is None and row.get("display_kind") != "failed_turn":
        return None
    meta: dict = {"notice": "failed_turn", "turn_outcome": outcome or "failed"}
    display = _display_meta(row)
    for key in ("error", "error_surface"):
        if display.get(key) is not None:
            meta[key] = display[key]
    return Retag("failed_turn_notice", "error_notice", "system:failed_turn", content, meta)


def _interrupt(row: Mapping[str, Any], content: str) -> Optional[Retag]:
    if not content and row.get("api_content") == INTERRUPT_PLACEHOLDER:
        return Retag("interrupt_placeholder", "interrupt", "system:interrupt", "",
                     {"notice": "no_assistant_output"}, clear_api_content=True)
    match = _WAITING_INTERRUPT.fullmatch(content.strip())
    if match:
        return Retag("operation_interrupted", "interrupt", "system:interrupt", content.strip(),
                     {"notice": "operation_interrupted", "elapsed_s": float(match.group(1))})
    if content.strip() == _BARE_INTERRUPT:
        return Retag("operation_interrupted", "interrupt", "system:interrupt", content.strip(),
                     {"notice": "operation_interrupted"})
    return None


def _guardrail(row: Mapping[str, Any], content: str) -> Optional[Retag]:
    match = _GUARDRAIL_HALT.fullmatch(content.strip())
    if not match:
        return None
    meta = {"notice": "tool_guardrail_halt", "tool": match.group(1), "count": int(match.group(2))}
    return Retag("tool_guardrail_halt", "error_notice", "system:tool_guardrail", content.strip(), meta)


def _compaction(row: Mapping[str, Any], content: str) -> Optional[Retag]:
    if not content.startswith(COMPACTION_PREFIXES) or not content.rstrip().endswith(COMPACTION_END):
        return None
    return Retag("compaction_summary_as_assistant", "compaction", "system:compaction", content, {})


_RULES = (_failed_turn, _interrupt, _guardrail, _compaction)


def classify_assistant_row(row: Mapping[str, Any], mirror_ids: Iterable[int] = ()) -> Optional[Retag]:
    """The re-tag for a legacy non-model assistant row, or None (a genuine model row, or not certain)."""
    if row.get("role") != "assistant" or has_model_columns(row):
        return None
    content = row.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str) or content.startswith("\x00json:"):
        return None
    if row.get("id") in set(mirror_ids):
        return Retag("send_mirror_by_id", "send_mirror", "cli:send", content, {"notice": "hermes_send_mirror"})
    for rule in _RULES:
        retag = rule(row, content)
        if retag is not None:
            return retag
    return None


def bare_assistant_candidate(row: Mapping[str, Any]) -> bool:
    """An assistant row with no model-side column and no finish reason: reported for review, never re-tagged
    on this evidence alone (recovered streams look the same)."""
    return (row.get("role") == "assistant" and not has_model_columns(row) and not row.get("finish_reason")
            and not row.get("display_kind"))
