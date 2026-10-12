"""Real plugin/tool executor checks for code policy and public cell registration."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest


PLUGIN = '''
import json
import threading
import time
EVENTS = []
ENTERED = threading.Event()
RELEASE = threading.Event()
def record(kind, **kw):
    EVENTS.append((kind, kw))
def register(ctx):
    ctx.register_tool(name="code_fixture", toolset="code-fixture", schema={
        "name": "code_fixture", "description": "Test echo", "parameters": {
        "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}},
        handler=lambda args, **kw: json.dumps({"echo": args["value"]}))
    for kind in ("pre_tool_call", "post_tool_call", "transform_tool_result"):
        ctx.register_hook(kind, lambda _kind=kind, **kw: record(_kind, **kw))
    ctx.register_middleware("tool_request", lambda **kw: record("request", **kw))
    def execution(next_call, **kw):
        record("execution", **kw)
        return next_call(kw["args"])
    ctx.register_middleware("tool_execution", execution)
    def cell(args, context):
        ENTERED.set()
        if args.get("wait"):
            RELEASE.wait(3)
        else:
            time.sleep(0.1)
        return {"session": context.call_context.agent.session_id,
                "history": context.call_context.fork_history(),
                "tracked": threading.get_ident() in context.call_context.agent._tool_worker_threads,
                "cell_id": context.authority.cell_id}
    ctx.register_cell_entry_point("plugin_cell_fixture", {
        "name": "plugin_cell_fixture", "description": "Public cell fixture", "parameters": {
        "type": "object", "properties": {"wait": {"type": "boolean"}}}}, cell)
'''


@pytest.fixture
def host(tmp_path, monkeypatch):
    from hermes_cli import plugins
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    root = tmp_path / "plugins" / "code-fixture"
    root.mkdir(parents=True)
    (root / "plugin.yaml").write_text("name: code-fixture\nversion: 0.1.0\n")
    (root / "__init__.py").write_text(PLUGIN)
    config = {"compression": {"enabled": False}, "plugins": {"enabled": ["code-fixture"]},
              "code_execution": {"mode": "project", "timeout": 10, "workflow_timeout": 10}}
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **kw: config)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda *a, **kw: config)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    from run_agent import AIAgent
    with patch("agent.process_bootstrap.OpenAI"):
        parent = AIAgent(api_key="fixture", base_url="http://127.0.0.1:1/v1", model="fixture", provider="custom",
                         enabled_toolsets=[], quiet_mode=True, skip_memory=True, skip_context_files=True,
                         session_id="code-owner")
    parent.valid_tool_names = {"execute_code", "code_fixture", "plugin_cell_fixture"}
    parent.enabled_toolsets = ["code_execution", "code-fixture"]
    module = manager._plugins["code-fixture"].module
    assert module is not None, manager._plugins["code-fixture"].error

    def invoke(name, args, call_id="outer", messages=None):
        messages = messages if messages is not None else []
        call = SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        from agent.subagent_context import bind_fork_source
        with bind_fork_source(parent, messages):
            parent._execute_tool_calls(SimpleNamespace(tool_calls=[call]), messages, "code-owner")
        return json.loads(next(row["content"] for row in reversed(messages) if row.get("role") == "tool"))

    yield SimpleNamespace(agent=parent, module=module, invoke=invoke, root=tmp_path, manager=manager)
    from tools.code_kernel import shutdown_all_kernels
    shutdown_all_kernels()
    manager.unload("code-fixture")
    parent.close()


def test_nested_policy_hooks_are_once_ordered_and_keep_parent_provenance(host):
    model_result = host.invoke("code_fixture", {"value": 4}, "model")
    assert model_result == {"echo": 4}
    model_order = [kind for kind, event in host.module.EVENTS if event.get("tool_name") == "code_fixture"]
    host.module.EVENTS.clear()
    messages = []
    result = host.invoke("execute_code", {"code": "from hermes_tools import tools\nprint(tools.call('code_fixture', {'value': 4}).data)"}, messages=messages)
    assert result["status"] == "success", result
    nested = [(kind, event) for kind, event in host.module.EVENTS if event.get("tool_name") == "code_fixture"]
    assert [kind for kind, event in nested] == model_order
    assert sum(kind == "post_tool_call" for kind, event in nested) == 1
    assert all(event["parent_call_id"] == "outer" and event["origin"] == "code" and event["cell_id"]
               for kind, event in nested)
    assert sum(kind == "post_tool_call" and event.get("tool_name") == "execute_code"
               for kind, event in host.module.EVENTS) == 1
    assert sum(row.get("role") == "tool" for row in messages) == 1


def test_registered_cell_uses_parent_context_fork_boundary_worker_and_deadline(host, monkeypatch):
    monkeypatch.setattr("agent.tool_executor._resolve_sequential_tool_timeout", lambda: 0.01)
    messages = [{"role": "user", "content": "Acceptance boundary"}]
    result = host.invoke("plugin_cell_fixture", {}, messages=messages)
    assert result["session"] == "code-owner"
    assert result["history"] == [{"role": "user", "content": "Acceptance boundary"}]
    assert result["tracked"] is True
    assert result["cell_id"]


def test_busy_registered_cell_rejects_before_handler_and_unload_removes_registration(host):
    from agent.code_cells import dispatch_cell_entry, is_cell_entry_point
    from agent.tool_invocation import CallContext, bind_call_context
    results = []
    worker = threading.Thread(target=lambda: results.append(host.invoke("plugin_cell_fixture", {"wait": True})))
    worker.start()
    assert host.module.ENTERED.wait(5)
    try:
        with bind_call_context(CallContext(host.agent, "different-task-same-session", "second")):
            with pytest.raises(RuntimeError, match="kernel_busy"):
                dispatch_cell_entry("plugin_cell_fixture", {})
    finally:
        host.module.RELEASE.set()
        worker.join(5)
    assert results and results[0]["session"] == "code-owner"
    host.manager.unload("code-fixture")
    assert not is_cell_entry_point("plugin_cell_fixture")


def test_missing_context_and_empty_grant_never_dispatch(host):
    from agent.code_bridge import dispatch_code_call
    with pytest.raises(ValueError, match="missing_call_context"):
        dispatch_code_call("tools.call", {"name": "code_fixture", "arguments": {"value": 1}}, "code-owner")
    host.agent.valid_tool_names = {"execute_code"}
    host.agent.enabled_toolsets = ["code_execution"]
    result = host.invoke("execute_code", {"code": "from hermes_tools import tools\ntools.call('code_fixture', {'value': 1})"})
    assert result["status"] == "error", result
    assert "tool_unavailable" in result["error"]
    assert not any(event.get("tool_name") == "code_fixture" for kind, event in host.module.EVENTS)
