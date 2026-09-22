"""Admission and handoff share one turn boundary, including cached-agent reuse."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.steering import begin_input_turn, drain_inputs, finish_input_turn
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from run_agent import AIAgent


def bare_agent():
    agent = object.__new__(AIAgent)
    agent._pending_steer_lock = threading.Lock()
    agent._pending_steer = None
    agent._input_turn_open = False
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user", "task"])
async def test_final_handoff_rejects_late_input_into_gateway_queue(monkeypatch, tmp_path, kind):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    runner._busy_input_mode = "steer"
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._send_busy_ack_reply = AsyncMock()
    runner._busy_steer_ack_enabled = lambda *args: False
    adapter = SimpleNamespace(_pending_messages={}, _internal_session_tasks={"s": object()})
    adapter.get_pending_message = lambda key: adapter._pending_messages.pop(key, None)
    runner._delivery_adapter_for = lambda source: adapter
    agent = bare_agent()
    runner._session_state("s").turn.agent = agent
    source = SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="u", chat_type="dm")
    begin_input_turn(agent)
    assert agent.steer_input_events([{"kind": kind, "text": "before close"}])
    result = {"final_response": "finished"}
    finish_input_turn(agent, result)
    late = MessageEvent(text="after close", source=source, message_id="late",
                        internal=kind == "task", task_feedback=kind == "task")
    assert await runner._handle_active_session_busy_message(late, "s")
    assert drain_inputs(agent) == []
    pending, text = await runner._run_agent_drain_pending(result, adapter, source, "s")
    assert text
    records = pending.metadata["input_events"]
    assert sorted((item["kind"], item["text"]) for item in records) == [
        (kind, "after close"), (kind, "before close")]
    assert not result.get("pending_input_events")
    assert not agent.steer("legacy late steer")
    begin_input_turn(agent)
    assert agent.steer_input_events([{"kind": kind, "text": "next turn"}])
    next_result = {}
    finish_input_turn(agent, next_result)
    assert next_result["pending_input_events"] == [{"kind": kind, "text": "next turn"}]


@pytest.mark.parametrize("early_failure", [False, True])
def test_loop_wrapper_closes_early_exits_and_preserves_accepted_inputs(monkeypatch, early_failure):
    from agent import conversation_loop, turn_context
    agent = bare_agent()
    record = {"kind": "task", "text": "result during setup"}

    def run_turn(*args, **kwargs):
        assert agent.steer_input_events([record])
        if early_failure:
            raise RuntimeError("setup failed")
        return {"final_response": "early return", "messages": []}

    monkeypatch.setattr(conversation_loop, "_run_conversation_turn", run_turn)
    monkeypatch.setattr(conversation_loop, "_close_durable_failed_turn", lambda *args: None)
    monkeypatch.setattr(turn_context, "export_current_turn_boundary", lambda a, r, u: r)
    if early_failure:
        with pytest.raises(RuntimeError, match="setup failed"):
            conversation_loop.run_conversation(agent, "start")
        # A caller recovering the failure can still hand off already accepted input.
        result = {}
        finish_input_turn(agent, result)
    else:
        result = conversation_loop.run_conversation(agent, "start")
    assert result["pending_input_events"] == [record]
    assert not agent.steer_input_events([{"kind": "task", "text": "too late"}])


@pytest.mark.asyncio
async def test_explicit_steer_after_close_queues_payload():
    from tests.gateway.test_steer_command import _make_event, _make_runner, _make_source, _session_entry
    from gateway.session import build_session_key
    runner, adapter = _make_runner(_session_entry())
    source = _make_source()
    key = build_session_key(source)
    agent = bare_agent()
    runner._session_state(key).turn.agent = agent
    reply = await runner._busy_steer_command(_make_event("/steer late correction"), key, source)
    assert "queued" in reply
    assert adapter._pending_messages[key].text == "late correction"
    assert drain_inputs(agent) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["setup", "finalizing", "presentation"])
async def test_failed_worker_requeues_accepted_input_before_adapter_cleanup(monkeypatch, tmp_path, failure_stage):
    from agent import conversation_loop, turn_context
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    runner = GatewayRunner(config=GatewayConfig())
    runner._busy_input_mode = "steer"
    adapter = SimpleNamespace(_pending_messages={})
    runner._delivery_adapter_for = lambda source: adapter
    agent = bare_agent()
    source = SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="u", chat_type="dm")
    ctx = SimpleNamespace(agent_holder=[agent], session_key="s", session_id="test-session",
                          run_generation=None, source=source, result_holder=[None])

    def failed_turn(*args, **kwargs):
        assert agent.steer_input_events([{"kind": "task", "text": "accepted result"}])
        assert agent.steer("accepted correction")
        if failure_stage == "presentation":
            return {"final_response": "finished", "messages": []}
        if failure_stage == "finalizing":
            finish_input_turn(agent)  # finalizer closes before a failing memory sync
        raise RuntimeError("worker failed")

    monkeypatch.setattr(conversation_loop, "_run_conversation_turn", failed_turn)
    monkeypatch.setattr(conversation_loop, "_close_durable_failed_turn", lambda *args: None)
    monkeypatch.setattr(turn_context, "export_current_turn_boundary", lambda a, r, u: r)

    def run_sync():
        ctx.result_holder[0] = conversation_loop.run_conversation(agent, "start")
        raise RuntimeError("worker failed")

    worker = runner._run_agent_start_turn_worker(ctx, run_sync)
    with pytest.raises(RuntimeError, match="worker failed"):
        await runner._run_agent_await_turn_worker(worker, ctx, asyncio.Event(), None)
    waiting = [adapter._pending_messages["s"], *runner._overflow_queue("s")]
    assert [(e.internal, e.text) for e in waiting] == [
        (False, "accepted correction"), (True, "accepted result")]
    assert drain_inputs(agent) == []
    assert not agent.steer_input_events([{"kind": "task", "text": "too late"}])
