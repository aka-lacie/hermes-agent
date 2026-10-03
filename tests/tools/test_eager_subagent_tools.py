"""Eager children preserve tool permissions and never change a parent's cached surface."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from unittest.mock import patch

import json
import hermes_yaml as yaml
import pytest


@contextmanager
def _profile(home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    token = set_hermes_home_override(home)
    secrets = set_secret_scope({})
    try:
        yield
    finally:
        reset_secret_scope(secrets)
        reset_hermes_home_override(token)


def _config(home, eager, eager_tools=()):
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"context_length": 128000},
        "compression": {"enabled": False},
        "delegation": {"max_spawn_depth": 2, "orchestrator_enabled": True},
        "tools": {"tool_search": {
            "enabled": "on", "eager_subagents": eager,
            "eager_subagent_tools": list(eager_tools),
            "defer": ["terminal", "process_manage", "read_file", "write_file", "patch", "search_files"],
        }},
    }))


def _names(definitions):
    return {td["function"]["name"] for td in definitions}


@pytest.mark.parametrize("eager_all", [True, False], ids=["all", "selected"])
def test_profile_role_and_concurrent_cache_isolation(tmp_path, eager_all):
    import model_tools
    from agent.delegation_context import delegated_child_context
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from tools.registry import registry
    from tools.tool_search import BRIDGE_TOOL_NAMES, assemble_tool_defs, load_config

    homes = [tmp_path / "a", tmp_path / "b"]
    _config(homes[0], eager_all, ["read_file", "mcp_fixture_selected", "kanban_list", "missing_tool"])
    _config(homes[1], False)
    previous = is_multiplex_active()
    set_multiplex_active(True)
    try:
        for home, selected in ((homes[0], True), (homes[1], False), (homes[0], True)):
            with _profile(home):
                # A real scoped MCP-style registry entry, without a network transport.
                for name in ("mcp_fixture_lookup", "mcp_fixture_selected"):
                    registry.register(
                        name=name, toolset="mcp-fixture",
                        schema={"name": name, "description": "Look up a record",
                                "parameters": {"type": "object", "properties": {}}},
                        handler=lambda args, **kw: '{"ok": true}',
                        scope=registry.current_scope_key(), override=True,
                    )
                def definitions():
                    return model_tools.get_tool_definitions(
                        enabled_toolsets=["file", "mcp-fixture", "connections", "kanban"],
                        disabled_toolsets=["kanban"], quiet_mode=True,
                    )

                parent = definitions()
                assert {"read_file", "mcp_fixture_lookup", "mcp_fixture_selected"}.isdisjoint(_names(parent))
                assert BRIDGE_TOOL_NAMES <= _names(parent)
                def child():
                    with delegated_child_context():
                        return definitions()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    worker = pool.submit(copy_context().run, child)
                    assert definitions() == parent
                    child_names = _names(worker.result())
                assert ("read_file" in child_names) is selected
                assert ("mcp_fixture_selected" in child_names) is selected
                assert ("mcp_fixture_lookup" in child_names) is (selected and eager_all)
                assert ("write_file" in child_names) is (selected and eager_all)
                assert "missing_tool" not in child_names
                assert not any(n.startswith("kanban_") for n in child_names)
                assert (BRIDGE_TOOL_NAMES <= child_names) is not (selected and eager_all)
                # An enabled connector route retains its bridge alongside eager local tools.
                with delegated_child_context():
                    connected = assemble_tool_defs([
                        {"type": "function", "function": {"name": "manage_connections"}},
                        {"type": "function", "function": {"name": "read_file"}},
                    ])
                assert BRIDGE_TOOL_NAMES <= _names(connected.tool_defs)
                assert definitions() == parent
                assert load_config().eager_subagents is (selected and eager_all)
    finally:
        set_multiplex_active(previous)


@pytest.mark.parametrize("eager_all", [True, False], ids=["all", "selected"])
def test_real_delegation_constructs_eager_children_at_both_depths(tmp_path, monkeypatch, eager_all):
    import model_tools
    from agent.delegation_context import delegated_child_context
    from run_agent import AIAgent
    from tools.delegate_tool import _build_child_agent
    from tools.delegate_tool_toolsets import DELEGATE_BLOCKED_TOOLS
    from tools.tool_search import BRIDGE_TOOL_NAMES
    from tools.registry import registry

    home = tmp_path / "profile"
    work_tools = {"terminal", "process_manage", "read_file", "write_file", "patch", "search_files"}
    _config(home, eager_all, sorted(work_tools | DELEGATE_BLOCKED_TOOLS | {"kanban_list"}))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    agents = []
    with _profile(home), patch("agent.process_bootstrap.OpenAI"):
        registry.register(
            name="mcp_fixture_lookup", toolset="mcp-fixture",
            schema={"name": "mcp_fixture_lookup", "description": "Look up a record",
                    "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: '{"ok": true}',
            scope=registry.current_scope_key(), override=True,
        )
        try:
            parent = AIAgent(
                model="test/model", provider="custom", api_key="test",
                base_url="http://127.0.0.1:1/v1", quiet_mode=True,
                enabled_toolsets=["terminal", "file", "delegation", "kanban", "mcp-fixture"],
                disabled_toolsets=["kanban"], skip_context_files=True, skip_memory=True,
            )
            agents.append(parent)
            parent_tools = list(parent.tools)
            assert work_tools.isdisjoint(parent.valid_tool_names)
            assert BRIDGE_TOOL_NAMES <= parent.valid_tool_names
            current = parent
            for depth in (1, 2):
                child = _build_child_agent(
                    task_index=0, goal="Inspect a file", context=None, toolsets=None,
                    model=None, max_iterations=2, task_count=1, parent_agent=current,
                )
                agents.append(child)
                assert work_tools <= child.valid_tool_names
                assert ("mcp_fixture_lookup" in child.valid_tool_names) is eager_all
                assert (BRIDGE_TOOL_NAMES <= child.valid_tool_names) is not eager_all
                assert ("delegate_task" in child.valid_tool_names) is (depth == 1)
                assert child.valid_tool_names.isdisjoint(DELEGATE_BLOCKED_TOOLS - {"delegate_task"})
                assert not any(n.startswith("kanban_") for n in child.valid_tool_names)
                if not eager_all:
                    with delegated_child_context():
                        described = json.loads(model_tools.handle_function_call(
                            "tool_describe", {"names": ["mcp_fixture_lookup"]},
                            enabled_toolsets=child.enabled_toolsets,
                            disabled_toolsets=child.disabled_toolsets,
                        ))
                        assert "mcp_fixture_lookup" in described["tools"]
                        called = json.loads(model_tools.handle_function_call(
                            "tool_call", {"calls": [{"name": "mcp_fixture_lookup", "arguments": {}}]},
                            enabled_toolsets=child.enabled_toolsets,
                            disabled_toolsets=child.disabled_toolsets,
                        ))
                        assert called == {"ok": True}
                assert parent.tools == parent_tools
                current = child
        finally:
            for agent in reversed(agents):
                agent.close()


@pytest.mark.parametrize("value, expected", [
    ("terminal", set()),
    (True, set()),
    (None, set()),
    ({"terminal": True}, set()),
    ([" read_file ", "read_file", "", None, 123], {"read_file"}),
])
def test_eager_exceptions_require_a_list_of_tool_names(value, expected, caplog):
    """Malformed selective config must never silently turn on full eager loading."""
    from tools.tool_search import ToolSearchConfig

    with caplog.at_level("WARNING", logger="tools.tool_search"):
        config = ToolSearchConfig.from_raw({"eager_subagent_tools": value})
    assert config.eager_subagent_tools == expected
    assert config.eager_subagents is False
    if not isinstance(value, list):
        assert any("eager_subagent_tools" in record.message for record in caplog.records)
