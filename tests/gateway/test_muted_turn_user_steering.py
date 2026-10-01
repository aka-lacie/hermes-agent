"""A diagnostic wake becomes conversational only when it consumes user input."""
import asyncio
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from agent.interrupt_control import InterruptControlMixin
from agent.notification_presentation import event_presentation_muted
from agent.steering import append_pending_inputs
from gateway.config import GatewayConfig, Platform, StreamingConfig
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.stream_consumer import StreamConsumerConfig
from gateway.turn_context import TurnContext
from tests.gateway.test_warning_notifications import RecordingAdapter


class SteeredAgent(InterruptControlMixin):
    def __init__(self, kind):
        self._pending_steer_lock = threading.Lock()
        self._pending_steer = None
        self.kind = kind

    def run_conversation(self, message, **kwargs):
        # These callbacks were constructed by real gateway wiring, then suppressed
        # by the actual conversation scope, rather than supplied directly by a test.
        assert self.stream_delta_callback is None
        assert self.interim_assistant_callback is None
        assert self.tool_progress_callback is None
        assert event_presentation_muted("message.complete", "session")
        self.steer_input_events([{"kind": self.kind, "text": "new input"}])
        messages = [{"role": "tool", "content": "tool finished"}]
        assert append_pending_inputs(self, messages)
        if self.kind == "user":
            assert not event_presentation_muted("message.complete", "session")
            assert callable(self.stream_delta_callback)
            assert callable(self.interim_assistant_callback)
            assert callable(self.tool_progress_callback)
            self.interim_assistant_callback("I saw your message.")
            self.stream_delta_callback("Answer to the user")
        else:
            assert self.stream_delta_callback is None
            assert event_presentation_muted("message.complete", "session")
        return {"final_response": "Answer to the user" if self.kind == "user" else "Hidden diagnostic",
                "received_user_input": self.kind == "user", "messages": messages, "completed": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user", "task"])
async def test_real_wiring_resumes_user_stream_but_not_task_diagnostic(monkeypatch, tmp_path, kind):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    gateway = GatewayRunner(config=GatewayConfig())
    gateway.config.streaming = StreamingConfig(enabled=True)
    adapter = RecordingAdapter()
    gateway._delivery_adapter_for = lambda source: adapter
    gateway._build_stream_consumer_config = lambda *a, **k: (
        StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=""), None)
    gateway._consume_pending_native_image_paths = lambda key: []
    gateway._consume_pending_turn_sidecar_notes = lambda key: []
    gateway._resolve_session_agent_runtime = lambda **kw: ("fake", {})
    gateway._resolve_session_reasoning_config = lambda **kw: None
    gateway._resolve_session_service_tier = lambda **kw: None
    gateway._resolve_turn_agent_config = lambda *a: {}
    gateway._service_tier = None
    gateway._provider_routing = None
    ctx = TurnContext(source=SessionSource(platform=Platform.DISCORD, chat_id="offline", chat_type="dm"),
                      user_config={}, mute_notification_reply=True, session_id="session", session_key="key",
                      message="Diagnostic wake", _hooks_ref=NS(loaded_hooks=[]),
                      _run_still_current=lambda: True, resolve_display_setting=lambda *a: None,
                      interim_assistant_messages_enabled=True, progress_callback=lambda *a: None)
    turn = TurnRunner(gateway, ctx)
    agent = SteeredAgent(kind)
    turn._combined_ephemeral_prompt = lambda: ""
    turn._resolve_turn_agent = lambda *a: (agent, False)
    turn._load_turn_history = lambda *a: ([], None, [])
    turn._prepare_turn_message = lambda *a: (None, None)
    turn._sync_session_after_run = lambda *a: (False, "session", 0)
    turn._append_auto_media_tags = lambda response, *a: response
    turn._attach_session_title_callback = lambda *a: None
    turn._make_bg_review_callbacks = lambda: (lambda *a: None, lambda: None)
    stream_task = asyncio.create_task(gateway._run_agent_stream_consumer_task(ctx.stream_consumer_holder, ctx))
    result = await asyncio.to_thread(turn.run_sync)
    await asyncio.wait_for(stream_task, 2)
    assert result["received_user_input"] is (kind == "user")
    assert ctx.mute_notification_reply is (kind != "user")
    texts = [entry[1] for entry in adapter.sent]
    if kind == "user":
        assert "I saw your message." in texts
        assert any("Answer to the user" in text for text in texts)
    else:
        assert texts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("final, visible, expected", [
    ("Answer to user", False, "Answer to user"),
    ("NO_REPLY", False, "warning"),
    ("NO_REPLY", True, None),
])
async def test_user_answer_before_successor_obeys_user_silence_policy(monkeypatch, tmp_path, final, visible, expected):
    from gateway.run_turn import _unexpected_silence_reply

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    gateway = GatewayRunner(config=GatewayConfig())
    gateway._deliver_queued_first_response = AsyncMock()
    gateway._pop_post_delivery_callback = lambda *a: None
    ctx = TurnContext(source=SessionSource(platform=Platform.DISCORD, chat_id="offline"),
                      mute_notification_reply=True, persist_user_display_kind="internal_notification")
    response = {"final_response": final, "received_user_input": True, "user_visible_response": visible}
    await gateway._run_agent_deliver_first_response(ctx, NS(), response, response, None)
    if expected is None:
        gateway._deliver_queued_first_response.assert_not_awaited()
    else:
        gateway._deliver_queued_first_response.assert_awaited_once()
        assert gateway._deliver_queued_first_response.call_args.args[0] == (
            _unexpected_silence_reply() if expected == "warning" else expected)


@pytest.mark.asyncio
async def test_muted_native_transport_does_not_start_before_user_resume():
    ctx = TurnContext(mute_notification_reply=True)
    started = asyncio.Event()
    ctx.stream_consumer_holder[0] = NS(run=AsyncMock(side_effect=started.set))
    gateway = object.__new__(GatewayRunner)
    task = asyncio.create_task(gateway._run_agent_stream_consumer_task(ctx.stream_consumer_holder, ctx))
    # Let its first readiness check run while mute is still in effect.
    await asyncio.sleep(0)
    assert not started.is_set()
    ctx.mute_notification_reply = False
    await asyncio.wait_for(task, 1)
    assert started.is_set()
