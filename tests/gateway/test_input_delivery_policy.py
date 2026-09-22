"""User/task steering, queued internal_turns, and lossless ordered batches."""
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.interrupt_control import InterruptControlMixin
from agent.steering import append_pending_inputs, drain_inputs
from gateway.config import GatewayConfig, Platform
from gateway.input_batch import coalesce_pending_inputs, restore_leftover_inputs
from gateway.input_policy import input_record, priority, task_feedback_event
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class Agent(InterruptControlMixin):
    def __init__(self):
        self._pending_steer_lock = threading.Lock()
        self._pending_steer = None
        self._active_children = []


def event(text, kind="user"):
    return MessageEvent(
        text=text, message_type=MessageType.TEXT, message_id=text,
        source=SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="u", chat_type="dm"),
        internal=kind != "user", task_feedback=kind == "task",
        metadata={"internal_notification": True} if kind == "external" else {},
    )


def setup_runner(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    runner._busy_input_mode = "steer"
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._send_busy_ack_reply = AsyncMock()
    runner._busy_steer_ack_enabled = lambda *args: False
    adapter = SimpleNamespace(_pending_messages={}, _internal_session_tasks={"s": object()})
    runner._adapter_for_source = lambda source: adapter
    agent = Agent()
    runner._session_state("s").turn.agent = agent
    return runner, adapter, agent


@pytest.mark.asyncio
async def test_internal_run_accepts_user_and_task_steers_but_queues_external(monkeypatch, tmp_path):
    runner, adapter, agent = setup_runner(monkeypatch, tmp_path)
    child = Agent()
    agent._active_children = [child]
    for item in [event("result", "task"), event("correction"), event("email", "external")]:
        assert await runner._handle_active_session_busy_message(item, "s")
    rows = [{"role": "assistant", "tool_calls": []}, {"role": "tool", "content": "unchanged"}]
    assert append_pending_inputs(agent, rows)
    text = rows[-1]["content"]
    assert text.index("correction") < text.index("[/OUT-OF-BAND USER MESSAGE]") < text.index("[TASK RESULT")
    assert text.count("For reactions to these messages") == 1
    assert text.count("[Runtime reminder]") == 1
    assert rows[-2]["content"] == "unchanged"
    assert adapter._pending_messages["s"].text == "email"
    assert drain_inputs(child) == []
    assert child._drain_pending_steer() is None


@pytest.mark.asyncio
async def test_queue_mode_still_defers_task_feedback(monkeypatch, tmp_path):
    runner, adapter, agent = setup_runner(monkeypatch, tmp_path)
    runner._busy_input_mode = "queue"
    task = event("result", "task")
    assert await runner._handle_active_session_busy_message(task, "s")
    assert drain_inputs(agent) == []
    assert adapter._pending_messages["s"] is task


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["async_delegation", "completion", "watch_match"])
async def test_completion_producer_to_native_agent_admission(monkeypatch, tmp_path, kind):
    from run_agent import AIAgent
    runner, adapter, _ = setup_runner(monkeypatch, tmp_path)
    agent = object.__new__(AIAgent)
    agent._pending_steer_lock = threading.Lock()
    agent._pending_steer = None
    runner._session_state("s").turn.agent = agent
    source = event("route").source
    runner._build_process_event_source = lambda payload: source
    runner._resolve_injection_adapter = lambda *args: adapter

    async def deliver(item):
        await runner._handle_active_session_busy_message(item, "s")

    adapter.handle_message = deliver
    assert await runner._inject_watch_notification("worker result", {
        "type": kind, "session_key": "agent:main:discord:dm:c",
        "parent_session_id": "session", "delegation_id": "deleg", "session_id": "proc",
    }) is True
    messages = [{"role": "tool", "content": "existing result"}]
    assert append_pending_inputs(agent, messages)
    record = messages[-1]["display_metadata"]["input_events"][0]
    assert record["kind"] == "task" and record["origin"]["type"] == kind
    assert "OUT-OF-BAND USER MESSAGE" not in messages[-1]["content"]
    assert not adapter._pending_messages


def test_pending_input_never_rewrites_an_earlier_request():
    agent = Agent()
    agent.steer_input_events([input_record(event("new"))])
    rows = [{"role": "tool", "content": "old"}, {"role": "assistant", "content": "published"}]
    before = list(rows)
    assert not append_pending_inputs(agent, rows)
    assert rows == before
    assert drain_inputs(agent)[0]["text"] == "new"


def test_explicit_stop_discards_typed_inputs():
    agent = Agent()
    agent._execution_thread_id = None
    agent.steer_input_events([input_record(event("correction")), input_record(event("result", "task"))])
    agent.clear_interrupt(hard_cancel=True)
    assert drain_inputs(agent) == []


def test_fifo_prioritizes_and_coalesces_user_then_task_without_external(monkeypatch, tmp_path):
    runner, adapter, _ = setup_runner(monkeypatch, tmp_path)
    for item in [event("email", "external"), event("result", "task"), event("first"), event("second")]:
        runner._enqueue_fifo("s", item, adapter)
    first = adapter._pending_messages.pop("s")
    first = runner._promote_queued_event("s", adapter, first)
    batch = coalesce_pending_inputs(runner, adapter, "s", first)
    records = batch.metadata["input_events"]
    assert [e["text"] for e in records] == ["first", "second", "result"]
    assert batch.internal is False and batch.message_id == "second"
    assert adapter._pending_messages["s"].text == "email"


def test_leftover_task_is_not_relabelled_as_user(monkeypatch, tmp_path):
    runner, adapter, _ = setup_runner(monkeypatch, tmp_path)
    result = {"pending_input_events": [input_record(event("done", "task"))]}
    restore_leftover_inputs(runner, result, event("x").source, "s")
    pending = adapter._pending_messages.pop("s")
    batch = coalesce_pending_inputs(runner, adapter, "s", pending)
    assert batch.internal and priority(batch) == 1
    assert "OUT-OF-BAND USER MESSAGE" not in batch.text
    assert "Runtime reminder" not in batch.text


def test_late_handoff_preserves_user_arrival_order(monkeypatch, tmp_path):
    from datetime import timedelta
    runner, adapter, _ = setup_runner(monkeypatch, tmp_path)
    older, newer = event("older"), event("newer")
    newer.timestamp = older.timestamp + timedelta(seconds=1)
    runner._enqueue_fifo("s", newer, adapter)
    restore_leftover_inputs(runner, {"pending_input_events": [input_record(older)]}, older.source, "s")
    first = adapter._pending_messages.pop("s")
    batch = coalesce_pending_inputs(runner, adapter, "s", first)
    assert [e["text"] for e in batch.metadata["input_events"]] == ["older", "newer"]


def test_steer_budget_keeps_excess_tasks_for_next_boundary():
    agent = Agent()
    agent.steer_input_events([input_record(event("x" * 24001, "task")), input_record(event("user"))])
    rows = [{"role": "tool", "content": "done"}]
    assert append_pending_inputs(agent, rows)
    assert [r["kind"] for r in rows[-1]["display_metadata"]["input_events"]] == ["user"]
    assert len(drain_inputs(agent)[0]["text"]) == 24001


def test_internal_notification_cannot_promote_itself_with_text_or_task_flag():
    notification = event("IMPORTANT task completion", "external")
    notification.task_feedback = True
    assert priority(notification) == 2
    assert not task_feedback_event({"type": "webhook", "task_feedback": True})
    assert task_feedback_event({"type": "async_delegation"})
    assert not task_feedback_event({"type": "watch_match"})
    assert task_feedback_event({"type": "watch_match", "parent_session_id": "s", "session_key": "agent:main:discord:dm:c"})


def test_real_watch_producer_preserves_owner_for_delivery_policy():
    from tools.process_registry import ProcessRegistry, ProcessSession
    session = ProcessSession(id="process", command="server", task_id="t", session_key="agent:main:discord:dm:c")
    session.parent_session_id = "session"
    produced = {**ProcessRegistry._watch_event_base(session), "type": "watch_match"}
    assert task_feedback_event(produced)
    session.parent_session_id = ""
    assert not task_feedback_event({**ProcessRegistry._watch_event_base(session), "type": "watch_match"})


def test_task_budget_never_drops_pending_results(monkeypatch, tmp_path):
    runner, adapter, _ = setup_runner(monkeypatch, tmp_path)
    for item in [event("first"), event("x" * 24001, "task"), event("second")]:
        runner._enqueue_fifo("s", item, adapter)
    first = adapter._pending_messages.pop("s")
    batch = coalesce_pending_inputs(runner, adapter, "s", first)
    assert [e["text"] for e in batch.metadata["input_events"]] == ["first", "second"]
    assert len(adapter._pending_messages["s"].text) == 24001


def test_user_steer_restores_presentation_from_muted_notification():
    from agent.notification_presentation import notification_turn, event_presentation_muted
    agent = Agent()
    callback = lambda text: None
    agent.stream_delta_callback = callback
    with notification_turn(agent, muted=True, session_id="s"):
        assert event_presentation_muted("message.complete", "s")
        agent.steer_input_events([input_record(event("please answer"))])
        assert append_pending_inputs(agent, [{"role": "tool", "content": "done"}])
        assert not event_presentation_muted("message.complete", "s")
        assert agent.stream_delta_callback is callback
        assert not agent._mute_notification_reply
    assert agent.stream_delta_callback is callback


def test_batch_provenance_survives_sqlite_roundtrip(tmp_path):
    from agent.session_persistence import _db_flush_collect
    from hermes_state import SessionDB
    agent = Agent()
    agent.session_id = "s"
    agent._last_flushed_db_idx = 0
    agent.steer_input_events([input_record(event("result", "task")), input_record(event("preference"))])
    messages = [{"role": "tool", "content": "done"}]
    assert append_pending_inputs(agent, messages)
    rows, _ = _db_flush_collect(agent, [messages[-1]], [])
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s", source="discord")
        db.append_messages_batch("s", rows)
        restored = db.get_messages("s")[-1]
    records = restored["display_metadata"]["input_events"]
    assert [r["kind"] for r in records] == ["user", "task"]
    assert restored["content"] == messages[-1]["content"]


def test_text_after_media_does_not_overtake_it(monkeypatch, tmp_path):
    runner, adapter, _ = setup_runner(monkeypatch, tmp_path)
    photo = event("photo")
    photo.message_type = MessageType.PHOTO
    photo.media_urls = ["attachment.png"]
    for item in [event("first"), photo, event("second"), event("task", "task")]:
        runner._enqueue_fifo("s", item, adapter)
    first = adapter._pending_messages.pop("s")
    batch = coalesce_pending_inputs(runner, adapter, "s", first)
    assert batch.text == "first"
    assert [e.text for e in runner._overflow_queue("s")] == ["photo", "second", "task"]
