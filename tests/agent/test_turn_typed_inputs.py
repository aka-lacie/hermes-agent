"""Turn-level typed inputs: the length-continuation nudge is a typed harness system_event (never the
human's voice), and ``pre_llm_call`` receives the current turn's parts as the additive ``inputs=``."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import message_parts as mp
from hermes_constants import FINISH_REASON_LENGTH


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                    skip_context_files=True, skip_memory=True)
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.compression_enabled = False
        a.save_trajectories = False
        a.context_compressor.context_length = 32768
        return a


def _response(content, finish_reason):
    from tests.agent.test_run_agent import _mock_assistant_msg
    return SimpleNamespace(
        id="resp", model="test/model",
        choices=[SimpleNamespace(index=0, message=_mock_assistant_msg(content=content), finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=4000, completion_tokens=40, total_tokens=4040))


def test_length_continuation_nudge_is_a_typed_system_event(loop_agent):
    seen = []

    def create(**kwargs):
        seen.append((kwargs["messages"], [dict(m) for m in loop_agent._session_messages]))
        return _response("part ", FINISH_REASON_LENGTH) if len(seen) == 1 else _response("done", "stop")

    loop_agent.client.chat.completions.create.side_effect = create
    with patch.object(loop_agent, "_persist_session"), patch.object(loop_agent, "_save_trajectory"), \
            patch.object(loop_agent, "_cleanup_task_resources"):
        loop_agent.run_conversation("write a long report")
    wire, live = seen[1]
    [nudge] = [m for m in live if m.get("_length_continuation_nudge")]
    assert nudge["role"] == "user" and nudge["sender_kind"] == "system"
    assert nudge["part_kind"] == "continuation_nudge" and nudge["sender_id"] == "system:continuation_nudge"
    assert nudge["_parts"] == [mp.system_event("continuation_nudge", nudge["content"])]
    # The wire renderer frames the event as one system-reminder block, and no provenance key reaches
    # the provider.
    wire_nudge = wire[-1]
    assert wire_nudge["role"] == "user"
    assert wire_nudge["content"] == f'<system-reminder kind="continuation_nudge">\n{nudge["content"]}\n</system-reminder>'
    assert not {"_parts", "sender_kind", "sender_id", "part_kind"} & set(wire_nudge)


def test_pre_llm_call_receives_the_current_turns_parts_as_inputs():
    from agent.turn_context import _collect_pre_llm_call_context
    agent = SimpleNamespace(session_id="s", model="m", platform="cli", _persist_disabled=False)
    typed = mp.system_event_message("cron", "nightly ok", sender_id="cron:nightly")
    messages = [{"role": "user", "content": "earlier"}, typed]
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[]) as hook:
        _collect_pre_llm_call_context(agent, effective_task_id="t", turn_id="u", original_user_message="x",
                                      messages=messages, conversation_history=[], current_turn_user_idx=1)
    kwargs = hook.call_args.kwargs
    assert kwargs["inputs"] == typed["_parts"]
    assert kwargs["inputs"] is not typed["_parts"]  # a copy: hooks cannot mutate the turn
    assert kwargs["user_message"] == "x" and "conversation_history" in kwargs  # old kwargs kept
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[]) as hook:
        _collect_pre_llm_call_context(agent, effective_task_id="t", turn_id="u", original_user_message="x",
                                      messages=messages, conversation_history=[], current_turn_user_idx=0)
    assert hook.call_args.kwargs["inputs"] == []  # an untyped turn
