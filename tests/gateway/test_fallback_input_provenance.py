"""Failure and non-persisting runtimes retain individual input provenance in SQLite."""
from unittest.mock import AsyncMock

import pytest

from agent.steering import input_message
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["exception", "normal"])
@pytest.mark.parametrize("mixed", [False, True])
async def test_fallback_persists_input_records_with_owner(tmp_path, fallback, mixed):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._session_db = None
    runner._hmwa_stop_typing_for_turn = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    source = SessionSource(platform=Platform.DISCORD, chat_id="offline", user_id="user")
    entry = store.get_or_create_session(source)
    db = store._db_for_session_id(entry.session_id)
    records = [{"kind": "task", "text": "Worker guessed a preference", "event_id": "task-id"}]
    if mixed:
        records.insert(0, {"kind": "user", "text": "I prefer the second option", "event_id": "user-id"})
    rendered = input_message(records)
    event = MessageEvent(text=rendered["content"], source=source, message_id="batch-id",
                         internal=not mixed, task_feedback=not mixed,
                         metadata={"input_events": records, "gateway_input_owner": "untrusted-extra"})
    prepared = runner._PreparedTurn([], "", event.text, None, None, "steer", entry.session_id, "accepted-owner")
    try:
        if fallback == "exception":
            await runner._hmwa_agent_error_reply(RuntimeError("controlled failure"), event, source,
                                                entry, entry.session_key, prepared)
        else:
            await runner._hmwa_persist_turn_transcript(
                event=event, source=source, session_entry=entry, session_key=entry.session_key,
                agent_result={"agent_persisted": False}, agent_messages=[], prepared=prepared,
                response="Acknowledged", agent_failed_early=False, hidden_reasoning_incomplete=False,
                is_context_overflow_failure=False)
        messages = db.get_messages(entry.session_id)
        users = [message for message in messages if message["role"] == "user"]
        assert len(users) == 1
        assert users[0]["content"] == rendered["content"]
        assert users[0]["display_metadata"] == {
            "input_events": records, "gateway_input_owner": "accepted-owner"}
        assert store.has_input_owner(entry.session_id, "accepted-owner")
    finally:
        db.close()
