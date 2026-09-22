"""Delivery receipts cross tool threads but never turn/profile boundaries."""
import asyncio
from contextlib import nullcontext
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from gateway.response_delivery import (
    response_delivery_scope, record_response_delivery, capture_response_delivery,
)
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.mark.asyncio
async def test_delivery_receipts_follow_actual_turn_and_profile(tmp_path, monkeypatch):
    from agent import secret_scope
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    source = SimpleNamespace(platform="discord", chat_id="123", thread_id=None)
    target = dict(platform="discord", chat_id="123")
    assert not record_response_delivery(**target)
    late = None
    for home in (tmp_path / "a", tmp_path / "b", tmp_path / "a"):
        token = set_hermes_home_override(str(home))
        try:
            with response_delivery_scope(source):
                empty = {}
                capture_response_delivery(empty)
                assert not empty["user_visible_response"]
                if late:
                    assert not late.run(record_response_delivery, **target)
                assert not record_response_delivery(platform="discord", chat_id="other")
                assert not record_response_delivery(**target, thread_id="other-thread")
                other = set_hermes_home_override(str(tmp_path / "other"))
                try:
                    assert not record_response_delivery(**target)
                finally:
                    reset_hermes_home_override(other)
                from agent.delegation_context import delegated_child_context
                with delegated_child_context():
                    assert not record_response_delivery(**target)
                # Same propagation mechanism as the gateway executor and tool workers.
                assert await asyncio.to_thread(record_response_delivery, **target)
                result = {}
                capture_response_delivery(result)
                assert result["user_visible_response"]
                with response_delivery_scope(source):
                    child = {}
                    capture_response_delivery(child)
                    assert not child["user_visible_response"]
                late = copy_context()
            assert not late.run(record_response_delivery, **target)
        finally:
            reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_gateway_wrapper_propagates_delivery_and_resets_between_invocations(monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig
    source = SimpleNamespace(platform="discord", chat_id="123", thread_id=None)
    runner = GatewayRunner(GatewayConfig())
    monkeypatch.setattr(runner, "_profile_scope_for_source", lambda source: nullcontext())
    async def run_inner(message, *args, **kwargs):
        if message == "react":
            assert await asyncio.to_thread(record_response_delivery, platform="discord", chat_id="123")
        result = {"final_response": "NO_REPLY", "failed": False}
        capture_response_delivery(result)
        return result
    monkeypatch.setattr(runner, "_run_agent_inner", run_inner)
    for message, expected in (("react", True), ("new turn", False), ("react", True)):
        result = await runner._run_agent(message, "", [], source, "session")
        assert result["user_visible_response"] is expected
    assert not record_response_delivery(platform="discord", chat_id="123")
