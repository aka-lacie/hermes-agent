"""Batch pending conversation input while leaving internal_turn notifications queued."""
from copy import copy, deepcopy
from datetime import datetime

from agent.steering import input_message
from gateway.input_policy import input_record, priority
from gateway.platforms.event import MessageEvent, MessageType


def input_metadata(event):
    from gateway.warning_notifications import diagnostic_metadata
    metadata = diagnostic_metadata(event)
    records = (getattr(event, "metadata", None) or {}).get("input_events")
    if records:
        metadata = {**metadata, "input_events": records}
    return metadata


def restore_leftover_inputs(runner, result, source, session_key):
    events = result.pop("pending_input_events", [])
    for record in events:
        origin = record.get("origin", {})
        event = MessageEvent(
            text=record["text"], source=source, message_type=MessageType.TEXT,
            message_id=record.get("event_id") or origin.get("message_id") or None,
            internal=record["kind"] == "task", task_feedback=record["kind"] == "task",
            allow_gateway_control=False,
        )
        event.metadata["input_record"] = record
        if origin.get("timestamp"):
            event.timestamp = datetime.fromisoformat(origin["timestamp"])
        runner._queue_or_replace_pending_event(session_key, event)
    if events:
        adapter = runner._delivery_adapter_for(source)
        slot = adapter._pending_messages.get(session_key) if adapter else None
        overflow = runner._overflow_queue(session_key)
        waiting = ([slot] if slot else []) + list(overflow or [])
        waiting.sort(key=lambda item: (priority(item), item.timestamp.timestamp()))
        if waiting:
            adapter._pending_messages[session_key] = waiting[0]
            if overflow is not None:
                overflow[:] = waiting[1:]


def _eligible(event):
    return (event is not None and priority(event) < 2 and event.text
            and event.message_type == MessageType.TEXT
            and not event.media_urls and not event.media_types and not event.is_command())


def _record_event(record, template):
    """Split a batch without inheriting its anchor's kind, ID or obsolete envelope."""
    event = copy(template)
    event.text = record["text"]
    event.internal = event.task_feedback = record["kind"] == "task"
    # event_id is transport-owned; model-facing origin IDs may have been redacted.
    event.message_id = record.get("event_id") or record.get("origin", {}).get("message_id") or None
    event.ledger_message_id = None
    event.source = copy(template.source)
    event.source.message_id = event.message_id
    event.metadata = {key: value for key, value in template.metadata.items()
                      if key not in {"input_record", "input_events"}}
    event.metadata["input_record"] = deepcopy(record)
    event.allow_gateway_control = False
    timestamp = record.get("origin", {}).get("timestamp")
    if timestamp:
        event.timestamp = datetime.fromisoformat(timestamp)
    return event


def _expand_event(event):
    records = event.metadata.get("input_events") if _eligible(event) else None
    return [_record_event(record, event) for record in records] if records else [event]


def _replace_pending(runner, adapter, session_key, remaining):
    adapter._pending_messages.pop(session_key, None)
    overflow = runner._overflow_queue(session_key)
    if overflow is not None:
        overflow.clear()
    for event in remaining:
        runner._enqueue_fifo(session_key, event, adapter)


def coalesce_pending_inputs(runner, adapter, session_key, first):
    if not _eligible(first):
        return first
    steer_mode = runner._effective_busy_input_mode(first.source) == "steer"
    typed_first = bool(first.metadata.get("input_record") or first.metadata.get("input_events"))
    if not steer_mode and not typed_first:
        return first
    slot = adapter._pending_messages.get(session_key)
    waiting = ([slot] if slot is not None else []) + list(runner._overflow_queue(session_key) or [])
    # Existing envelopes are containers, never user text. Expand before priority, media
    # boundaries and budgeting so a requeued mixed batch cannot conceal task siblings.
    candidates = [item for event in [first, *(waiting if steer_mode else [])]
                  for item in _expand_event(event)]
    if any(event.metadata.get("input_events") for event in [first, *waiting]):
        # An envelope's anchor timestamp hides earlier siblings. Only these envelopes
        # need chronological expansion; ordinary ingress keeps its established FIFO.
        candidates.sort(key=lambda item: (priority(item), item.timestamp.timestamp()))
    else:
        candidates.sort(key=priority)
    selected, remaining = [], []
    task_chars = 0
    blocked_by_media = blocked_by_budget = False
    has_users = any(priority(event) == 0 for event in candidates)
    for event in candidates:
        eligible = _eligible(event) and not blocked_by_media
        if priority(event) == 0 and not eligible:
            blocked_by_media = True  # preserve user order across voice/photo/document boundaries
        if eligible and priority(event) == 1:
            record = event.metadata.get("input_record")
            size = len(record["text"] if record else event.text)
            # A single oversized task can progress on its own, never ahead of user input.
            eligible = not blocked_by_budget and (
                task_chars + size <= 24000 or (not selected and not has_users))
            blocked_by_budget = not eligible
            if eligible:
                task_chars += size
        (selected if eligible else remaining).append(event)
    if not steer_mode:
        remaining.extend(waiting)  # queue mode renders only the already-formed first batch
    if not selected:
        # Flattening can reveal an earlier media boundary hidden by a batch's reply anchor.
        next_event = remaining.pop(0)
        _replace_pending(runner, adapter, session_key, remaining)
        return next_event
    if len(selected) == 1 and selected[0] is first and not typed_first:
        return first
    _replace_pending(runner, adapter, session_key, remaining)
    records = [deepcopy(event.metadata.get("input_record")) or input_record(
        event, origin=runner._steer_origin(event)) for event in selected]
    row = input_message(records)
    # The last user message is the reply anchor; task-only batches retain synthetic identity.
    users = [event for event in selected if priority(event) == 0]
    batch = copy(users[-1] if users else selected[0])
    batch.metadata = {key: value for key, value in batch.metadata.items() if key != "input_record"}
    batch.metadata["input_events"] = records
    batch.text = row["content"]
    batch.allow_gateway_control = False
    return batch
