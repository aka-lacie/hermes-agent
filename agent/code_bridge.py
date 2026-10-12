"""Public code-call context and namespace dispatch; no independent agent loop."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import uuid
import time


_CELL = ContextVar("hermes_active_cell_authority", default=None)


def current_cell_authority():
    return _CELL.get()


@contextmanager
def bind_cell_authority(authority):
    token = _CELL.set(authority)
    try:
        yield authority
    finally:
        _CELL.reset(token)


class CodeCallContext:
    def __init__(self, context):
        context.validate()
        self.call_context = context
        self.authority = current_cell_authority()
        if self.authority is None or not self.authority.active:
            raise ValueError("missing_cell_context: invoke from an active execution cell")

    @property
    def subagent_lifecycle(self):
        from agent.subagent_lifecycle import SubagentLifecycleService
        return SubagentLifecycleService(lambda: self.call_context.agent)

    def require_plugin(self, name):
        from hermes_cli.plugins import get_plugin_manager
        manager = get_plugin_manager()
        if not any((key == name or loaded.manifest.name == name) and loaded.enabled and not loaded.error
                   for key, loaded in manager._plugins.items()):
            raise ValueError(f"plugin_dependency: enable {name} in this profile")

    def describe_tool(self, name):
        from agent.code_tool_catalog import describe_tool
        return describe_tool(self.call_context, name)

    def preflight_tools(self, expectations):
        from agent.code_tool_catalog import preflight_tools
        preflight_tools(self.call_context, expectations)

    def register_workflow_contract(self, name, revision, expectations):
        self.preflight_tools(expectations)
        for ident, existing in self.call_context.contracts.items():
            if existing == (name, revision, expectations):
                return ident
        ident = uuid.uuid4().hex
        self.call_context.contracts[ident] = (name, revision, dict(expectations))
        return ident

    def pin_package(self, name, prepare):
        if name not in self.call_context.packages:
            self.call_context.packages[name] = prepare()
        return self.call_context.packages[name]

    def snapshot_skill(self, name):
        from tools.workflow_packages import snapshot_skill
        return snapshot_skill(name)

    def execute(self, source, *, package_files=None, invocation_contract=None):
        from tools.code_execution_tool import execute_code
        from agent.tool_invocation import session_tool_names
        if package_files or invocation_contract:
            raise ValueError("Use workflows.prepare and bind_workflow_contract for package composition")
        return json.loads(execute_code(source, task_id=self.call_context.task_id,
                                      enabled_tools=list(session_tool_names(self.call_context))))


def namespace_sources():
    from hermes_cli.plugins import get_plugin_manager
    from tools.code_tools_stub import SOURCE
    parts = [SOURCE]
    for name, entry in getattr(get_plugin_manager(), "_code_namespaces", {}).items():
        # Each plugin source has its own namespace so imports/locals cannot
        # accidentally overwrite a sibling's helper implementation.
        parts.append(f"\nimport types as _namespace_types\n{name} = _namespace_types.ModuleType({name!r})\n"
                     f"exec({entry['source']!r}, {name}.__dict__)\n")
    return "\n".join(parts)


def dispatch_code_call(tool_name, tool_args, task_id, *, workflow_contract=None):
    from agent.tool_invocation import current_call_context
    from agent.code_tool_catalog import current_definitions, describe_tool
    parent = current_call_context()
    if parent is None or parent.task_id != task_id:
        raise ValueError("missing_call_context: code tools require the originating agent call")
    context = CodeCallContext(parent)
    if tool_name == "tools.describe":
        return json.dumps(describe_tool(parent, tool_args["name"]))
    if tool_name == "tools.search":
        from tools.tool_search_catalog import build_catalog, search_catalog
        catalog = build_catalog([{"type": "function", "function": item}
                                 for item in current_definitions(parent).values()])
        hits = search_catalog(catalog, tool_args.get("query", ""), limit=tool_args.get("limit", 10))
        return json.dumps([{"name": hit.name, "description": hit.description} for hit in hits])
    if tool_name == "tools.read_artifact":
        from agent.code_artifacts import prepare_artifact
        return json.dumps(prepare_artifact(tool_args["handle"], context=parent, authority=context.authority))
    if "." in tool_name and not tool_name.startswith("tools."):
        from hermes_cli.plugins import get_plugin_manager
        namespace, method = tool_name.split(".", 1)
        entry = getattr(get_plugin_manager(), "_code_namespaces", {}).get(namespace)
        if entry is None or method not in entry["handlers"]:
            raise ValueError("namespace_unavailable: refresh code interfaces in an active session")
        return json.dumps(entry["handlers"][method](tool_args, context), ensure_ascii=False)
    return _invoke_code_tool(tool_name, tool_args, parent, workflow_contract)


def _invoke_code_tool(tool_name, tool_args, parent, workflow_contract):
    from agent.code_artifacts import ToolCallError
    from agent.tool_invocation import child_call_context
    context = child_call_context(parent)
    try:
        return _invoke_code_tool_inner(tool_name, tool_args, parent, workflow_contract, context)
    except ToolCallError:
        raise
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        if tool_name != "tools.call":
            raise
        prefix = str(exc).split(":", 1)[0]
        code = prefix if prefix.isidentifier() else "tool_operation_failed"
        raise ToolCallError(code, str(exc), tool=tool_args.get("name"),
                            dispatched=context.dispatched, call_id=context.call_id) from exc


def _invoke_code_tool_inner(tool_name, tool_args, parent, workflow_contract, call_context):
    from agent.tool_invocation import invoke_tool
    from agent.code_tool_catalog import describe_tool, preflight_tools
    from agent.code_artifacts import normalize_result
    generic = tool_name == "tools.call"
    name = tool_args["name"] if generic else tool_name
    arguments = tool_args.get("arguments", {}) if generic else tool_args
    expected = tool_args.get("expected_fingerprint") if generic else None
    if workflow_contract:
        contract = parent.contracts.get(workflow_contract)
        if contract is None or name not in contract[2]:
            raise ValueError("undeclared_tool: revise the saved workflow dependency declarations")
        if expected is not None and expected != contract[2][name]:
            raise ValueError("workflow_schema_mismatch: explicit fingerprint conflicts with saved contract")
        expected = contract[2][name]
        call_context.approval_workflow = (contract[0], contract[1])
    if expected is not None:
        preflight_tools(parent, {name: expected})
    definition = describe_tool(parent, name)
    call_context.expected_fingerprint = expected
    started = time.monotonic()
    outcome = invoke_tool({"name": name, "arguments": arguments}, call_context)
    result = outcome.result
    if generic:
        return json.dumps(normalize_result(result, context=call_context, name=name,
                                           fingerprint=definition["fingerprint"],
                                           duration_seconds=time.monotonic() - started,
                                           dispatched=outcome.dispatched and not outcome.blocked), ensure_ascii=False)
    return result
