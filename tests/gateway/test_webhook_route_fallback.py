"""Webhook routes may pin provider/model and declare ``fallback_providers`` (plan §4.3).

The credential-free route lands in ``sessions.route`` once and every turn of that session reads it
back: the first delivery's agent and a keyed session's next delivery get the same primary and chain,
whatever the route config says later. The live wire (content-filter block on the pinned primary,
failover onto the declared chain, again after a restart) is test_webhook_session_keys_e2e.py.
"""
import json
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms import webhook_route_runtime as runtime_mod
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import GatewayRunner
from hermes_cli.session_route import declared_route, rehydrate_chain, route_fallback_chain, route_pinned
from hermes_state import SessionDB

PROFILE_CHAIN = [{"provider": "openrouter", "model": "z-ai/glm-5.2"}]
DECLARED = [{"provider": "cliproxy-claude", "model": "claude-opus-5-5", "key_env": "CLIPROXY_KEY"}]
STORED = [{"provider": "cliproxy-claude", "model": "claude-opus-5-5"}]
PINNED_ROUTE = {"prompt": "{text}", "provider": "openai-codex", "model": "gpt-6.1-sol", "fallback_providers": DECLARED}


@pytest.fixture
def webhook_turn(tmp_path):
    """A turn of webhook session ``wh-1`` on route ``pr`` over a real SessionDB."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("wh-1", source="webhook")
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"secret": "s", "routes": {"pr": dict(PINNED_ROUTE)}}))
    runner = SimpleNamespace(
        _session_db=db, _adapter_for_source=lambda source: adapter, _refresh_fallback_model=lambda: list(PROFILE_CHAIN),
        session_store=SimpleNamespace(lookup_by_session_key=lambda key: SimpleNamespace(session_id="wh-1")),
        _pre_agent_fallback_notice=None)
    source = adapter.build_source(chat_id="webhook:v2:x", user_id="webhook:pr", chat_type="webhook")
    yield SimpleNamespace(db=db, adapter=adapter, runner=runner, source=source)
    db.close()


def _stored(db, sid="wh-1"):
    raw = db._read_one("SELECT route FROM sessions WHERE id=?", (sid,))["route"]
    return json.loads(raw) if raw else None


def _build_agent(turn_env):
    """``AIAgent`` kwargs of the turn's fresh agent (``GatewaySessionAgentMixin._build_fresh_agent``)."""
    from gateway.session_execution import GatewaySessionAgentMixin
    built = {}
    turn = GatewaySessionAgentMixin.__new__(GatewaySessionAgentMixin)
    turn._runner = turn_env.runner
    turn_env.runner._prefill_messages, turn_env.runner._service_tier = None, None
    turn._ctx = SimpleNamespace(source=turn_env.source, user_config={}, enabled_toolsets=[], disabled_toolsets=None,
                                session_id="wh-1", session_key="k", AIAgent=lambda **kw: built.update(kw))
    turn._build_fresh_agent({"model": "m", "runtime": {}}, "webhook", None, 1, None, {}, False)
    return turn, built


def test_declared_chain_reaches_the_first_agent_and_is_stored_credential_free(webhook_turn):
    _turn, built = _build_agent(webhook_turn)
    assert built["fallback_model"] == DECLARED  # key_env re-attached from the live route config
    assert _stored(webhook_turn.db) == {"provider": "openai-codex", "model": "gpt-6.1-sol", "fallback_providers": STORED}


def test_next_delivery_keeps_the_stored_route_after_a_config_edit(webhook_turn):
    _build_agent(webhook_turn)
    webhook_turn.adapter._routes["pr"] = {"prompt": "{text}", "model": "other"}  # chain removed from config
    turn, built = _build_agent(webhook_turn)
    assert built["fallback_model"] == STORED  # stored route wins; no credential reference left to attach
    agent = SimpleNamespace(_fallback_chain=[], _fallback_model=None, _fallback_activated=False)
    GatewayRunner._apply_fallback_chain_to_agent(agent, turn._turn_fallback_chain())  # cached-agent refresh
    assert agent._fallback_chain == STORED


@pytest.mark.parametrize(("route", "expected"), [
    ({"prompt": "x"}, PROFILE_CHAIN),                                   # declares nothing: as before
    ({"prompt": "x", "model": "m"}, None),                              # pinned: never the profile chain
    ({"prompt": "x", "model": "m", "fallback_providers": []}, None),
    ({"prompt": "x", "fallback_providers": DECLARED}, DECLARED),        # unpinned with its own chain
    ({"prompt": "x", "fallback_providers": []}, None),
])
def test_route_chain_matrix(webhook_turn, route, expected):
    webhook_turn.adapter._routes["pr"] = route
    _turn, built = _build_agent(webhook_turn)
    assert built["fallback_model"] == expected


def test_non_webhook_turns_never_touch_the_store():
    runner = SimpleNamespace(_refresh_fallback_model=lambda: PROFILE_CHAIN)  # no _session_db at all
    source = SimpleNamespace(platform=None, user_id="u")
    assert runtime_mod.turn_fallback_chain(runner, source, "sid", runner._refresh_fallback_model) == PROFILE_CHAIN
    assert runtime_mod.pinned_runtime(runner, source, "k", "model", {"provider": "p"}) == ("model", {"provider": "p"})


def test_pinned_runtime_resolves_the_route_provider(webhook_turn, monkeypatch):
    seen = []

    def resolve(provider, target_model=None):
        seen.append((provider, target_model))
        return {"provider": provider, "api_key": "k", "base_url": "https://codex.example/v1"}
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", resolve)
    model, kwargs = runtime_mod.pinned_runtime(webhook_turn.runner, webhook_turn.source, "k", "profile-model",
                                               {"provider": "nous"})
    assert (model, kwargs["provider"], seen) == ("gpt-6.1-sol", "openai-codex", [("openai-codex", "gpt-6.1-sol")])
    assert webhook_turn.runner._pre_agent_fallback_notice is None


def test_pinned_runtime_walks_the_declared_chain_when_the_primary_cannot_resolve(webhook_turn, monkeypatch):
    def unavailable(provider, target_model=None):
        raise RuntimeError("No Codex credentials stored")
    requests = []

    def resolve_runtime_provider(**kwargs):
        requests.append(kwargs)
        return {"provider": kwargs["requested"], "api_key": kwargs.get("explicit_api_key"), "base_url": "https://c/v1"}
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", unavailable)
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve_runtime_provider)
    monkeypatch.setenv("CLIPROXY_KEY", "sk-cliproxy")
    model, kwargs = runtime_mod.pinned_runtime(webhook_turn.runner, webhook_turn.source, "k", "profile-model", {})
    assert (model, kwargs["provider"]) == ("claude-opus-5-5", "cliproxy-claude")
    assert requests[0]["requested"] == "cliproxy-claude" and requests[0]["target_model"] == "claude-opus-5-5"
    assert "unavailable" in webhook_turn.runner._pre_agent_fallback_notice


def test_pinned_runtime_without_a_chain_fails_like_any_pinned_turn(webhook_turn, monkeypatch):
    webhook_turn.adapter._routes["pr"] = {"prompt": "x", "provider": "openai-codex", "model": "gpt-6.1-sol"}

    def unavailable(provider, target_model=None):
        raise RuntimeError("No Codex credentials stored")
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", unavailable)
    with pytest.raises(RuntimeError, match="No Codex credentials"):
        runtime_mod.pinned_runtime(webhook_turn.runner, webhook_turn.source, "k", "profile-model", {})


# ── the shared credential-free route ────────────────────────────────────────


def test_declared_route_is_credential_free_and_capped():
    chain = [{"provider": f"p{i}", "model": "m", "api_key": "sk", "key_env": "K"} for i in range(10)]
    route = declared_route(provider="openai-codex", model=" gpt ", base_url="https://x/v1/", fallback_providers=chain)
    assert route["model"] == "gpt" and "base_url" not in route and len(route["endpoint_sha256"]) == 64
    assert len(route["fallback_providers"]) == 8
    assert all(set(e) == {"provider", "model"} for e in route["fallback_providers"])
    assert declared_route() is None and declared_route(fallback_providers=None) is None
    assert declared_route(fallback_providers=[]) == {"fallback_providers": []}
    assert route_pinned(route) and not route_pinned({"fallback_providers": []}) and not route_pinned(None)


def test_rehydrate_takes_credentials_only_from_matching_current_entries():
    stored = [{"provider": "a", "model": "m"}, {"provider": "b", "model": "n"}]
    current = [{"provider": "a", "model": "m", "key_env": "A_KEY"}, {"provider": "c", "model": "o", "api_key": "x"}]
    assert rehydrate_chain(stored, current) == [{"provider": "a", "model": "m", "key_env": "A_KEY"},
                                                {"provider": "b", "model": "n"}]
    assert route_fallback_chain({"model": "m"}, PROFILE_CHAIN, owner="t") is None
    assert route_fallback_chain({}, PROFILE_CHAIN, owner="t") == PROFILE_CHAIN
