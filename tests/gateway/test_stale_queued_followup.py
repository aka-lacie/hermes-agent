"""Invalidated turns must not execute or persist their accepted late steers.

Exercise the real pending drain / follow-up boundary with a stand-in turn executor
that performs a tool side effect and writes an assistant row to a real SessionDB.
"""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, ProcessingOutcome
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from hermes_state import SessionDB
from tests.gateway.test_queued_followup_processing_hooks import (
    HookRecordingAdapter, _install_fake_agent, _make_runner,
)


KEY = "agent:main:telegram:dm:4242"
SID = "stale-followup"


def fixture_turn(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner._draining = False
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="4242", chat_type="dm")
    adapter = HookRecordingAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._intake_adapter_for = lambda source: adapter
    generation = runner._begin_session_run_generation(KEY)
    ctx = TurnContext(source=source, session_id=SID, session_key=KEY,
                      run_generation=generation, history=[], context_prompt="")
    runner._run_agent_deliver_first_response = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(side_effect=lambda **kw: kw["event"].text)
    runner._session_key_for_source = lambda source: KEY
    runner._persist_prompt_pins = AsyncMock()
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SID, source="test")
    tools = []

    async def execute(**kwargs):
        tools.append(kwargs["message"])
        db.append_message(SID, "assistant", content="follow-up executed")
        return {"final_response": "done", "messages": []}

    runner._run_agent = execute
    return runner, adapter, ctx, db, tools


def invalidate(runner, reason):
    runner._interrupt_running_turn(KEY, interrupt_reason="Operation interrupted.",
                                   invalidation_reason=reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["user_stop", "new_session", "reaped_session_eviction"])
@pytest.mark.parametrize("kind", ["steer", "internal", "admitted"])
async def test_stale_followup_never_executes_and_wakes_keep_fifo(tmp_path, reason, kind):
    runner, adapter, ctx, db, tools = fixture_turn(tmp_path)
    interrupt = asyncio.Event()
    interrupt.set()
    adapter._active_sessions[KEY] = interrupt
    events = [MessageEvent(text=f"wake-{i}", source=ctx.source, internal=True,
                           message_id=f"wake-{i}") for i in range(3)]
    for event in events:
        runner._enqueue_fifo(KEY, event, adapter)
    result = {"interrupted": True, "pending_steer": "use medium", "messages": []}
    # This is the hung turn's finalizer returning a steer accepted before /stop.
    pending_event, pending = await runner._run_agent_drain_pending(result, adapter, ctx.source, KEY)
    if kind != "steer":
        pending_event, pending = await runner._run_agent_drain_pending(
            {"interrupted": True}, adapter, ctx.source, KEY)
        if kind == "admitted":
            pending_event.internal = False
            pending_event._admitted_before_drain = True
    invalidate(runner, reason)
    await runner._run_agent_queued_followup(ctx, adapter, pending, pending_event, result, result, None)
    assert tools == []
    assert db.get_messages(SID) == []
    assert interrupt.is_set(), "stale follow-up cleared the replacement's interrupt"
    assert adapter.started == []
    assert adapter.completed == []
    remaining = [adapter._pending_messages[KEY], *(runner._overflow_queue(KEY) or [])]
    assert remaining == events
    assert all(actual is original for actual, original in zip(remaining, events))
    ctx.run_generation = runner._begin_session_run_generation(KEY)
    for expected in events:
        fresh = {"interrupted": True, "messages": []}
        event, text = await runner._run_agent_drain_pending(fresh, adapter, ctx.source, KEY, ctx.run_generation)
        assert event is expected
        await runner._run_agent_queued_followup(ctx, adapter, text, event, fresh, fresh, None)
    assert tools == [event.text for event in events]
    assert len(db.get_messages(SID)) == len(events)
    assert runner._queue_depth(KEY, adapter=adapter) == 0
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_text", [None, "replacement request"])
async def test_hung_turn_stop_discards_steer_without_losing_wakes(monkeypatch, tmp_path, interrupt_text):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    tools = []
    calls = []
    instances = []
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SID, source="test")

    class HungAgent:
        def __init__(self, **kwargs):
            self.tools = []
            self.pending_steer = None
            instances.append(self)

        def steer(self, text, **kwargs):
            self.pending_steer = text
            return True

        def hard_interrupt(self, *args, **kwargs):
            release.set()

        def run_conversation(self, message, **kwargs):
            calls.append(message)
            if message == "hung request":
                loop.call_soon_threadsafe(started.set)
                assert release.wait(10), "test did not interrupt the hung turn"
                return {"final_response": "", "messages": [], "interrupted": True,
                        "pending_steer": self.pending_steer, "interrupt_message": interrupt_text}
            tools.append(message)
            db.append_message(SID, "assistant", content="stale follow-up ran")
            return {"final_response": "done", "messages": []}

    _install_fake_agent(monkeypatch, tmp_path, HungAgent)
    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    generation = runner._begin_session_run_generation(KEY)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="4242", chat_type="dm")
    wakes = [MessageEvent(text=f"wake-{i}", source=source, internal=True) for i in range(3)]
    task = asyncio.create_task(runner._run_agent(
        message="hung request", context_prompt="", history=[], source=source,
        session_id=SID, session_key=KEY, run_generation=generation))
    try:
        await asyncio.wait_for(started.wait(), 10)
        runner._session_state(KEY).turn.agent = instances[0]
        assert runner._steer_for_event(instances[0], "use medium", MessageEvent(text="use medium", source=source))
        for wake in wakes:
            runner._enqueue_fifo(KEY, wake, adapter)
        invalidate(runner, "user_stop")
        await asyncio.wait_for(task, 10)
        assert calls == ["hung request"]
        assert tools == []
        assert db.get_messages(SID) == []
        # A stale finalizer must not materialize its steer behind interrupt text.
        assert adapter.get_pending_message(KEY) is wakes[0]
        assert runner._overflow_queue(KEY) == wakes[1:]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["steer", "internal"])
async def test_invalidation_during_followup_setup_blocks_execution(tmp_path, kind):
    runner, adapter, ctx, db, tools = fixture_turn(tmp_path)
    event = MessageEvent(text="wake", source=ctx.source, internal=True, message_id="wake") if kind == "internal" else None

    async def refresh(*args):
        invalidate(runner, "user_stop")

    runner._refresh_agent_cache_message_count = refresh
    result = {"interrupted": True, "messages": []}
    await runner._run_agent_queued_followup(ctx, adapter, "use medium", event, result, result, None)
    assert tools == []
    assert db.get_messages(SID) == []
    if event:
        assert adapter.get_pending_message(KEY) is event
        assert adapter.completed == [("wake", ProcessingOutcome.CANCELLED)]
    db.close()
