"""User steering is parent-only, including while the parent supervises children."""

import threading

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource

class _Agent:
    def __init__(self, children=()):
        self.payload = None
        self._active_children = list(children)
        self._active_children_lock = threading.Lock()

    def steer(self, text):
        self.payload = text
        return True

def _event(text="focus on rows 10-20"):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c", user_id="u", chat_type="dm")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="m")

@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["busy_steer_mode", "priority", "explicit_command"])
async def test_busy_steer_stays_with_parent(route, tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    child_a, child_b = _Agent(), _Agent()
    parent = _Agent(children=[child_a, child_b])
    runner._session_state("key").turn.agent = parent
    event = _event("/steer focus on rows 10-20" if route == "explicit_command" else "focus on rows 10-20")

    if route == "busy_steer_mode":
        outcome = await runner._resolve_busy_steer_or_redirect(event, "key", "steer", parent)
        assert outcome.steered is True
    elif route == "priority":
        runner._hm_busy_steer(event, parent, "key")
    else:
        reply = await runner._busy_steer_command(event, "key", event.source)
        assert "subagent" not in reply

    assert parent.payload and parent.payload.endswith("focus on rows 10-20")
    # The parent decides which child, if any, needs the new user input.
    assert child_a.payload is None
    assert child_b.payload is None


def test_busy_steer_ack_names_subagents(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    parent = _Agent(children=[_Agent()])
    kwargs = dict(is_steer_mode=True, is_queue_mode=False, is_redirect_mode=False,
                  demoted_for_subagents=False, demoted_for_compression=False)
    with_children = runner._compose_busy_ack_message(_event(), 0.0, None, parent, **kwargs)
    without = runner._compose_busy_ack_message(_event(), 0.0, None, _Agent(), **kwargs)
    assert "subagent" not in with_children
    assert "subagent" not in without
