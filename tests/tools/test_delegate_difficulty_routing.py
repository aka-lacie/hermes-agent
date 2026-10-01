"""Per-task routes reach real children without changing parent, siblings, or another profile."""

import json
from copy import deepcopy
from unittest.mock import patch

import pytest
import hermes_yaml as yaml


def test_profile_routes_through_registry_and_real_children(tmp_path, monkeypatch):
    from agent.secret_scope import set_multiplex_active
    from gateway.run import _profile_runtime_scope
    from run_agent import AIAgent
    from tools import delegate_tool as dt
    from tools.delegate_tool_results import _child_route_metadata
    from tools.registry import registry

    homes = [tmp_path / "a", tmp_path / "b"]
    for i, home in enumerate(homes):
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({
            "compression": {"enabled": False},
            "terminal": {"cwd": str(home)},
            "delegation": {
                "model": f"default-{i}", "base_url": "http://127.0.0.1:1/v1", "api_key": "test-only",
                "reasoning_effort": "high", "max_concurrent_children": 4,
                "max_spawn_depth": 2, "orchestrator_enabled": True,
                "difficulty_routes": {
                    "easy": {"model": f"easy-{i}", "reasoning_effort": "max"},
                    "medium": {},
                    "hard": {"model": f"hard-{i}", "reasoning_effort": "high"},
                },
            },
        }))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    built = []

    def capture(batch, background):
        built.append(batch)
        return json.dumps({"routes": [_child_route_metadata(c) for _, _, c in batch.children]})

    monkeypatch.setattr(dt, "_run_batch", capture)  # stop only before the actual provider calls
    set_multiplex_active(True)
    try:
        with patch("agent.process_bootstrap.OpenAI"):
            for i in (0, 1, 0):
                with _profile_runtime_scope(homes[i]):
                    original = (homes[i] / "config.yaml").read_bytes()
                    parent = AIAgent(
                        model="parent-model", provider="custom", base_url="http://127.0.0.1:1/v1", api_key="test-only",
                        reasoning_config={"enabled": True, "effort": "low"},
                        enabled_toolsets=["file", "delegation"], skip_memory=True, quiet_mode=True,
                    )
                    start = len(built)
                    try:
                        tasks = [{"goal": "Inspect a bounded component carefully", **({"difficulty": d} if d else {})}
                                 for d in ("easy", "medium", "hard", None)]
                        response = json.loads(registry.dispatch("delegate_task", {"tasks": tasks}, parent_agent=parent))
                        assert "error" not in response, response
                        batch = built[-1]
                        children = [c for _, _, c in batch.children]
                        assert [c.model for c in children] == [f"easy-{i}", f"default-{i}", f"hard-{i}", f"default-{i}"]
                        assert [c.reasoning_config["effort"] for c in children] == ["max", "high", "high", "high"]
                        assert parent.model == "parent-model" and parent.reasoning_config["effort"] == "low"
                        assert all("delegate_task" in c.valid_tool_names for c in children)
                        schema = dt._build_dynamic_schema_overrides()["parameters"]["properties"]["tasks"]
                        assert set(schema["items"]["properties"]["difficulty"]["enum"]) == {"easy", "medium", "hard"}
                        # An easy child can choose hard later; omitted difficulty returns to the profile default.
                        nested = json.loads(registry.dispatch("delegate_task", {"tasks": [
                            {"goal": "Investigate newly discovered complexity", "difficulty": "hard"},
                            {"goal": "Check an ordinary follow-up component"},
                        ]}, parent_agent=children[0]))
                        assert "error" not in nested, nested
                        leaves = [c for _, _, c in built[-1].children]
                        assert [c.model for c in leaves] == [f"hard-{i}", f"default-{i}"]
                        assert all("delegate_task" not in c.valid_tool_names for c in leaves)
                        assert children[0].model == f"easy-{i}" and children[0].reasoning_config["effort"] == "max"
                        assert (homes[i] / "config.yaml").read_bytes() == original
                        # The result distinguishes a fallback from the requested route.
                        children[0].model = "fallback-model"
                        from tools.delegate_tool_child_run import _fabricated_entry
                        entry = _fabricated_entry(0, "error", "provider stopped", children[0])
                        assert entry["model"] == "fallback-model"
                        assert entry["requested_model"] == f"easy-{i}" and entry["difficulty"] == "easy"
                        manifest = homes[i] / "cache/delegation/live" / batch.live_deleg_id / "manifest.json"
                        recorded = json.loads(manifest.read_text())
                        assert recorded["model"] is None  # a mixed batch has no single model
                        assert recorded["tasks"][0]["requested_model"] == f"easy-{i}"
                    finally:
                        for batch in reversed(built[start:]):
                            for _, _, child in reversed(batch.children):
                                child.close()
                        parent.close()
    finally:
        set_multiplex_active(False)


@pytest.mark.parametrize("difficulty", ["unknown", None, 1, {}, "HARD"])
def test_invalid_level_refuses_entire_batch_before_construction(monkeypatch, difficulty):
    from tools import delegate_tool as dt
    from tools.registry import registry
    from tests.tools.test_delegate import _make_mock_parent

    monkeypatch.setattr(dt, "_load_config", lambda: {"difficulty_routes": {"easy": {}}})
    with patch.object(dt, "_build_children") as build:
        response = json.loads(registry.dispatch("delegate_task", {"tasks": [
            {"goal": "Perform the valid first assignment", "difficulty": "easy"},
            {"goal": "Perform the invalid second assignment", "difficulty": difficulty},
        ]}, parent_agent=_make_mock_parent()))
    assert "difficulty" in response["error"]
    build.assert_not_called()


def test_route_validation_and_copies_preserve_default_and_transport():
    from tools.delegate_tool_config import _task_route_configs

    cfg = {"model": "default", "reasoning_effort": "high", "difficulty_routes": {
        "easy": {"model": "small", "reasoning_effort": False},
        "hard": {"model": "large", "provider": "anthropic", "fallback_providers": [],
                 "request_overrides": {"extra_body": {"value": 1}}},
    }}
    original = deepcopy(cfg)
    routes = _task_route_configs([{}, {"difficulty": "easy"}, {"difficulty": "hard"}], cfg)
    assert routes[0]["model"] == "default" and routes[1]["reasoning_effort"] is False
    assert routes[2]["provider"] == "anthropic" and routes[2]["fallback_providers"] == []
    routes[2]["request_overrides"]["extra_body"]["value"] = 2
    assert cfg == original
    with pytest.raises(ValueError, match="no configured"):
        _task_route_configs([{"difficulty": "medium"}], cfg)
    cfg["difficulty_routes"]["hard"]["reasoning_effort"] = "typo"
    with pytest.raises(ValueError, match="reasoning_effort"):
        _task_route_configs([{"difficulty": "hard"}], cfg)


@pytest.mark.parametrize("setting,value", [("command", "/missing/route-command"), ("args", ["--route"])])
def test_unsupported_transport_route_refuses_batch_before_construction(monkeypatch, setting, value):
    from tools import delegate_tool as dt
    from tools.registry import registry
    from tests.tools.test_delegate import _make_mock_parent

    monkeypatch.setattr(dt, "_load_config", lambda: {
        "difficulty_routes": {"easy": {}, "hard": {setting: value}},
    })
    with patch.object(dt, "_build_children") as build:
        response = json.loads(registry.dispatch("delegate_task", {"tasks": [
            {"goal": "Perform the valid first assignment", "difficulty": "easy"},
            {"goal": "Perform the unsupported second assignment", "difficulty": "hard"},
        ]}, parent_agent=_make_mock_parent()))
    assert "Unsupported delegation route settings" in response["error"]
    assert setting in response["error"]
    build.assert_not_called()
