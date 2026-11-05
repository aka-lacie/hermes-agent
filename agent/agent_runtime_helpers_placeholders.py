"""Interrupt placeholder wording substituted into empty non-final wire rows.

Split out of ``agent/agent_runtime_helpers.py`` (code-health size ratchet); a leaf module (it imports
only the stdlib-only ``agent.message_parts``) so every reader (runtime helpers, turn prep, the
conversation loop) can import it directly.
"""

from agent.message_parts import harness_notice_message

# Placeholder for an empty non-final message the provider would reject. This is the single source of
# the wording: every healing path imports it from here, so healed transcripts read consistently.
#
# Wording matters: this text is substituted into an assistant row's ``content`` on the wire copy
# (``fill_empty_non_final_wire_payload``, ``repair_empty_non_final_messages``, and the projection in
# ``run_conversation``), so the model reads it as something IT said. A short natural-language phrase
# in that position gets echoed verbatim — a clean ``finish_reason=stop`` turn answering an ordinary
# instruction with just the placeholder (#132949, and #81841 for the same hazard on the scaffold).
# Keep it a structural label the model has no conversational reason to reproduce, never prose.
_INTERRUPTED_PLACEHOLDER = "[interrupt: no assistant output for this turn]"

# The spelling shipped before #132949. Models reproduce it verbatim, so rows already carrying it
# stay poisoned after the wording change; the replay filters reference this name to retire them.
_LEGACY_INTERRUPTED_PLACEHOLDER = "[response interrupted]"


# The evidence an interrupt notice row carries when the turn ended with no model output at all.
INTERRUPTED_NO_OUTPUT_TEXT = "The turn was interrupted before the model replied."


def interrupt_notice_row(text: str = "", *, notice: str = "no_assistant_output", stopped: bool = True) -> dict:
    """The row that closes an interrupted turn (Rule 1: a ``role="user"`` system_event, never an assistant
    placeholder; the wire renderer merges consecutive non-assistant rows, so nothing has to keep strict
    alternation). With no *text* it is invisible in transcripts (``display_kind="hidden"``) and carries the
    neutral :data:`INTERRUPTED_NO_OUTPUT_TEXT`; a caller banner (truncation notice, partial-delivery copy)
    stays visible as the turn's only user-facing explanation. ``turn_outcome`` closes the turn:
    ``interrupted`` for a stop, ``partial`` for a terminal truncation/overflow exit."""
    outcome = "interrupted" if stopped else "partial"
    if not text:
        return harness_notice_message("interrupt", INTERRUPTED_NO_OUTPUT_TEXT, notice=notice, turn_outcome=outcome,
                                      display_kind="hidden")
    return harness_notice_message("interrupt" if stopped else "error_notice", text, notice=notice,
                                  turn_outcome=outcome)
