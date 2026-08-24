"""Generic internal-turn envelope and live-runner handoff."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import sys
import types
from unittest.mock import AsyncMock

import pytest

from gateway.internal_turns import (
    INTERNAL_NOTIFICATION_MARKER,
    InternalTurnService,
    InternalTurnTarget,
    format_internal_notification,
    handle_internal_turn_control_request,
    trusted_internal_notification_context,
)
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SessionSource


def test_internal_notification_envelope_is_stable():
    text = format_internal_notification(
        "Check the customer thread",
        kind="reminder",
        source_label="cron",
        event_id="job-1",
    )

    assert text == (
        f"{INTERNAL_NOTIFICATION_MARKER}\n"
        "kind: reminder\n"
        "source: cron\n"
        "event_id: job-1\n"
        "---\n"
        "This is an automated background event, not a new message from the "
        "user. Use the current conversation context, act if useful, and reply "
        "only when the user needs an update. Otherwise respond with exactly "
        "[SILENT].\n\n"
        "Check the customer thread"
    )


def test_service_hands_target_to_active_runner(monkeypatch):
    calls = []
    runner = types.SimpleNamespace(
        enqueue_internal_turn=lambda **kwargs: calls.append(kwargs) or True
    )
    fake_gateway_run = types.ModuleType("gateway.run")
    fake_gateway_run.get_active_gateway_runner = lambda: runner
    monkeypatch.setitem(sys.modules, "gateway.run", fake_gateway_run)

    accepted = InternalTurnService().enqueue(
        "Background work completed",
        platform="discord",
        chat_id="dm-123",
        user_id="freya",
        kind="webhook_completion",
        source_label="inbox",
        event_id="event-1",
    )

    assert accepted is True
    assert len(calls) == 1
    call = calls[0]
    assert call["target"].platform == "discord"
    assert call["target"].chat_id == "dm-123"
    assert call["target"].user_id == "freya"
    assert call["metadata"]["internal_notification"] is True
    assert INTERNAL_NOTIFICATION_MARKER in call["text"]


def test_service_hands_off_to_gateway_control_socket(monkeypatch):
    fake_gateway_run = types.ModuleType("gateway.run")
    fake_gateway_run.get_active_gateway_runner = lambda: None
    monkeypatch.setitem(sys.modules, "gateway.run", fake_gateway_run)
    seen = {}

    def query(home, verb, *, payload=None, timeout=2.0):
        seen.update({"home": home, "verb": verb, "payload": payload})
        return {"accepted": True}

    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: "/tmp/profile")

    accepted = InternalTurnService().enqueue(
        "Background work completed",
        platform="discord",
        chat_id="dm-123",
        kind="cron_completion",
        source_label="cron",
    )

    assert accepted is True
    assert seen["verb"] == "enqueue_internal_turn"
    assert seen["payload"]["target"]["chat_id"] == "dm-123"
    assert seen["payload"]["kind"] == "cron_completion"
    assert seen["payload"]["source_label"] == "cron"
    assert seen["payload"]["text"] == "Background work completed"


def test_control_request_hands_turn_to_runner():
    calls = []
    runner = types.SimpleNamespace(
        enqueue_internal_turn=lambda **kwargs: calls.append(kwargs) or True
    )

    result = handle_internal_turn_control_request(
        runner,
        {
            "payload": {
                "target": {
                    "platform": "discord",
                    "chat_id": "dm-123",
                    "chat_type": "dm",
                },
                "text": "Wake up",
                "kind": "reminder",
                "source_label": "cron",
                "metadata": {"cron_job_id": "job-1"},
            }
        },
    )

    assert result == {"accepted": True}
    assert calls[0]["target"] == InternalTurnTarget(
        platform="discord", chat_id="dm-123", chat_type="dm"
    )
    assert INTERNAL_NOTIFICATION_MARKER in calls[0]["text"]
    assert calls[0]["metadata"] == {
        "cron_job_id": "job-1",
        "internal_notification": True,
        "internal_notification_kind": "reminder",
        "internal_notification_source": "cron",
    }


def test_service_rejects_empty_payload():
    assert (
        InternalTurnService().enqueue(
            "   ",
            platform="discord",
            chat_id="dm-123",
        )
        is False
    )


def test_notification_context_requires_synthetic_event_and_service_flag():
    marker_text = f"{INTERNAL_NOTIFICATION_MARKER}\n---\nCall Sam"
    ordinary = MessageEvent(
        text=marker_text,
        source=SessionSource(platform=Platform.DISCORD, chat_id="dm-123"),
        metadata={
            "internal_notification": True,
            "internal_notification_kind": "reminder",
        },
    )
    unrelated_internal = MessageEvent(
        text=marker_text,
        source=ordinary.source,
        internal=True,
        metadata={"internal_notification": "true"},
    )

    assert trusted_internal_notification_context(ordinary) is None
    assert trusted_internal_notification_context(unrelated_internal) is None


def test_notification_context_sanitizes_authenticated_metadata():
    event = MessageEvent(
        text=f"{INTERNAL_NOTIFICATION_MARKER}\n---\nCall Sam",
        source=SessionSource(platform=Platform.DISCORD, chat_id="dm-123"),
        internal=True,
        metadata={
            "internal_notification": True,
            "internal_notification_kind": "reminder",
            "internal_notification_source": "cron",
            "internal_notification_id": 42,
            "platform_secret": "must-not-leak",
        },
    )

    assert trusted_internal_notification_context(event) == {
        "kind": "reminder",
        "source": "cron",
        "event_id": "42",
    }


@pytest.mark.asyncio
async def test_runner_dispatches_to_most_recent_matching_channel_source():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._gateway_event_loop = asyncio.get_running_loop()
    runner._background_tasks = set()
    cached_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="dm-123",
        chat_type="dm",
        user_id="freya",
        user_name="Freya",
        profile="yuri",
    )
    runner._session_sources = OrderedDict([("session-key", cached_source)])
    adapter = types.SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter

    accepted = runner.enqueue_internal_turn(
        target=InternalTurnTarget(
            platform="discord",
            chat_id="dm-123",
            profile="yuri",
        ),
        text="[HERMES_INTERNAL_NOTIFICATION]\n---\nDone",
        metadata={"internal_notification": True},
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert accepted is True
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.internal is True
    assert event.source.user_id == "freya"
    assert event.source.profile == "yuri"
    assert event.metadata["internal_notification"] is True


@pytest.mark.asyncio
async def test_runner_recovers_persisted_conversation_source_after_restart():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._gateway_event_loop = asyncio.get_running_loop()
    runner._background_tasks = set()
    runner._session_sources = OrderedDict()
    persisted_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-123",
        chat_type="group",
        user_id="freya",
        scope_id="guild-9",
    )
    runner.session_store = types.SimpleNamespace(
        list_sessions=lambda: [types.SimpleNamespace(origin=persisted_source)]
    )
    runner.config = types.SimpleNamespace(multiplex_profiles=False)
    adapter = types.SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter

    accepted = runner.enqueue_internal_turn(
        target=InternalTurnTarget(
            platform="discord",
            chat_id="channel-123",
        ),
        text="[HERMES_INTERNAL_NOTIFICATION]\n---\nDone",
        metadata={"internal_notification": True},
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert accepted is True
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "group"
    assert event.source.scope_id == "guild-9"
    assert event.source.user_id == "freya"


@pytest.mark.asyncio
async def test_runner_does_not_reuse_thread_for_top_level_target():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._gateway_event_loop = asyncio.get_running_loop()
    runner._background_tasks = set()
    runner._session_sources = OrderedDict()
    threaded_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-123",
        chat_type="group",
        thread_id="thread-9",
        scope_id="guild-9",
    )
    runner.session_store = types.SimpleNamespace(
        list_sessions=lambda: [types.SimpleNamespace(origin=threaded_source)]
    )
    runner.config = types.SimpleNamespace(multiplex_profiles=False)
    adapter = types.SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter

    accepted = runner.enqueue_internal_turn(
        target=InternalTurnTarget(
            platform="discord",
            chat_id="channel-123",
            chat_type="group",
        ),
        text="[HERMES_INTERNAL_NOTIFICATION]\n---\nDone",
        metadata={"internal_notification": True},
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert accepted is True
    event = adapter.handle_message.await_args.args[0]
    assert event.source.thread_id is None
    assert event.source.scope_id is None
