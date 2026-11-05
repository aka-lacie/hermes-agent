"""Compression requests are internal prompts, not renderer-owned conversation turns."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

from agent import context_compressor as cc
from agent import message_parts as mp
from agent import wire_render as wr
from agent.transports.chat_completions import ChatCompletionsTransport


def test_compressor_internal_request_bypasses_rendering_and_has_no_parts(monkeypatch):
    monkeypatch.setattr(cc, "get_model_context_length", lambda *a, **kw: 100000)
    compressor = cc.ContextCompressor(model="test", provider="summary-test", quiet_mode=True)
    row = {"role": "user"}
    mp.set_parts(row, [mp.human_message("Keep this request verbatim.", origin={"message_id": "15"}),
                       mp.turn_injection("jev_system1", "private per-turn hint")])
    original = copy.deepcopy(row)
    prompt = "Internal summary request: summarize the following transcript.\n\nKeep this request verbatim."
    monkeypatch.setattr(compressor, "_build_summary_prompt", lambda *a, **kw: prompt)
    render = Mock(side_effect=AssertionError("internal prompts must not be rendered"))
    monkeypatch.setattr(wr, "render_messages", render)

    class InternalPromptTrap(wr.WireRenderer):
        def render(self, messages, *, agent=None):
            raise AssertionError("compressor request entered the conversation renderer")

    def provider_call(**kwargs):
        assert kwargs["messages"] == [{"role": "user", "content": prompt}]
        assert "_parts" not in repr(kwargs["messages"])
        # The actual transport leaves the synthesized request bytes untouched as well.
        wire = ChatCompletionsTransport().convert_messages(kwargs["messages"], model="test", base_url="http://localhost")
        assert wire == kwargs["messages"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Summary of the request."),
                                                        finish_reason="stop")])

    monkeypatch.setattr(cc, "call_llm", provider_call)
    wr.register_renderer(InternalPromptTrap(), provider="summary-test")
    try:
        summary = compressor._generate_summary([row])
    finally:
        wr.unregister_renderer(provider="summary-test")
    assert summary and "Summary of the request." in summary
    render.assert_not_called()
    assert row == original  # _parts remain on canonical history, never on the aux prompt
