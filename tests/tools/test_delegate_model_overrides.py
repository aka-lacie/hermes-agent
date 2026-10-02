"""Explicit task routes reach real children without leaking another route or profile."""

import json
from unittest.mock import patch

import pytest
import hermes_yaml as yaml


def test_task_pins_through_registry_and_real_profile_provider_resolution(tmp_path, monkeypatch):
    from agent.secret_scope import set_multiplex_active
    from gateway.run import _profile_runtime_scope
    from run_agent import AIAgent
    from tools import delegate_tool as dt
    from tools.delegate_tool_results import _child_route_metadata
    from tools.registry import registry

    homes = [tmp_path / "a", tmp_path / "b"]
    for i, home in enumerate(homes):
        home.mkdir()
        (home / ".env").write_text(f"REVIEW_KEY=test-review-{i}\n")
        (home / "config.yaml").write_text(yaml.safe_dump({
            "compression": {"enabled": False}, "terminal": {"cwd": str(home)},
            "providers": {"review-route": {
                "base_url": f"http://127.0.0.1:{10000 + i}/v1", "key_env": "REVIEW_KEY",
                "api_mode": "codex_responses", "extra_body": {"temperature": 0.2},
            }},
            "delegation": {
                "model": f"default-{i}", "base_url": "http://127.0.0.1:1/v1", "api_key": "test-default",
                "request_overrides": {"temperature": 0.9}, "api_mode": "chat_completions",
                "fallback_providers": [{"provider": "custom", "model": "route-fallback"}],
                "reasoning_effort": "high", "max_concurrent_children": 4, "max_spawn_depth": 2,
                "difficulty_routes": {"easy": {"model": f"easy-{i}", "reasoning_effort": "low"}},
            },
        }))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    built = []

    def capture(batch, background):
        built.append(batch)
        return json.dumps({"routes": [_child_route_metadata(c) for _, _, c in batch.children]})

    monkeypatch.setattr(dt, "_run_batch", capture)  # only stop before network execution
    set_multiplex_active(True)
    try:
        with patch("agent.process_bootstrap.OpenAI"):
            for i in (0, 1, 0):
                with _profile_runtime_scope(homes[i]):
                    original = (homes[i] / "config.yaml").read_bytes()
                    parent = AIAgent(
                        model="parent-model", provider="custom", base_url="http://127.0.0.1:2/v1",
                        api_key="test-parent", request_overrides={"temperature": 0.7},
                        enabled_toolsets=["file", "delegation"], skip_memory=True, quiet_mode=True,
                    )
                    start = len(built)
                    try:
                        response = json.loads(registry.dispatch("delegate_task", {"tasks": [
                            {"goal": "Check the default route without overrides"},
                            {"goal": "Check difficulty routing without overrides", "difficulty": "easy"},
                            {"goal": "Check a model on the selected route", "difficulty": "easy", "model": " pinned-model "},
                            {"goal": "Check a different configured provider", "difficulty": "easy",
                             "model": "review-model", "provider": " review-route "},
                        ]}, parent_agent=parent))
                        assert "error" not in response, response
                        batch = built[-1]
                        children = [c for _, _, c in batch.children]
                        default, easy, pinned, switched = children
                        assert [c.model for c in children] == [f"default-{i}", f"easy-{i}", "pinned-model", "review-model"]
                        assert pinned.provider == easy.provider == default.provider
                        assert pinned.base_url == easy.base_url == default.base_url
                        assert pinned.api_key == easy.api_key == "test-default"
                        assert pinned.request_overrides == easy.request_overrides == {"temperature": 0.9}
                        assert pinned._fallback_chain == easy._fallback_chain
                        assert switched.base_url == f"http://127.0.0.1:{10000 + i}/v1"
                        assert switched.api_key == f"test-review-{i}"
                        assert switched.requested_provider == "review-route"
                        assert switched.api_mode == "codex_responses"
                        assert switched.request_overrides == {"extra_body": {"temperature": 0.2}}
                        assert not switched._fallback_chain
                        assert switched.acp_command is None
                        assert switched.reasoning_config["effort"] == pinned.reasoning_config["effort"] == "low"
                        assert parent.model == "parent-model" and parent.api_key == "test-parent"
                        assert parent.request_overrides == {"temperature": 0.7}
                        schema = dt._build_dynamic_schema_overrides()["parameters"]["properties"]["tasks"]
                        assert {"model", "provider", "difficulty"} <= schema["items"]["properties"].keys()
                        manifest = json.loads((homes[i] / "cache/delegation/live" / batch.live_deleg_id / "manifest.json").read_text())
                        assert manifest["tasks"][3]["requested_model"] == "review-model"
                        assert manifest["tasks"][3]["requested_provider"] == "review-route"
                        assert response["routes"][3]["requested_model"] == "review-model"
                        assert response["routes"][3]["requested_provider"] == "review-route"
                        # A child's own pin does not leak into its next delegation.
                        nested = json.loads(registry.dispatch("delegate_task", {"tasks": [
                            {"goal": "Check the nested default route"},
                            {"goal": "Check another model on the nested default", "model": "nested-model"},
                        ]}, parent_agent=switched))
                        assert "error" not in nested, nested
                        leaves = [c for _, _, c in built[-1].children]
                        assert [c.model for c in leaves] == [f"default-{i}", "nested-model"]
                        assert all(c.api_key == "test-default" for c in leaves)
                        # With no delegation defaults, model-only inherits the live parent connection.
                        with monkeypatch.context() as isolated:
                            isolated.setattr(dt, "_load_config", lambda: {})
                            inherited = json.loads(registry.dispatch("delegate_task", {"tasks": [
                                {"goal": "Check a model on the parent route", "model": "parent-route-model"},
                            ]}, parent_agent=parent))
                        assert "error" not in inherited, inherited
                        inherited_child = built[-1].children[0][2]
                        assert inherited_child.model == "parent-route-model"
                        assert inherited_child.provider == parent.provider
                        assert inherited_child.base_url == parent.base_url
                        assert inherited_child.api_key == parent.api_key
                        assert inherited_child.request_overrides == parent.request_overrides
                        assert (homes[i] / "config.yaml").read_bytes() == original
                    finally:
                        for batch in reversed(built[start:]):
                            for _, _, child in reversed(batch.children):
                                child.close()
                        parent.close()
    finally:
        set_multiplex_active(False)


@pytest.mark.parametrize("pins, message", [
    ({"model": value}, "model must be a non-empty string") for value in (None, "", "  ", 1, False, {})
] + [
    ({"model": "test-model", "provider": value}, "provider must be a non-empty string")
    for value in (None, "", "  ", 1, False, {})
] + [
    ({"provider": "openrouter"}, "provider requires an explicit model"),
    ({"model": "test-model", "provider": "AUTO"}, "specific provider"),
    ({"model": "test-model", "provider": "nonexistent-test-provider"}, "Cannot resolve delegation provider"),
])
def test_invalid_task_pin_refuses_whole_batch_before_construction(monkeypatch, pins, message):
    from tools import delegate_tool as dt
    from tools.registry import registry
    from tests.tools.test_delegate import _make_mock_parent

    monkeypatch.setattr(dt, "_load_config", lambda: {
        "model": "default", "base_url": "http://127.0.0.1:1/v1", "api_key": "test-default",
    })
    with patch.object(dt, "_build_children") as build:
        response = json.loads(registry.dispatch("delegate_task", {"tasks": [
            {"goal": "Perform the valid first assignment", "model": "valid-model"},
            {"goal": "Perform the invalid second assignment", **pins},
        ]}, parent_agent=_make_mock_parent()))
    assert message in response["error"]
    build.assert_not_called()
