"""Child process notifications must not replace the human's gateway conversation.

Regression for the October 12 incident: a terminal completion targeted a running
delegate, repointed Discord to its transcript, and waited on the child's lease.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "route.db")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    store._db = db
    source = SessionSource(platform=Platform.DISCORD, chat_id="route-test", user_id="owner", chat_type="dm")
    entry = store.get_or_create_session(source)
    db.create_session(entry.session_id, source="discord")
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    try:
        yield runner, db, entry, source
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["source", "creation_source", "marker", "compressed"])
async def test_child_completion_cannot_change_route_or_close_parent(conversation, identity):
    runner, db, entry, _ = conversation
    # Both immutable origin and the marker must work after transport metadata has
    # made source='discord'. Compression copies the marker from the child root.
    child = "delegate-child"
    db.create_session(child, source="subagent" if identity in {"source", "creation_source"} else "discord",
                      parent_session_id=entry.session_id,
                      model_config={"_delegate_from": entry.session_id} if identity in {"marker", "compressed"} else {})
    if identity == "creation_source":
        db._write_sql("UPDATE sessions SET source = 'discord' WHERE id = ?", (child,))
    if identity == "compressed":
        db.end_session(child, end_reason="compression")
        db.create_session("delegate-tip", source="discord", parent_session_id=child,
                          model_config={"_delegate_from": entry.session_id})
        child = "delegate-tip"
    assert db.try_acquire_session_turn_lease(child, "delegate-worker", ttl_seconds=300)
    event = {"type": "completion", "session_id": "proc-child", "parent_session_id": child}

    # Preflight must terminally decline before adapter acceptance, not falsely
    # acknowledge or retry forever. Already-admitted events need the same guard.
    claim = await runner._preflight_completion_delivery(event)
    resolved = await runner._resolve_async_delegation_session(entry, child)

    assert not claim.proceed and claim.early_result is None
    assert resolved is None
    assert runner.session_store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
    assert db.get_session(entry.session_id)["ended_at"] is None
    assert db.session_turn_lease_expires_at(child, "delegate-worker") is not None


@pytest.mark.asyncio
async def test_queued_child_watch_event_never_starts_foreground_turn(conversation):
    runner, db, entry, source = conversation
    db.create_session("watch-child", source="subagent", parent_session_id=entry.session_id)
    foreground_turn = AsyncMock()

    async def handle(event):
        resolved = await runner._resolve_async_delegation_session(
            entry, event.metadata["gateway_session_id"],
        )
        if resolved is not None:
            await foreground_turn(resolved)
        event._gateway_accepted = True

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=handle))
    runner._build_process_event_source = MagicMock(return_value=source)
    runner._resolve_injection_adapter = MagicMock(return_value=adapter)
    event = {"type": "watch_match", "session_id": "proc-watch", "parent_session_id": "watch-child",
             "session_key": entry.session_key}

    await runner._inject_watch_notification("process ready", event)

    adapter.handle_message.assert_awaited_once()
    foreground_turn.assert_not_awaited()
    assert runner.session_store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
    assert db.get_session(entry.session_id)["ended_at"] is None


@pytest.mark.asyncio
async def test_parent_results_and_compression_continuations_still_deliver(conversation):
    runner, db, entry, _ = conversation
    parent_id = entry.session_id
    # The delegate's RESULT and a process explicitly handed to the parent target
    # this parent ID, unlike an intermediate process owned by the child.
    claim = await runner._preflight_completion_delivery(
        {"type": "completion", "session_id": "handed-off", "parent_session_id": entry.session_id},
    )
    assert claim.proceed
    assert await runner._resolve_async_delegation_session(entry, entry.session_id) is entry

    db.end_session(entry.session_id, end_reason="compression")
    db.create_session("parent-tip", source="discord", parent_session_id=entry.session_id)
    assert await runner._classify_completion_target(entry.session_id) == "deliver"
    resolved = await runner._resolve_async_delegation_session(entry, entry.session_id)
    assert resolved.session_id == "parent-tip"
    assert runner.session_store.lookup_by_session_key(entry.session_key).session_id == "parent-tip"
    assert db.get_session(parent_id)["end_reason"] == "compression"
