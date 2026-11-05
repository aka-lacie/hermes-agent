"""Regression: a debounced inbound batch that flushes while its adapter is being replaced by a
reconnect is admitted through the reconnected adapter, exactly once, instead of being refused
``not_found`` by ``session_ingress_context._binding`` and silently lost.

Incident (2026-11-05 01:15): a Discord DM sat in the text-batch debounce buffer when the websocket
health check forced a reconnect. ``_handle_adapter_fatal_error_impl`` unregistered the adapter
first; the batch flushed 0.6s later on the retired instance, admission found no registered adapter
owning the source and raised ``RuntimeStoreError('not_found')``; the message was never admitted.

The registry steps below are the runner's own: ``self.adapters.pop`` (fatal handler) and
``_publish_primary_adapter`` + ``hand_over_held_inbound`` (``_install_reconnected_adapter``).
"""
import asyncio
import logging

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.platforms.helpers import hand_over_held_inbound
from gateway.platforms import retired_inbound
from gateway.session_ingress_context import (
    TransportRetired, capture_provenance, native_callback)
from hermes_state_runtime import RuntimeStoreError, _row

MESSAGE_ID = "1567829738071007253"


def _discord(runner=None, token="fixture-token"):
    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token=token, typing_indicator=False))
    adapter.gateway_runner = runner  # what GatewayRunner._create_adapter stamps on every adapter it builds
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    return adapter


async def _runner(monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    monkeypatch.setattr(SessionAuthority, "_schedule", lambda self, ref: None)  # admit, don't run a turn
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    runner = GatewayRunner(GatewayConfig())
    old = _discord(runner)
    runner.adapters = {Platform.DISCORD: old}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    runner._wire_adapter_handlers(old)
    return runner, authority, old


def _admissions(authority):
    with authority.db._read_ctx() as conn:
        return [_row(row) for row in conn.execute("SELECT * FROM session_admissions ORDER BY seq")]


async def _settle(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)
    for _ in range(20):  # let the spawned processing tasks finish their bookkeeping
        await asyncio.sleep(0)


def _reconnect(runner, old):
    """The two steps of ``_install_reconnected_adapter`` that matter: publish, then hand over."""
    new = _discord(runner)
    runner._wire_adapter_handlers(new)
    runner._publish_primary_adapter(Platform.DISCORD, new)
    hand_over_held_inbound(old, new)
    return new


def _dm(adapter, text="are you there? this got debounced first"):
    source = adapter.build_source(chat_id="42", chat_type="dm", user_id="42")
    return MessageEvent(text=text, source=source, message_id=MESSAGE_ID)


def _assert_admitted_once(authority, event, adapter):
    rows = _admissions(authority)
    assert len(rows) == 1, rows
    payload = rows[0]["payload"]
    assert payload["text"] == event.text
    assert rows[0]["request_id"] == MESSAGE_ID
    envelope = payload["native_text_v1"]
    assert envelope["event"]["message_id"] == MESSAGE_ID
    origin = envelope["source"]
    assert (origin["platform"], origin["chat_id"], origin["user_id"]) == ("discord", "42", "42")
    provenance = envelope["provenance"]
    assert provenance["platform"] == "discord"
    from gateway.run import GatewayRunner
    assert provenance["connector"] == GatewayRunner._adapter_credential_fingerprint(adapter)


@pytest.mark.asyncio
async def test_batch_flushed_mid_reconnect_is_admitted_once_through_the_new_adapter(monkeypatch):
    runner, authority, old = await _runner(monkeypatch)
    try:
        event = _dm(old)
        old._enqueue_text_event(event)
        # Websocket health check fails: the fatal handler unregisters the adapter before the
        # debounce timer fires.
        runner.adapters.pop(Platform.DISCORD)
        await _settle(lambda: retired_inbound.held_events(old))
        assert _admissions(authority) == []  # nothing bound yet: no adapter owns the source

        new = _reconnect(runner, old)
        await _settle(lambda: _admissions(authority))
        _assert_admitted_once(authority, event, new)
        assert event.source._transport_adapter_ref() is new
        assert retired_inbound.held_events(old) == [] and retired_inbound.held_events(new) == []
        # A second hand-over (or a late duplicate flush of the same object) admits nothing more.
        hand_over_held_inbound(old, new)
        await _settle(lambda: True)
        assert len(_admissions(authority)) == 1
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_batch_flushed_after_the_new_adapter_is_published_forwards_at_once(monkeypatch):
    runner, authority, old = await _runner(monkeypatch)
    try:
        event = _dm(old)
        runner.adapters.pop(Platform.DISCORD)
        new = _reconnect(runner, old)  # old's timer fires only after the replacement is live
        old._enqueue_text_event(event)
        await _settle(lambda: _admissions(authority))
        _assert_admitted_once(authority, event, new)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_adapter_unregistered_inside_the_handler_is_held_not_lost(monkeypatch):
    """The fatal handler can pop the adapter after the handler's entry check: admission's own
    ``TransportRetired`` (raised before any commit) holds the event too."""
    runner, authority, old = await _runner(monkeypatch)
    try:
        event = _dm(old)
        original = runner._hm_admit_event

        async def _pop_then_admit(evt):
            runner.adapters.pop(Platform.DISCORD, None)
            return await original(evt)

        monkeypatch.setattr(runner, "_hm_admit_event", _pop_then_admit)
        await old.handle_message(event)
        await _settle(lambda: retired_inbound.held_events(old))
        assert _admissions(authority) == []
        monkeypatch.setattr(runner, "_hm_admit_event", original)
        new = _reconnect(runner, old)
        await _settle(lambda: _admissions(authority))
        _assert_admitted_once(authority, event, new)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_failed_candidate_hands_held_inbound_back_and_the_next_success_admits(monkeypatch):
    runner, authority, old = await _runner(monkeypatch)
    try:
        runner.adapters.pop(Platform.DISCORD)
        candidate = _discord(runner)
        runner._wire_adapter_handlers(candidate)
        event = _dm(candidate)  # received while the candidate was connecting, never published
        await candidate.handle_message(event)
        await _settle(lambda: retired_inbound.held_events(candidate))
        hand_over_held_inbound(candidate, old)  # connect failed: the runner keeps the predecessor
        assert retired_inbound.held_events(old) == [event]
        new = _reconnect(runner, old)
        await _settle(lambda: _admissions(authority))
        _assert_admitted_once(authority, event, new)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_held_inbound_with_no_reconnect_is_reported_at_error_with_its_message_id(monkeypatch, caplog):
    runner, authority, old = await _runner(monkeypatch)
    monkeypatch.setattr(retired_inbound, "ALERT_SECONDS", 0.05)
    try:
        runner.adapters.pop(Platform.DISCORD)
        event = _dm(old)
        with caplog.at_level(logging.WARNING, logger="gateway.platforms.retired_inbound"):
            old._enqueue_text_event(event)
            await _settle(lambda: retired_inbound.held_events(old))
            await asyncio.sleep(0.15)
        errors = [r for r in caplog.records if r.levelno == logging.ERROR and MESSAGE_ID in r.getMessage()]
        assert errors and "kept for redelivery" in errors[0].getMessage()
        assert retired_inbound.held_events(old) == [event]  # still kept, not discarded
        assert _admissions(authority) == []
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_foreign_adapter_event_is_still_refused_not_found_and_never_held(monkeypatch):
    """An event from an adapter this runner never built (no lineage) keeps the plain ``not_found``
    refusal: nothing is held and nothing can later forward it."""
    runner, authority, old = await _runner(monkeypatch)
    try:
        stranger = _discord()  # never bound to this runner
        event = _dm(stranger)
        with native_callback(runner, event, runner._native_transport_homes[None]):
            with pytest.raises(RuntimeStoreError) as refused:
                capture_provenance(runner, event)
        assert refused.value.reason == "not_found" and not isinstance(refused.value, TransportRetired)
        assert runner._hold_retired_inbound(event) is False
        assert retired_inbound.held_events(stranger) == []
        assert _admissions(authority) == []
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_held_inbound_never_moves_to_an_adapter_with_another_credential(monkeypatch):
    runner, authority, old = await _runner(monkeypatch)
    try:
        event = _dm(old)
        runner.adapters.pop(Platform.DISCORD)
        old._enqueue_text_event(event)
        await _settle(lambda: retired_inbound.held_events(old))
        other_bot = _discord(runner, token="another-bot-token")
        runner._wire_adapter_handlers(other_bot)
        runner._publish_primary_adapter(Platform.DISCORD, other_bot)
        hand_over_held_inbound(old, other_bot)
        await _settle(lambda: True)
        assert _admissions(authority) == []
        assert event.source._transport_adapter_ref() is old
    finally:
        authority.db.close()
