"""Tests for plugins/memory/honcho/session.py — HonchoSession and helpers."""

import sys
from datetime import datetime
from unittest.mock import MagicMock

from plugins.memory.honcho.client import HonchoClientConfig
from plugins.memory.honcho.session import (
    HonchoSession,
    HonchoSessionManager,
)


# ---------------------------------------------------------------------------
# HonchoSession dataclass
# ---------------------------------------------------------------------------


class TestHonchoSession:
    def _make_session(self):
        return HonchoSession(
            key="telegram:12345",
            user_peer_id="user-telegram-12345",
            assistant_peer_id="hermes-assistant",
            honcho_session_id="telegram-12345",
        )

    def test_initial_state(self):
        session = self._make_session()
        assert session.key == "telegram:12345"
        assert session.messages == []
        assert isinstance(session.created_at, datetime)
        assert isinstance(session.updated_at, datetime)

    def test_add_message(self):
        session = self._make_session()
        session.add_message("user", "Hello!")
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "Hello!"
        assert "timestamp" in session.messages[0]

    def test_add_message_with_kwargs(self):
        session = self._make_session()
        session.add_message("assistant", "Hi!", source="gateway")
        assert session.messages[0]["source"] == "gateway"

    def test_add_message_updates_timestamp(self):
        session = self._make_session()
        original = session.updated_at
        session.add_message("user", "test")
        assert session.updated_at >= original

    def test_get_history(self):
        session = self._make_session()
        session.add_message("user", "msg1")
        session.add_message("assistant", "msg2")
        history = session.get_history()
        assert len(history) == 2
        assert history[0] == {"role": "user", "content": "msg1"}
        assert history[1] == {"role": "assistant", "content": "msg2"}

    def test_get_history_strips_extra_fields(self):
        session = self._make_session()
        session.add_message("user", "hello", extra="metadata")
        history = session.get_history()
        assert "extra" not in history[0]
        assert set(history[0].keys()) == {"role", "content"}

    def test_get_history_max_messages(self):
        session = self._make_session()
        for i in range(10):
            session.add_message("user", f"msg{i}")
        history = session.get_history(max_messages=3)
        assert len(history) == 3
        assert history[0]["content"] == "msg7"
        assert history[2]["content"] == "msg9"

    def test_get_history_max_messages_larger_than_total(self):
        session = self._make_session()
        session.add_message("user", "only one")
        history = session.get_history(max_messages=100)
        assert len(history) == 1

    def test_clear(self):
        session = self._make_session()
        session.add_message("user", "msg1")
        session.add_message("user", "msg2")
        session.clear()
        assert session.messages == []

    def test_clear_updates_timestamp(self):
        session = self._make_session()
        session.add_message("user", "msg")
        original = session.updated_at
        session.clear()
        assert session.updated_at >= original


# ---------------------------------------------------------------------------
# HonchoSessionManager._sanitize_id
# ---------------------------------------------------------------------------


class TestSanitizeId:
    def test_clean_id_unchanged(self):
        mgr = HonchoSessionManager()
        assert mgr._sanitize_id("telegram-12345") == "telegram-12345"

    def test_colons_replaced(self):
        mgr = HonchoSessionManager()
        assert mgr._sanitize_id("telegram:12345") == "telegram-12345"

    def test_special_chars_replaced(self):
        mgr = HonchoSessionManager()
        result = mgr._sanitize_id("user@chat#room!")
        assert "@" not in result
        assert "#" not in result
        assert "!" not in result

    def test_alphanumeric_preserved(self):
        mgr = HonchoSessionManager()
        assert mgr._sanitize_id("abc123_XYZ-789") == "abc123_XYZ-789"


# ---------------------------------------------------------------------------
# HonchoSessionManager._format_migration_transcript
# ---------------------------------------------------------------------------


class TestFormatMigrationTranscript:
    def test_basic_transcript(self):
        messages = [
            {"role": "user", "content": "Hello", "timestamp": "2026-01-01T00:00:00"},
            {"role": "assistant", "content": "Hi!", "timestamp": "2026-01-01T00:01:00"},
        ]
        result = HonchoSessionManager._format_migration_transcript("telegram:123", messages)
        assert isinstance(result, bytes)
        text = result.decode("utf-8")
        assert "<prior_conversation_history>" in text
        assert "user: Hello" in text
        assert "assistant: Hi!" in text
        assert 'session_key="telegram:123"' in text
        assert 'message_count="2"' in text

    def test_empty_messages(self):
        result = HonchoSessionManager._format_migration_transcript("key", [])
        text = result.decode("utf-8")
        assert "<prior_conversation_history>" in text
        assert "</prior_conversation_history>" in text

    def test_missing_fields_handled(self):
        messages = [{"role": "user"}]  # no content, no timestamp
        result = HonchoSessionManager._format_migration_transcript("key", messages)
        text = result.decode("utf-8")
        assert "user: " in text  # empty content


# ---------------------------------------------------------------------------
# HonchoSessionManager.delete / list_sessions
# ---------------------------------------------------------------------------


class TestManagerCacheOps:
    def test_delete_cached_session(self):
        mgr = HonchoSessionManager()
        session = HonchoSession(
            key="test", user_peer_id="u", assistant_peer_id="a",
            honcho_session_id="s",
        )
        mgr._cache["test"] = session
        assert mgr.delete("test") is True
        assert "test" not in mgr._cache

    def test_delete_nonexistent_returns_false(self):
        mgr = HonchoSessionManager()
        assert mgr.delete("nonexistent") is False

    def test_list_sessions(self):
        mgr = HonchoSessionManager()
        s1 = HonchoSession(key="k1", user_peer_id="u", assistant_peer_id="a", honcho_session_id="s1")
        s2 = HonchoSession(key="k2", user_peer_id="u", assistant_peer_id="a", honcho_session_id="s2")
        s1.add_message("user", "hi")
        mgr._cache["k1"] = s1
        mgr._cache["k2"] = s2
        sessions = mgr.list_sessions()
        assert len(sessions) == 2
        keys = {s["key"] for s in sessions}
        assert keys == {"k1", "k2"}
        s1_info = next(s for s in sessions if s["key"] == "k1")
        assert s1_info["message_count"] == 1


class _FakeSessionPeerConfig:
    def __init__(self, *, observe_me, observe_others):
        self.observe_me = observe_me
        self.observe_others = observe_others


class TestObservationModes:
    def _make_manager(self, observation_mode: str) -> HonchoSessionManager:
        cfg = HonchoClientConfig(
            api_key="test-key",
            enabled=True,
            write_frequency="turn",
            observation_mode=observation_mode,
        )
        mgr = HonchoSessionManager(config=cfg)
        mgr._honcho = MagicMock()
        return mgr

    def test_session_peer_configs_respect_unified_directional_and_bidirectional(self):
        fake_module = type("FakeHonchoSessionModule", (), {"SessionPeerConfig": _FakeSessionPeerConfig})
        user_peer = object()
        ai_peer = object()

        expected = {
            "unified": ((True, False), (False, False)),
            "directional": ((True, False), (False, True)),
            "bidirectional": ((True, True), (True, True)),
        }

        for mode, ((user_me, user_others), (ai_me, ai_others)) in expected.items():
            mgr = self._make_manager(mode)
            fake_session = MagicMock()
            fake_session.context.return_value = MagicMock(messages=[])
            mgr._honcho.session.return_value = fake_session

            old_module = sys.modules.get("honcho.session")
            sys.modules["honcho.session"] = fake_module
            try:
                mgr._get_or_create_honcho_session("sess", user_peer, ai_peer)
            finally:
                if old_module is not None:
                    sys.modules["honcho.session"] = old_module
                else:
                    del sys.modules["honcho.session"]

            add_peers_arg = fake_session.add_peers.call_args.args[0]
            _, user_cfg = add_peers_arg[0]
            _, ai_cfg = add_peers_arg[1]
            assert (user_cfg.observe_me, user_cfg.observe_others) == (user_me, user_others)
            assert (ai_cfg.observe_me, ai_cfg.observe_others) == (ai_me, ai_others)

    def test_dialectic_query_uses_cross_observation_for_directional_and_bidirectional(self):
        for mode in ("directional", "bidirectional"):
            mgr = self._make_manager(mode)
            session = HonchoSession(
                key="k",
                user_peer_id="user",
                assistant_peer_id="ai",
                honcho_session_id="sess",
            )
            mgr._cache[session.key] = session

            ai_peer = MagicMock()
            ai_peer.chat.return_value = "answer"
            mgr._peers_cache["ai"] = ai_peer

            result = mgr.dialectic_query("k", "what does the user prefer?", peer="user")

            assert result == "answer"
            ai_peer.chat.assert_called_once_with(
                "what does the user prefer?",
                target="user",
                reasoning_level="low",
            )

    def test_create_conclusion_uses_cross_scope_for_bidirectional(self):
        mgr = self._make_manager("bidirectional")
        session = HonchoSession(
            key="k",
            user_peer_id="user",
            assistant_peer_id="ai",
            honcho_session_id="sess",
        )
        mgr._cache[session.key] = session

        assistant_peer = MagicMock()
        conclusions = MagicMock()
        assistant_peer.conclusions_of.return_value = conclusions
        mgr._peers_cache["ai"] = assistant_peer

        ok = mgr.create_conclusion("k", "User prefers terse responses")

        assert ok is True
        assistant_peer.conclusions_of.assert_called_once_with("user")
        conclusions.create.assert_called_once()
