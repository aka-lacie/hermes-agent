"""Human steering belongs only to the addressed parent, never its running children.

Background delegate tasks receive a new human message only when their parent explicitly
spawns, resumes, or steers them. Inbound DMs, /steer and priority steers must not fan out.
"""
import threading

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _Agent:
    def __init__(self, children=(), accepts=True):
        self.payload = None
        self.accepts = accepts
        self._active_children = list(children)
        self._active_children_lock = threading.Lock()

    def steer(self, text):
        self.payload = text
        return self.accepts


class _TypedAgent(_Agent):
    def steer(self, text, *, origin=None):
        self.origin = origin
        return super().steer(text)


def _event(text="focus on rows 10-20"):
    source = SessionSource(platform=Platform.DISCORD, chat_id="c", user_id="u", chat_type="dm")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="m")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [_Agent, _TypedAgent])
@pytest.mark.parametrize("route", ["busy_steer_mode", "priority", "explicit_command"])
async def test_busy_dm_steer_only_reaches_addressed_parent(route, agent_type, tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    child_a, child_b = _Agent(), _TypedAgent()
    parent = agent_type(children=[child_a, child_b])
    runner._session_state("key").turn.agent = parent
    event = _event("/steer focus on rows 10-20" if route == "explicit_command" else "focus on rows 10-20")

    if route == "busy_steer_mode":
        outcome = await runner._resolve_busy_steer_or_redirect(event, "key", "steer", parent)
        assert outcome.steered is True
    elif route == "priority":
        runner._hm_busy_steer(event, parent, "key")
    else:
        await runner._busy_steer_command(event, "key", event.source)

    assert parent.payload and parent.payload.endswith("focus on rows 10-20")
    assert child_a.payload is None
    assert child_b.payload is None
    if isinstance(parent, _TypedAgent):
        assert parent.origin is not None
        assert parent.origin["platform"] == "discord"


@pytest.mark.parametrize("agent_type", [_Agent, _TypedAgent])
def test_parent_refusal_never_becomes_success_through_a_child(agent_type, tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    child = _Agent()
    parent = agent_type(children=[child], accepts=False)
    assert runner._steer_for_event(parent, "parent-only note", _event()) is False
    assert child.payload is None


@pytest.mark.asyncio
async def test_inbound_discord_dm_during_running_delegation_is_parent_only(monkeypatch):
    from tests.gateway.test_busy_session_ack import _make_adapter, _make_runner
    from gateway.session import build_session_key

    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    runner, _sentinel = _make_runner()
    runner._busy_input_mode = "steer"
    adapter = _make_adapter()
    event = _event("private direction to parent")
    key = build_session_key(event.source)
    runner.adapters[Platform.DISCORD] = adapter
    children = [_Agent(), _TypedAgent()]
    parent = _TypedAgent(children=children)
    runner._running_agents[key] = parent

    assert await runner._handle_active_session_busy_message(event, key)
    assert parent.payload == "private direction to parent"
    assert all(child.payload is None for child in children)
    assert key not in adapter._pending_messages
