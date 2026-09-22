"""A capped queued-turn chain must reconcile streaming before returning its final.

Live Discord incident: the stream finalized a reply, then the depth-limit exit
returned an unmarked result and the outer handler sent the same text again.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from gateway.turn_context import TurnContext
from tests.gateway.test_stream_final_contract import _make_draft_adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["streamed", "unstreamed", "failed"])
@pytest.mark.parametrize("pending_kind", ["event", "text"])
async def test_depth_limit_preserves_final_delivery(delivery, pending_kind):
    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.DISCORD, chat_id="chat", chat_type="dm", user_id="user")
    session_key = "agent:main:discord:dm:chat"
    adapter = _make_draft_adapter()
    adapter.draft_stream_is_message = False
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter.queue_message = Mock()
    runner._MAX_INTERRUPT_DEPTH = 3
    runner._draining = False
    runner._delivery_adapter_for = lambda source: adapter
    runner._delivery_adapter_for = lambda source: adapter
    runner._get_proxy_url = lambda: None
    runner._release_running_agent_state = Mock()
    runner._should_send_voice_reply = lambda *args, **kwargs: False
    runner._deliver_media_from_response = AsyncMock()

    response = {"final_response": "The completed answer.", "messages": [], "completed": True}
    if delivery == "failed":
        response.update(failed=True, final_response="The run failed after its preview.")
    consumer = None
    if delivery != "unstreamed":
        consumer = GatewayStreamConsumer(
            adapter, source.chat_id,
            StreamConsumerConfig(transport="edit", cursor="", buffer_threshold=1),
        )
        consumer.on_delta("The completed answer.")
        consumer.finish("The completed answer.")

    ctx = TurnContext(
        source=source, session_key=session_key, session_id="session", history=[],
        _interrupt_depth=runner._MAX_INTERRUPT_DEPTH, result_holder=[response],
        stream_consumer_holder=[consumer],
    )
    runner._run_agent_display_settings = lambda source: SimpleNamespace(
        _native_slack_task_cards=False, needs_progress_queue=False, log_mode_enabled=False,
    )
    runner._run_agent_build_turn_context = lambda *args, **kwargs: (
        ctx, SimpleNamespace(run_sync=lambda: None), adapter,
    )
    runner._run_agent_bind_turn_wiring = lambda *args: None
    runner._run_agent_start_streaming_tts = lambda *args: None
    runner._run_agent_track_agent = AsyncMock()
    runner._run_agent_monitor_for_interrupt = AsyncMock()
    runner._run_agent_notify_long_running = AsyncMock()
    runner._run_agent_start_turn_worker = lambda *args: SimpleNamespace(executor_task=None)
    runner._run_agent_await_turn_worker = AsyncMock(return_value=response)
    runner._run_agent_evict_on_fallback = lambda *args: None
    runner._run_agent_schedule_bubble_cleanup = Mock()
    pending = MessageEvent(text="One more correction", source=source, message_type=MessageType.TEXT)
    runner._run_agent_drain_pending = AsyncMock(return_value=(
        pending if pending_kind == "event" else None, pending.text,
    ))

    # Real turn orchestration, depth-limit branch, consumer flush, and delivery
    # reconciliation. Only model execution and unrelated status wiring are stubbed.
    result = await runner._run_agent_inner(
        message="Original request", context_prompt="", history=[], source=source,
        session_id="session", session_key=session_key, _interrupt_depth=runner._MAX_INTERRUPT_DEPTH,
    )
    outward = await runner._hmwa_deliver_turn_response(
        source=source, session_key=session_key, run_generation=None, event=pending,
        response=result["final_response"], agent_messages=[], agent_result=result, _footer_line=None,
        session_entry=None, _intentional_silence=False,
    )
    if delivery == "streamed":
        assert consumer.final_content_delivered
        assert consumer.delivered_final_matches(result["final_response"]) is True
        assert outward is None, "the outer adapter must not send the streamed answer again"
        assert len(adapter.send_calls) == 1
    else:
        assert outward == result["final_response"], "undelivered text and errors still need delivery"
        assert not result.get("already_sent")
    if pending_kind == "event":
        assert adapter._pending_messages[session_key] is pending
    else:
        adapter.queue_message.assert_called_once_with(session_key, pending.text)
