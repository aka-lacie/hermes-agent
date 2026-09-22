"""Repeated batching preserves source items through requeue, splitting and handoff."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.input_batch import coalesce_pending_inputs, restore_leftover_inputs
from gateway.input_policy import input_record
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def event(text, kind="user", index=0):
    return MessageEvent(
        text=text, message_id=f"message-{index}",
        source=SessionSource(platform=Platform.DISCORD, chat_id="channel", user_id="user", chat_type="dm"),
        internal=kind != "user", task_feedback=kind == "task",
        timestamp=datetime(2026, 1, 1) + timedelta(seconds=index),
    )


@pytest.fixture
def queue(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    runner._busy_input_mode = "steer"
    adapter = SimpleNamespace(_pending_messages={})
    runner._adapter_for_source = lambda source: adapter
    return runner, adapter


def take(queue):
    runner, adapter = queue
    first = adapter._pending_messages.pop("s", None)
    first = runner._promote_queued_event("s", adapter, first)
    return coalesce_pending_inputs(runner, adapter, "s", first)


def records(batch):
    return batch.metadata["input_events"]


def test_rebatch_retains_original_records_and_anchor(queue):
    runner, adapter = queue
    user, task, newer = event("first user", index=1), event("worker conjecture", "task", 2), event("second user", index=3)
    runner._enqueue_fifo("s", task, adapter)
    batch = coalesce_pending_inputs(runner, adapter, "s", user)
    original = records(batch)
    runner._enqueue_fifo("s", newer, adapter)
    rebatch = coalesce_pending_inputs(runner, adapter, "s", batch)
    assert records(rebatch) == [original[0], input_record(newer, origin=runner._steer_origin(newer)), original[1]]
    assert rebatch.message_id == newer.message_id
    assert "input_record" not in rebatch.metadata
    assert rebatch.text.count("worker conjecture") == 1
    assert rebatch.text.index("[/OUT-OF-BAND USER MESSAGE]") < rebatch.text.index("worker conjecture")
    assert coalesce_pending_inputs(runner, adapter, "s", rebatch).metadata == rebatch.metadata


def test_rebatch_leftovers_does_not_drop_sibling(queue):
    runner, adapter = queue
    old, newer = event("old task", "task", 1), event("newer task", "task", 2)
    original = [input_record(old), input_record(newer)]
    restore_leftover_inputs(runner, {"pending_input_events": original}, old.source, "s")
    batch = take(queue)
    assert records(batch) == original
    batch.metadata["input_record"] = original[0]  # stale anchor from older queued batches
    rebatch = coalesce_pending_inputs(runner, adapter, "s", batch)
    assert records(rebatch) == original
    assert "input_record" not in rebatch.metadata
    assert rebatch.internal and rebatch.task_feedback


def test_budget_splits_requeued_tasks_after_user_without_loss(queue):
    runner, adapter = queue
    task1, task2 = event("x" * 14000, "task", 1), event("y" * 14000, "task", 2)
    original = [input_record(task1), input_record(task2)]
    batch = event("rendered task envelope", "task", 1)
    batch.metadata["input_events"] = original
    runner._enqueue_fifo("s", batch, adapter)
    user = event("latest user", index=3)
    runner._enqueue_fifo("s", user, adapter)
    first = take(queue)
    assert [record["event_id"] for record in records(first)] == [user.message_id, task1.message_id]
    second = take(queue)
    assert records(second) == [original[1]]
    assert second.message_id == task2.message_id
    assert take(queue) is None
    # Another finalizer handoff/render cycle preserves exact records, without envelopes.
    restore_leftover_inputs(runner, {"pending_input_events": records(first)}, user.source, "s")
    replay = take(queue)
    assert records(replay) == records(first)
    assert take(queue) is None


def test_oversized_task_waits_for_user_then_progresses_alone(queue):
    runner, adapter = queue
    task, user = event("x" * 24001, "task", 1), event("new request", index=2)
    batch = event("old envelope", "task", 1)
    batch.metadata["input_events"] = [input_record(task)]
    runner._enqueue_fifo("s", user, adapter)
    first = coalesce_pending_inputs(runner, adapter, "s", batch)
    assert [record["text"] for record in records(first)] == [user.text]
    assert records(take(queue)) == [input_record(task)]
    assert take(queue) is None


def test_task_prefix_order_does_not_skip_over_budget_sibling(queue):
    runner, adapter = queue
    tasks = [event("a" * 14000, "task", 1), event("b" * 14000, "task", 2), event("small", "task", 3)]
    batch = event("envelope", "task", 1)
    batch.metadata["input_events"] = [input_record(item) for item in tasks]
    first = coalesce_pending_inputs(runner, adapter, "s", batch)
    assert records(first) == [input_record(tasks[0])]
    assert records(take(queue)) == [input_record(item) for item in tasks[1:]]


def test_rebatch_respects_media_boundary_between_user_records(queue):
    runner, adapter = queue
    early, later, task = event("early", index=1), event("later", index=3), event("task", "task", 4)
    batch = event("old envelope", index=3)
    batch.metadata["input_events"] = [input_record(early), input_record(later), input_record(task)]
    photo = event("photo", index=2)
    photo.message_type = MessageType.PHOTO
    photo.media_urls = ["photo.png"]
    runner._enqueue_fifo("s", photo, adapter)
    first = coalesce_pending_inputs(runner, adapter, "s", batch)
    assert records(first) == [input_record(early)]
    assert take(queue) is photo
    assert records(take(queue)) == [input_record(later), input_record(task)]
    assert take(queue) is None


def test_queue_mode_does_not_absorb_other_pending_turns(queue):
    runner, adapter = queue
    runner._busy_input_mode = "queue"
    task = event("task", "task", 1)
    batch = event("old envelope", "task", 1)
    batch.metadata["input_events"] = [input_record(task)]
    user = event("separate turn", index=2)
    runner._enqueue_fifo("s", user, adapter)
    assert records(coalesce_pending_inputs(runner, adapter, "s", batch)) == [input_record(task)]
    assert take(queue) is user


def test_transport_id_retained_when_model_origin_is_redacted(queue):
    runner, adapter = queue
    user = event("user", index=1)
    record = input_record(user, origin={"message_id": "redacted-id"})
    batch = event("envelope", index=10)
    batch.metadata["input_events"] = [record]
    rebatch = coalesce_pending_inputs(runner, adapter, "s", batch)
    assert rebatch.message_id == user.message_id
    assert rebatch.source.message_id == user.message_id
    assert records(rebatch) == [record]


@pytest.mark.asyncio
@pytest.mark.parametrize("leftover_kind", ["external", "oversized_task", "media"])
async def test_real_depth_cap_requeues_without_merging_pending_sibling(queue, leftover_kind):
    runner, adapter = queue
    adapter.get_pending_message = lambda key: adapter._pending_messages.pop(key, None)
    first = event("first user", index=1)
    if leftover_kind == "external":
        items = [first, event("task result", "task", 2), event("webhook", "external", 3)]
        items[-1].metadata["internal_notification"] = True
    elif leftover_kind == "oversized_task":
        items = [first, event("x" * 24001, "task", 2)]
    else:
        photo = event("photo", index=2)
        photo.message_type = MessageType.PHOTO
        photo.media_urls = ["photo.png"]
        items = [first, photo, event("later user", index=3), event("task result", "task", 4)]
    for item in items:
        runner._enqueue_fifo("s", item, adapter)
    result = {"final_response": "already completed", "messages": []}
    pending_event, pending = await runner._run_agent_drain_pending(result, adapter, first.source, "s")
    assert adapter._pending_messages["s"] is not pending_event
    ctx = SimpleNamespace(
        source=first.source, session_id="session", session_key="s", run_generation=1,
        _interrupt_depth=runner._MAX_INTERRUPT_DEPTH, history=[], _status_thread_metadata=None,
        result_holder=[result],
    )
    assert await runner._run_agent_queued_followup(ctx, adapter, pending, pending_event, result, result, None) is result
    # Follow the same dequeue/coalesce path the next adapter turn will take. Every
    # admitted item survives exactly once, with its original type and media payload.
    delivered = []
    while (batch := take(queue)) is not None:
        if batch.metadata.get("input_events"):
            delivered.extend((record["event_id"], record["text"], record["kind"], [])
                             for record in records(batch))
        else:
            kind = "user" if not batch.internal else "task" if batch.task_feedback else "external"
            delivered.append((batch.message_id, batch.text, kind, batch.media_urls))
    expected = [(item.message_id, item.text,
                 "user" if not item.internal else "task" if item.task_feedback else "external", item.media_urls)
                for item in items]
    assert delivered == expected
