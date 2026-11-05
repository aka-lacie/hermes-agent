"""Rule 1 for delivery mirrors (plan §4.7 class A): a mirrored send is never written as the target
session's assistant. ``hermes send`` (script text) mirrors nowhere; the ``send_message`` tool mirrors
into OTHER sessions only, as a ``role="user"`` system_event naming the sending session."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import gateway.mirror as mirror_mod
from gateway.config import Platform
from hermes_state import SessionDB


@pytest.fixture
def target_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("target", source="discord", session_key="discord:dm:1", chat_id="1")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    index = sessions_dir / "sessions.json"
    index.write_text(json.dumps({"dm": {"session_id": "target", "origin": {"platform": "discord", "chat_id": "1"},
                                        "updated_at": "2026-11-05T00:00:00"}}), encoding="utf-8")
    with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), patch.object(mirror_mod, "_SESSIONS_INDEX", index), \
            patch("hermes_state_registry.acquire", return_value=db), patch("hermes_state_registry.release_or_close"):
        yield db
    db.close()


def test_mirror_is_a_user_system_event_naming_its_sender(target_db):
    assert mirror_mod.mirror_to_session("discord", "1", "sent from elsewhere", source_label="telegram",
                                        sender_id="session:sender") is True
    [row] = target_db.get_messages_as_conversation("target")
    assert row["role"] == "user" and row["content"] == "sent from elsewhere"
    assert (row["sender_kind"], row["sender_id"], row["part_kind"]) == ("system", "session:sender", "delivery_mirror")
    assert row["_parts"] == [{"type": "system_event", "v": 1, "kind": "delivery_mirror", "text": "sent from elsewhere",
                              "meta": {"source": "telegram", "platform": "discord"}}]
    assert target_db._conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'assistant'").fetchone()[0] == 0


def test_mirror_into_the_senders_own_session_is_skipped(target_db):
    assert mirror_mod.mirror_to_session("discord", "1", "my own reply", sender_id="session:target",
                                        exclude_session_id="target") is False
    assert target_db.get_messages_as_conversation("target") == []


def _send(**kw):
    from tools.send_message_tool import send_message_tool
    discord_cfg = SimpleNamespace(enabled=True, token="***", extra={})
    config = SimpleNamespace(platforms={Platform.DISCORD: discord_cfg}, get_home_channel=lambda _platform: None)
    with patch("gateway.config.load_gateway_config", return_value=config), \
            patch("tools.interrupt.is_interrupted", return_value=False), \
            patch("tools.send_message_tool.resolve_send_target", return_value=("1", None, None)), \
            patch("model_tools._run_async", side_effect=asyncio.run), \
            patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})), \
            patch("tools.send_message_tool._authorize_relay_target", return_value=None), \
            patch("tools.send_message_tool._mirror_sent_message", return_value=True) as mirror:
        result = json.loads(send_message_tool({"action": "send", "target": "discord:1", "message": "hi"}, **kw))
    return result, mirror


def test_send_message_tool_mirrors_but_hermes_send_does_not():
    result, mirror = _send()
    assert result["success"] is True and result.get("mirrored") is True
    mirror.assert_called_once()
    result, mirror = _send(mirror=False)
    assert result["success"] is True and "mirrored" not in result
    mirror.assert_not_called()


def test_tool_mirror_names_the_sending_session_and_excludes_it():
    from tools.send_message_tool import _mirror_sent_message
    env = {"HERMES_SESSION_ID": "sender-sid", "HERMES_SESSION_PLATFORM": "telegram"}
    with patch("gateway.session_context.get_session_env", side_effect=lambda name, default="": env.get(name, default)), \
            patch("gateway.mirror.mirror_to_session", return_value=True) as mirror:
        assert _mirror_sent_message("discord", "1", "hi", None) is True
    assert mirror.call_args.kwargs["sender_id"] == "session:sender-sid"
    assert mirror.call_args.kwargs["exclude_session_id"] == "sender-sid"


def test_hermes_send_cli_never_mirrors(monkeypatch):
    import sys
    import types
    from hermes_cli import send_cmd
    calls = []
    stub = types.ModuleType("tools.send_message_tool")
    stub.send_message_tool = lambda args, **kw: calls.append(kw) or json.dumps({"success": True})
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", stub)
    parser = __import__("argparse").ArgumentParser()
    send_cmd.register_send_subparser(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        send_cmd.cmd_send(parser.parse_args(["send", "--to", "discord:1", "alert"]))
    assert calls == [{"mirror": False}]
