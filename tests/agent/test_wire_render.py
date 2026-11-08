"""The core wire renderer (unified-session plan §4.8 Layer 3, Phase 2c step 3).

One render point turns typed parts into wire text; this pins the default presentation, the route
registry, alternation by merging, the bypass paths, and that ``_parts`` never reaches a provider.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import message_parts as mp
from agent import wire_render as wr


def _human(text="hello", **origin):
    row = {"role": "user"}
    mp.set_parts(row, [mp.human_message(text, origin=origin or None)])
    return row


def test_default_presentation_of_each_part_type(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"gateway": {"message_timestamps": {"enabled": True}}})
    turn = _human("hello", platform="discord", message_id="15", channel_id="42", sent_at=0.0)
    mp.append_parts(turn, [mp.turn_injection("jev_system1", "route hint")])
    steer = {"role": "user"}
    mp.set_parts(steer, [mp.human_message("also", delivery="steer", origin={"message_id": "16"})])
    event = mp.system_event_message("process_complete", "Command: ls", sender_id="system:process_complete",
                                    meta={"session_id": "proc_1", "exit_code": 0})
    peer = mp.system_event_message("peer_message", "hi", sender_kind="peer", sender_id="s2",
                                   meta={"sender_id": "s2", "sender_title": "Reviewer", "relation": "sibling"})
    with patch.object(wr, "format_sent_at", lambda v: "T0" if v is not None else None):
        out = wr.DEFAULT_RENDERER.render([turn, steer, event, peer, _human("plain")])
    assert [m["content"] for m in out] == [
        '<origin platform="discord" message_id="15" channel_id="42" sent_at="T0"/>\nhello\n\n'
        '<system-reminder kind="jev_system1">\nroute hint\n</system-reminder>',
        '<steer message_id="16"/>\nalso',
        '<system-reminder kind="process_complete" exit_code="0" session_id="proc_1">\nCommand: ls\n</system-reminder>',
        '<peer_message from="s2" title="Reviewer" relation="sibling">\nhi\n</peer_message>',
        "plain",  # no origin: the raw text, nothing else
    ]
    # Rendered rows carry no parts and no row provenance; the inputs are untouched.
    assert all("_parts" not in m and "sender_kind" not in m for m in out)
    assert turn["content"] == "hello" and "_parts" in turn


def test_attribute_values_are_escaped():
    row = mp.system_event_message("webhook", "x", sender_id="webhook:r", meta={"route": 'a"<b>&'})
    (out,) = wr.DEFAULT_RENDERER.render([row])
    assert 'route="a&quot;&lt;b&gt;&amp;"' in out["content"]


def test_untyped_rows_pass_through_as_the_same_objects():
    rows = [{"role": "user", "content": "legacy"}, {"role": "assistant", "content": "ok"}]
    assert wr.DEFAULT_RENDERER.render(rows) is rows


def test_a_row_whose_content_was_rewritten_renders_its_content(caplog):
    row = _human("secret token", platform="discord", message_id="1")
    row["content"] = "[REDACTED]"  # a later writer won: nothing it removed comes back from the parts
    with caplog.at_level(logging.WARNING, logger="agent.wire_render"):
        (out,) = wr.DEFAULT_RENDERER.render([row])
    assert out["content"] == "[REDACTED]" and "_parts" not in out


def test_consecutive_non_assistant_rows_merge_into_one_wire_user_turn():
    from agent.agent_runtime_helpers import drop_thinking_only_and_merge_users

    event = mp.system_event_message("cron", "nightly ok", sender_id="cron:nightly")
    rows = [{"role": "assistant", "content": "earlier"}, event, _human("and now me", message_id="5")]
    merged = drop_thinking_only_and_merge_users(wr.DEFAULT_RENDERER.render(rows))
    assert [m["role"] for m in merged] == ["assistant", "user"]
    assert merged[1]["content"] == ('<system-reminder kind="cron">\nnightly ok\n</system-reminder>\n\n'
                                    '<origin message_id="5"/>\nand now me')


def test_registry_resolves_provider_then_api_mode_then_default():
    class Custom(wr.WireRenderer):
        name = "custom"

    custom = Custom()
    assert wr.resolve_renderer(SimpleNamespace(provider="x", api_mode="codex_responses")) is wr.RESPONSES_RENDERER
    assert wr.resolve_renderer(SimpleNamespace(provider="x", api_mode="chat_completions")) is wr.DEFAULT_RENDERER
    wr.register_renderer(custom, provider="x")
    try:
        assert wr.resolve_renderer(SimpleNamespace(provider="x", api_mode="codex_responses")) is custom
    finally:
        wr.unregister_renderer(provider="x")
    with pytest.raises(ValueError):
        wr.register_renderer(custom)


def test_a_native_renderer_keeps_parts_for_its_adapter_through_the_merge():
    from agent.agent_runtime_helpers import drop_thinking_only_and_merge_users

    class Native(wr.WireRenderer):
        native = True

    event = mp.system_event_message("cron", "nightly ok", sender_id="cron:nightly")
    human = _human("me")
    merged = drop_thinking_only_and_merge_users(Native().render([event, human]))
    assert len(merged) == 1 and merged[0][mp.RENDERED] is True
    # The default rendering still fills content; the adapter reads every part, in order.
    assert merged[0]["content"] == '<system-reminder kind="cron">\nnightly ok\n</system-reminder>\n\nme'
    assert [p["type"] for p in merged[0]["_parts"]] == ["system_event", "human"]
    # ...and the transport's underscore sweep still keeps both keys off the wire.
    from agent.transports.chat_completions import ChatCompletionsTransport

    [wire] = ChatCompletionsTransport().convert_messages(merged, model="gpt-4o", base_url="https://api.openai.com/v1")
    assert set(wire) == {"role", "content"}


def test_media_renders_bytes_inside_the_window_and_one_reference_outside_never_both():
    sha = "a" * 64

    def media_row(path):
        row = {"role": "user"}
        mp.set_parts(row, [mp.human_message("look"), mp.media_ref(sha, "image/png", path)])
        return row

    old, new = media_row("/m/old.png"), media_row("/m/new.png")
    with patch("agent.image_replay._turn_replay_window", return_value=1), \
            patch("agent.image_replay._retained_data_url", side_effect=lambda p: f"data:image/png;base64,{p}"):
        out = wr.DEFAULT_RENDERER.render([old, {"role": "assistant", "content": "seen"}, new], agent=object())
    assert out[0]["content"] == f'look\n\n<media mime="image/png" sha256="{sha}" path="/m/old.png"/>'
    assert out[2]["content"] == [{"type": "text", "text": "look"},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,/m/new.png"}}]


def test_render_messages_falls_back_to_the_plain_view_when_a_renderer_fails():
    class Broken(wr.WireRenderer):
        def render(self, messages, *, agent=None):
            raise RuntimeError("boom")

    wr.register_renderer(Broken(), provider="broken")
    try:
        (out,) = wr.render_messages(SimpleNamespace(provider="broken", api_mode=""), [_human("hi", message_id="1")])
    finally:
        wr.unregister_renderer(provider="broken")
    assert out["content"] == "hi" and "_parts" not in out


@pytest.fixture
def chat_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent

    agent = AIAgent(api_key="k", base_url="https://api.groq.com/openai/v1", provider="custom", model="m",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    from agent.turn_context import _PER_TURN_RESET_STATE

    for name, value in _PER_TURN_RESET_STATE:  # the turn state a request needs (as ctx.side_question sets it)
        setattr(agent, name, value)
    agent._cached_system_prompt = "SYS"
    agent._current_turn_timestamp = time.time()
    agent._current_turn_id, agent._current_task_id, agent._current_api_request_id = "u", "t", "r"
    agent._disable_streaming = True
    return agent


def _wire(agent, messages, idx):
    from agent.turn_api_request import build_request_payload
    from agent.turn_request_assembly import assemble_api_request

    assembled = assemble_api_request(
        agent, messages=messages, current_turn_user_idx=idx, _ext_prefetch_cache=None, _plugin_user_context=None,
        moa_config=None, active_system_prompt="SYS", original_user_message=None, pending_moa_prepared_request=None,
        request_logger=logging.getLogger("test"))
    built = build_request_payload(
        agent, api_messages=assembled.api_messages, _moa_prepared_request=None, tools_for_api=None,
        system_message=None, api_call_count=1, api_request_id="r", effective_task_id="t", turn_id="u")
    return built.api_kwargs


def test_the_render_point_reaches_the_provider_payload_without_parts(chat_agent):
    turn = _human("what changed?", platform="discord", message_id="9")
    mp.append_parts(turn, [mp.turn_injection("memory_context", "a recalled fact")])
    kwargs = _wire(chat_agent, [turn], 0)
    user = kwargs["messages"][-1]
    assert user == {"role": "user", "content": '<origin platform="discord" message_id="9"/>\nwhat changed?\n\n'
                    '<system-reminder kind="memory_context">\na recalled fact\n</system-reminder>'}
    assert "_parts" not in repr(kwargs) and "sender_kind" not in repr(kwargs)


def test_the_max_iterations_summary_renders_typed_rows(chat_agent):
    from agent.chat_completion_helpers import _iteration_summary_api_messages

    out = _iteration_summary_api_messages(chat_agent, [_human("q", message_id="3")])
    assert out[-1]["content"] == '<origin message_id="3"/>\nq' and "_parts" not in out[-1]


@pytest.mark.parametrize("summary", [False, True])
def test_typed_replay_ignores_legacy_sidecar_without_losing_event_identity(chat_agent, summary, caplog):
    """Migrated rows retain API logs; those logs must not override their typed inputs."""
    import copy
    from agent.chat_completion_helpers import _iteration_summary_api_messages

    event = mp.system_event_message("process_complete", "job finished", sender_id="system:process_complete")
    event["api_content"] = "old flattened event"
    human = _human("next task", message_id="9")
    mp.append_parts(human, [mp.turn_injection("memory_context", "retained context")])
    human["api_content"] = "old flattened human"
    messages = [event, {"role": "assistant", "content": "seen"}, human,
                {"role": "assistant", "content": "ready"},
                {"role": "user", "content": "legacy", "api_content": "legacy compatibility"},
                {"role": "assistant", "content": "ok"}, _human("continue")]
    original = copy.deepcopy(messages)
    out = (_iteration_summary_api_messages(chat_agent, messages) if summary
           else _wire(chat_agent, messages, len(messages) - 1)["messages"])
    text = "\n".join(m.get("content", "") for m in out)
    assert '<system-reminder kind="process_complete">\njob finished\n</system-reminder>' in text
    assert '<origin message_id="9"/>\nnext task' in text
    assert '<system-reminder kind="memory_context">\nretained context\n</system-reminder>' in text
    assert "old flattened" not in text
    assert "legacy compatibility" in text
    assert messages == original
    assert "typed row content diverged" not in caplog.text


def test_token_estimate_ignores_typed_rows_legacy_api_log():
    from agent.context_compressor import _estimate_msg_budget_tokens
    from agent.model_metadata import estimate_messages_tokens_rough

    message = _human("current text")
    baseline = estimate_messages_tokens_rough([message])
    budget = _estimate_msg_budget_tokens(message)
    message["api_content"] = "historical rendered log " * 1000
    assert estimate_messages_tokens_rough([message]) == baseline
    assert _estimate_msg_budget_tokens(message) == budget


def test_moa_advisors_read_the_rendered_conversation_and_their_context_is_an_injection():
    from agent.turn_request_assembly import _append_moa_context

    seen = {}

    def fake_aggregate(**kw):
        seen["messages"] = kw["api_messages"]
        return "advisor view"

    clone = _human("q", message_id="3")
    with patch("agent.moa_loop.aggregate_moa_context", side_effect=fake_aggregate):
        _append_moa_context(SimpleNamespace(provider="x", api_mode="chat_completions"), [clone],
                            {"reference_models": []}, "q")
    assert seen["messages"][0]["content"] == '<origin message_id="3"/>\nq'
    assert clone["content"] == "q" and clone["_parts"][-1] == mp.turn_injection("moa_guidance", "advisor view")
