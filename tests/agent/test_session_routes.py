"""Stored session routes refresh credentials without silently changing endpoints (agent/session_routes.py).

Ported from the retired tools/delegate_session_routes.py tests: the same identity rules now apply to the
credential-free ``sessions.route`` every spawn, resume, webhook and cron re-fire resolves through.
"""

import json
from types import SimpleNamespace

import pytest

from agent import session_routes as routes
from tools import delegate_tool_config as config


def _agent(endpoint="https://private.example/v1", key="initial-key", provider="custom"):
    return SimpleNamespace(base_url=endpoint, api_key=key, provider=provider, requested_provider=provider,
                           model="selected-model", api_mode="chat_completions", reasoning_config=None)


def test_direct_route_uses_current_credentials_and_refuses_changed_endpoint(monkeypatch):
    selected = {"base_url": "https://private.example/v1", "api_key": "initial-key", "model": "selected-model"}
    cfg = selected
    monkeypatch.setattr(config, "_load_config", lambda: cfg)
    parent = _agent("https://parent.example/v1", provider="openai")
    route = routes.capture_agent_route(_agent(), routing_cfg=selected)
    encoded = json.dumps(route)
    assert "initial-key" not in encoded and "private.example" not in encoded
    selected["api_key"] = "rotated-key"
    resumed = routes.resolve_route(route, parent, model="another-model")
    resolved = config._resolve_delegation_credentials(resumed, parent)
    assert resolved["api_key"] == "rotated-key"
    assert resolved["model"] == "another-model"
    assert resolved["base_url"] == selected["base_url"]
    selected["base_url"] = "https://different.example/v1"
    with pytest.raises(ValueError, match="no longer matches"):
        routes.resolve_route(route, parent)


def test_parent_route_does_not_adopt_new_delegation_default(monkeypatch):
    cfg = {}
    monkeypatch.setattr(config, "_load_config", lambda: cfg)
    parent = _agent()
    route = routes.capture_agent_route(_agent(), routing_cfg=cfg)
    cfg.update(base_url="https://new-default.example/v1", api_key="wrong-key")
    parent.api_key = "rotated-parent-key"
    assert routes.resolve_route(route, parent) == {"model": "selected-model"}
    parent.provider = parent.requested_provider = "openai"
    with pytest.raises(ValueError, match="provider has changed"):
        routes.resolve_route(route, parent)
    parent.provider = parent.requested_provider = "custom"
    parent.base_url = "https://another-parent.example/v1"
    with pytest.raises(ValueError, match="no longer matches"):
        routes.resolve_route(route, parent)


def test_named_provider_keeps_its_identity_and_chain_when_defaults_change(monkeypatch):
    cfg = {"provider": "chosen-profile", "model": "selected-model"}
    monkeypatch.setattr(config, "_load_config", lambda: cfg)
    route = routes.capture_agent_route(
        _agent(provider="chosen-profile"),
        routing_cfg={**cfg, "fallback_providers": [{"provider": "backup", "model": "b", "api_key": "x"}]})
    cfg.update(provider="different-profile", base_url="https://wrong.example", api_key="wrong-key")
    resumed = routes.resolve_route(route, _agent(), model="new-model")
    assert resumed["provider"] == "chosen-profile"
    assert resumed["model"] == "new-model"
    assert resumed["fallback_providers"] == [{"provider": "backup", "model": "b"}]  # never [] on resume
    assert "base_url" not in resumed and "api_key" not in resumed


def test_explicitly_disabled_chain_stays_disabled_and_absent_inherits(monkeypatch):
    monkeypatch.setattr(config, "_load_config", lambda: {"fallback_providers": [{"provider": "inh", "model": "i"}]})
    disabled = routes.capture_agent_route(_agent(provider="p"), routing_cfg={"fallback_providers": []})
    assert routes.resolve_route(disabled, _agent())["fallback_providers"] == []
    inherited = routes.capture_agent_route(_agent(provider="p"), routing_cfg={})
    assert routes.resolve_route(inherited, _agent())["fallback_providers"] == [{"provider": "inh", "model": "i"}]


def test_external_route_captures_actual_endpoint_and_refuses_unrelated_default(monkeypatch):
    cfg = {"provider": "different-profile", "model": "some-model"}
    monkeypatch.setattr(config, "_load_config", lambda: cfg)
    actual = {"base_url": "https://private.example/v1", "api_key": "aux-key"}
    route = routes.capture_agent_route(_agent(), routing_cfg=actual)
    assert route["kind"] == "default"
    cfg.clear()
    cfg.update(base_url="https://wrong.example/v1", api_key="wrong-key")
    with pytest.raises(ValueError, match="no longer matches"):
        routes.resolve_route(route, _agent())


def test_endpoint_identity_normalizes_equivalent_urls_but_keeps_route_path():
    assert routes.endpoint_fingerprint("https://USER:SECRET@EXAMPLE.com:443/v1/") == routes.endpoint_fingerprint("https://example.com/v1")
    assert routes.endpoint_fingerprint("https://example.com/v1") != routes.endpoint_fingerprint("https://example.com/v2")


def test_stored_routes_refuse_credentials_and_unknown_shapes():
    for bad in ({"kind": "named", "api_key": "k"}, {"kind": "named", "base_url": "https://x"}, {"kind": "nope"},
                {"kind": "named", "fallback_providers": [{"provider": "p"}]},
                {"kind": "named", "fallback_providers": [{"provider": "p", "model": "m"}] * 9}):
        with pytest.raises(routes.RouteError):
            routes.validate_route(bad)
