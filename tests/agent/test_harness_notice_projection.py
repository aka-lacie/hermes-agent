"""Harness notice evidence and human UI copy are independent projections."""

import pytest

from agent import message_parts as mp
from agent.turn_failure_copy import FAILED_TURN_COPY, failed_turn_boundary
from agent.wire_render import DEFAULT_RENDERER
from hermes_state import SessionDB


NEUTRAL_FAILED_TURN = "Previous turn ended with an error before delivering a response."


@pytest.mark.parametrize("outcome", ["failed", "partial", "interrupted"])
def test_failed_turn_stores_neutral_evidence_but_rehydrates_ui_copy(tmp_path, outcome):
    row = failed_turn_boundary(outcome, display_metadata={"error_surface": {"code": "timeout"}})
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s", source="test")
        db.append_messages_batch("s", [row])
        (stored,) = db.get_messages_as_conversation("s")
    assert stored["content"] == stored["_parts"][0]["text"] == NEUTRAL_FAILED_TURN
    assert stored["turn_outcome"] == outcome
    assert stored["display_metadata"]["error_surface"] == {"code": "timeout"}
    assert mp.project_harness_notice_for_display(stored)["content"] == FAILED_TURN_COPY[outcome]
    live, replay = DEFAULT_RENDERER.render([row]), DEFAULT_RENDERER.render([stored])
    assert live[0]["content"] == replay[0]["content"]
    assert NEUTRAL_FAILED_TURN in replay[0]["content"]
    assert FAILED_TURN_COPY[outcome] not in replay[0]["content"]
    from agent.message_metadata import without_persistence_fields
    from agent.transports.chat_completions import ChatCompletionsTransport
    api_rows = [without_persistence_fields(m) for m in replay]  # the request assembly's metadata sweep
    provider_rows = ChatCompletionsTransport().convert_messages(api_rows, model="test", base_url="http://localhost")
    assert FAILED_TURN_COPY[outcome] not in repr(provider_rows)


@pytest.mark.parametrize("notice,kind", [
    ("tool_guardrail_halt", "error_notice"), ("preflight_context_error", "error_notice"),
    ("pre_llm_call_blocked", "error_notice"), ("pending_tool_result", "error_notice"),
    ("interrupted_tool_sequence", "interrupt"), ("runaway_partial_interrupted", "interrupt"),
    ("truncation", "error_notice"),
])
def test_sibling_notices_keep_ui_instructions_out_of_model_text(notice, kind):
    copy = "I stopped this turn. Send it again if you still want me to carry it out."
    row = mp.harness_notice_message(kind, copy, notice=notice, turn_outcome="partial",
                                    meta={"error_class": "RuntimeError"})
    assert copy not in row["content"] and copy not in repr(row["_parts"])
    (wire,) = DEFAULT_RENDERER.render([row])
    assert copy not in wire["content"] and 'error_class="RuntimeError"' in wire["content"]
    assert mp.project_harness_notice_for_display(row)["content"] == copy


def test_custom_boundary_ui_copy_does_not_replace_neutral_event():
    row = failed_turn_boundary("interrupted", notice="The gateway restarted; send /retry.")
    assert row["_parts"][0]["text"] == NEUTRAL_FAILED_TURN
    assert mp.project_harness_notice_for_display(row)["content"] == "The gateway restarted; send /retry."


@pytest.mark.parametrize("exit_reason", ["guardrail_halt", "ollama_runtime_context_too_small"])
def test_finalizer_keeps_the_display_reply_separate_from_notice_evidence(exit_reason):
    from types import SimpleNamespace
    from agent.turn_finalizer import HARNESS_REPLY_EXITS, _close_transcript_tail

    copy = "I stopped this turn; change the settings and try again."
    rows = [{"role": "user", "content": "hello"}]
    _close_transcript_tail(SimpleNamespace(), rows, copy, False, False,
                           harness_exit=HARNESS_REPLY_EXITS[exit_reason])
    notice = rows[-1]
    assert notice["role"] == "user" and notice["_parts"][0]["type"] == "system_event"
    assert copy not in notice["content"] and copy not in repr(notice["_parts"])
    assert mp.project_harness_notice_for_display(notice)["content"] == copy
