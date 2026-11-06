"""A pinned webhook runtime builds the detached compression agents (dev2 -> dev3 parity review, P2).

``early_pinned_runtime`` marks a webhook route's pinned runtime with a private turn-route flag (the
cached worker on a fallback is rebuilt on its pinned primary). Session hygiene and manual
``/compress`` spread the resolved runtime straight into ``AIAgent``, whose constructor declares no
such keyword, so every compression of a pinned webhook session raised ``TypeError``. The flag stays
with the turn route; the constructor receives only what it declares.
"""
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import GatewayRunner
from hermes_state import SessionDB

PINNED_ROUTE = {"prompt": "{text}", "provider": "openai-codex", "model": "gpt-6.1-sol"}


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """``(runner, model, runtime)``: the real session runtime resolution of a pinned webhook session."""
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {
        "model": {"provider": "nous", "default": "profile-model"}})
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs",
                        lambda: (_ for _ in ()).throw(AssertionError("the pin resolves its own provider")))
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda provider, **kw: {
        "provider": provider, "api_key": "sk-pinned", "base_url": "https://codex.example/v1"})
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda *a: None)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("wh-1", source="webhook")
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"secret": "s", "routes": {"pr": dict(PINNED_ROUTE)}}))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._adapter_for_source = lambda source: adapter
    runner._session_db = db
    runner.session_store = SimpleNamespace(lookup_by_session_key=lambda key: SimpleNamespace(session_id="wh-1"))
    runner._resolve_session_key_or_none = lambda source, key: "k"
    source = adapter.build_source(chat_id="webhook:v2:x", user_id="webhook:pr", chat_type="webhook")
    model, runtime = runner._resolve_session_agent_runtime(user_config={}, source=source, session_key="k")
    # The compression builders read the live prompt through the async store facade.
    runner._session_db = SimpleNamespace(get_session=AsyncMock(return_value=None), _db=db)
    yield SimpleNamespace(runner=runner, model=model, runtime=runtime)
    db.close()


@pytest.fixture
def constructed(monkeypatch):
    """``AIAgent`` replaced by a recorder holding it to the real constructor's keyword contract."""
    from run_agent import AIAgent
    signature = inspect.signature(AIAgent.__init__)
    built = []

    class Recorder:
        def __init__(self, **kwargs):
            signature.bind(self, **kwargs)  # TypeError on a keyword the real constructor does not declare
            built.append(kwargs)
    monkeypatch.setattr("run_agent.AIAgent", Recorder)
    monkeypatch.setattr("gateway.run._seed_hygiene_system_prompt", lambda agent, row: False)
    return built


def test_the_pinned_route_still_reaches_the_turn_route(pinned):
    assert (pinned.model, pinned.runtime["provider"]) == ("gpt-6.1-sol", "openai-codex")
    assert pinned.runner._resolve_turn_agent_config("", pinned.model, pinned.runtime)["webhook_runtime_pinned"] is True


@pytest.mark.asyncio
async def test_session_hygiene_builds_its_agent_on_the_pinned_runtime(pinned, constructed):
    pinned.runtime["_webhook_future_marker"] = "private"
    await GatewayRunner._hmwa_hygiene_build_agent(pinned.runner, pinned.model, pinned.runtime,
                                                  SimpleNamespace(session_id="wh-1"))
    assert len(constructed) == 1
    assert (constructed[0]["provider"], constructed[0]["model"], constructed[0]["api_key"]) == (
        "openai-codex", "gpt-6.1-sol", "sk-pinned")
    # Building the agent leaves the resolved runtime (and its turn-route flag) untouched.
    assert pinned.runner._resolve_turn_agent_config("", pinned.model, pinned.runtime)["webhook_runtime_pinned"] is True


@pytest.mark.asyncio
async def test_manual_compress_builds_its_agent_on_the_pinned_runtime(pinned, constructed):
    # ``_run_manual_compression`` adds these before it builds the agent.
    runtime = {**pinned.runtime, "platform": "webhook", "gateway_session_key": "k", "reasoning_config": None}
    agent = await GatewayRunner._build_manual_compression_agent(pinned.runner, "wh-1", pinned.model, runtime)
    assert agent._end_session_on_close is False
    assert len(constructed) == 1
    assert (constructed[0]["provider"], constructed[0]["model"], constructed[0]["gateway_session_key"]) == (
        "openai-codex", "gpt-6.1-sol", "k")
