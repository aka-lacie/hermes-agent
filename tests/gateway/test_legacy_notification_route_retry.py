"""A deterministic adapter key mismatch spends a bounded retry budget only on the legacy lane."""
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.wake import WakeNotAccepted
from tests.gateway.test_background_process_notifications import _build_runner, _watch_event
from tests.module_clock import install_module_clock


def _legacy(monkeypatch, tmp_path, *, mismatch):
    runner = _build_runner(monkeypatch, tmp_path, "all")
    event = _watch_event()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", thread_id="42")
    adapter = SimpleNamespace(handle_message=AsyncMock(), send=AsyncMock(),
        _event_session_key=lambda e: "wrong-route" if mismatch else e.metadata["gateway_session_key"])
    runner._build_process_event_source = lambda e: source
    runner._resolve_injection_adapter = lambda *a: adapter
    return runner, adapter, event


@pytest.mark.asyncio
async def test_legacy_bad_route_has_a_permanent_reason(monkeypatch, tmp_path):
    runner, adapter, event = _legacy(monkeypatch, tmp_path, mismatch=True)
    with pytest.raises(WakeNotAccepted) as refusal:
        await runner._inject_watch_notification("ready", event, raise_not_accepted=True)
    assert refusal.value.reason == "invalid_params" and refusal.value.permanent
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_adapter_deterministic_bad_route_is_not_refunded(monkeypatch, tmp_path):
    from gateway import session_authorities
    from tests.gateway.test_automation_compression_media import _discord_owner
    runner, adapter, authority, source = await _discord_owner(tmp_path, monkeypatch)
    try:
        # Exercise the actual adapter rejection with no authority, rather than merely a fake receipt.
        monkeypatch.setattr(session_authorities, "active_authority", lambda runner: None)
        wrong_source = adapter.build_source(chat_id="different-chat", chat_type="dm", user_id=source.user_id)
        monkeypatch.setattr(runner, "_build_process_event_source", lambda evt: wrong_source)
        event = {"type": "watch_match", "session_id": "proc-real", "started_at": 1,
                 "session_key": runner.session_store._generate_session_key(source)}
        with pytest.raises(WakeNotAccepted) as refusal:
            await runner._inject_watch_notification("ready", event, raise_not_accepted=True)
        assert refusal.value.reason == "invalid_params" and refusal.value.permanent
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_legacy_bad_route_exhausts_retry_budget(monkeypatch, tmp_path):
    from gateway import run_notifications
    runner, adapter, event = _legacy(monkeypatch, tmp_path, mismatch=True)
    clock = install_module_clock(monkeypatch, run_notifications)
    pending = queue.Queue()
    pending.put(event)
    for _ in range(50):
        if pending.empty():
            break
        clock.now += 120
        await runner._drain_watch_notifications(pending)
    assert pending.empty()
    assert adapter.handle_message.await_count == run_notifications._MAX_COMPLETION_INJECTION_ATTEMPTS


@pytest.mark.asyncio
async def test_legacy_transient_refusal_keeps_refunding_budget(monkeypatch, tmp_path):
    from gateway import run_notifications
    runner, adapter, event = _legacy(monkeypatch, tmp_path, mismatch=False)
    clock = install_module_clock(monkeypatch, run_notifications)
    pending = queue.Queue()
    pending.put(event)
    for _ in range(run_notifications._MAX_COMPLETION_INJECTION_ATTEMPTS + 3):
        clock.now += 120
        await runner._drain_watch_notifications(pending)
    assert not pending.empty()
    assert adapter.handle_message.await_count > run_notifications._MAX_COMPLETION_INJECTION_ATTEMPTS


@pytest.mark.asyncio
async def test_drain_refusal_remains_transient_even_for_bad_legacy_route(monkeypatch, tmp_path):
    runner, adapter, event = _legacy(monkeypatch, tmp_path, mismatch=True)
    adapter.gateway_runner = SimpleNamespace(_draining=True)
    with pytest.raises(WakeNotAccepted) as refusal:
        await runner._inject_watch_notification("ready", event, raise_not_accepted=True)
    assert refusal.value.reason == "runtime_draining" and not refusal.value.permanent
    adapter.handle_message.assert_not_awaited()
