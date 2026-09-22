"""Tests for send_message action='react'/'unreact' dispatch.

Kept separate from ``test_send_message_tool.py`` because that module skips
wholesale when optional Telegram dependencies are not installed.
"""

import json
import pytest
from types import SimpleNamespace
from unittest.mock import patch

import tools.send_message_tool as smt


class _FakePhotonAdapter:
    """Adapter exposing add_reaction/remove_reaction coroutines."""

    def __init__(self):
        self.calls = []

    async def add_reaction(self, chat_id, emoji, message_id=None):
        self.calls.append(("add", chat_id, emoji, message_id))
        return {"success": True, "emoji": emoji}

    async def remove_reaction(self, chat_id, message_id=None):
        self.calls.append(("remove", chat_id, message_id))
        return {"success": True}


class _NoReactionAdapter:
    """Adapter with no reaction support at all."""


def _runner_with(adapter):
    from gateway.config import Platform

    return SimpleNamespace(adapters={Platform("photon"): adapter})


def _call(args):
    return json.loads(smt.send_message_tool(args))


@pytest.mark.parametrize("same_chat", [True, False])
def test_react_dispatches_to_add_reaction(same_chat):
    adapter = _FakePhotonAdapter()
    from gateway.response_delivery import response_delivery_scope, capture_response_delivery
    source = SimpleNamespace(platform="photon", chat_id="+15551234567" if same_chat else "other", thread_id=None)
    with response_delivery_scope(source), patch("gateway.run._gateway_runner_ref", lambda: _runner_with(adapter)):
        result = _call(
            {"action": "react", "target": "photon:+15551234567", "emoji": "❤️"}
        )
        receipt = {}
        capture_response_delivery(receipt)
        assert receipt["user_visible_response"] is same_chat
    assert result["success"] is True
    assert adapter.calls == [("add", "+15551234567", "❤️", None)]


def test_react_without_live_gateway():
    with patch("gateway.run._gateway_runner_ref", lambda: None):
        result = _call(
            {"action": "react", "target": "photon:+15551234567", "emoji": "👍"}
        )
    assert result.get("success") is not True
    assert "live" in json.dumps(result)


@pytest.mark.parametrize("mode", ["text", "attachment", "failed", "skipped", "other-chat"])
def test_send_receipt_requires_actual_delivery_to_current_conversation(monkeypatch, tmp_path, mode):
    from unittest.mock import AsyncMock
    from gateway.config import Platform
    from gateway.response_delivery import response_delivery_scope, capture_response_delivery
    from tools.registry import ToolRegistry
    source = SimpleNamespace(platform="discord", chat_id="123", thread_id=None)
    target_chat = "456" if mode == "other-chat" else "123"
    monkeypatch.setattr(smt, "_resolve_tool_target", lambda target: ("discord", target_chat, None, None))
    monkeypatch.setattr(smt, "_resolve_platform_config", lambda *a: (
        Platform.DISCORD, SimpleNamespace(token="fake"), None, None))
    monkeypatch.setattr(smt, "_authorize_relay_target", lambda *a, **kw: None)
    monkeypatch.setattr(smt, "_mirror_sent_message", lambda *a: False)
    skipped = {"success": True, "skipped": True} if mode == "skipped" else None
    monkeypatch.setattr(smt, "_maybe_skip_cron_duplicate_send", lambda *a: skipped)
    sender = AsyncMock(return_value={"success": mode != "failed", "message_id": "sent"})
    monkeypatch.setattr(smt, "_send_to_platform", sender)
    message = "Here you go."
    if mode == "attachment":
        attachment = tmp_path / "photo.png"
        attachment.write_bytes(b"fixture")
        message = f"MEDIA:{attachment}"
    registry = ToolRegistry()
    registry.register(name="send_message", toolset="messaging", schema={}, handler=smt.send_message_tool)
    with response_delivery_scope(source):
        result = json.loads(registry.dispatch("send_message", {
            "action": "send", "target": f"discord:{target_chat}", "message": message,
        }))
        assert result.get("success") is (mode != "failed")
        receipt = {}
        capture_response_delivery(receipt)
        assert receipt["user_visible_response"] is (mode in {"text", "attachment"})
    if mode == "skipped":
        sender.assert_not_awaited()
    elif mode == "attachment":
        assert sender.await_args.kwargs["media_files"]
