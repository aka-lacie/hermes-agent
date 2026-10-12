"""Public execution context shared by model tools and code-originated calls.

The model adapter still owns transcript insertion and presentation budgets.
An invocation carries the actual calling agent, never an ambient/global fallback.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import os
import threading
import time
import uuid


_CURRENT = ContextVar("hermes_tool_call_context", default=None)


@dataclass
class CallContext:
    agent: object
    task_id: str
    call_id: str
    origin: str = "model"
    parent_call_id: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    deadline: float | None = None
    fork_messages: list | None = None
    fork_snapshot: list | None = None
    packages: dict = field(default_factory=dict)
    contracts: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    expected_fingerprint: str | None = None
    dispatched: bool = False
    approval_origin: object | None = None
    approval_workflow: tuple[str, str] | None = None

    def __post_init__(self):
        inherited = vars(self.agent).get("_workflow_deadline") if self.agent is not None else None
        if inherited is not None:
            self.deadline = min(self.deadline, inherited) if self.deadline is not None else inherited
        cancellation = vars(self.agent).get("_workflow_cancelled") if self.agent is not None else None
        if cancellation is not None:
            self.cancelled = cancellation

    def validate(self):
        if self.agent is None or not self.task_id or not self.call_id:
            raise ValueError("missing_call_context: invoke from an active agent tool call")
        if self.cancelled.is_set() or vars(self.agent).get("_interrupt_requested") is True:
            raise RuntimeError("cancelled: the originating call was interrupted")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RuntimeError("deadline_exceeded: the originating call deadline expired")

    def capture_fork_boundary(self):
        if self.fork_messages is None:
            # The turn lease holds conversation entries stable during this cell.
            # Copy the boundary now; materialize filtered contents only on a fork.
            from agent.subagent_context import capture_fork_boundary
            self.fork_messages = capture_fork_boundary(self.agent)

    def fork_history(self):
        from agent.subagent_context import conversation_snapshot
        self.capture_fork_boundary()
        if self.fork_snapshot is None:
            self.fork_snapshot = conversation_snapshot(self.fork_messages)
        from copy import deepcopy
        return deepcopy(self.fork_snapshot)


def current_call_context():
    return _CURRENT.get()


def invocation_provenance(call_id):
    """Add nested identity only to the actual current invocation's hook record."""
    context = current_call_context()
    if context is None or context.origin != "code" or context.call_id != call_id:
        return {}
    from agent.code_bridge import current_cell_authority
    authority = current_cell_authority()
    return {"parent_call_id": context.parent_call_id or "", "origin": "code",
            "cell_id": authority.identity()["cell_id"] if authority is not None else ""}


@contextmanager
def bind_call_context(context):
    context.validate()
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


def invoke_tool(call: dict, context: CallContext, *, execute=None, **pipeline_options):
    """Apply the common policy pipeline once; return its untrimmed outcome.

The model adapter supplies its existing dispatch callback and finishes its own
post/observe/transcript pipeline. Code calls use the same policy and dispatch,
then record their outcome without appending synthetic conversation messages.
"""
    from agent import tool_executor as executor
    context.validate()
    name, arguments = call["name"], call["arguments"]
    from agent.approval_provenance import capture_origin
    context.approval_origin = capture_origin(context, name)
    is_code = execute is None
    if is_code:
        from agent.code_tool_catalog import describe_tool, validate_arguments
        definition = describe_tool(context, name)
        validate_arguments(definition["schema"], arguments)

        def execute(final_args):
            # Middleware may rewrite arguments; validate those before dispatch too.
            context.validate()
            if context.expected_fingerprint is not None:
                from agent.code_tool_catalog import preflight_tools
                preflight_tools(context, {name: context.expected_fingerprint})
            current = describe_tool(context, name)
            validate_arguments(current["schema"], final_args)
            from agent.code_cells import is_cell_entry_point
            if name == "execute_code" or is_cell_entry_point(name):
                raise ValueError("recursive_cell: use workflows.run or a child kernel")
            import model_tools
            with model_tools.suppress_post_tool_call_hook():
                context.dispatched = True
                return model_tools.handle_function_call(
                    name, final_args, context.task_id, tool_call_id=context.call_id,
                    session_id=getattr(context.agent, "session_id", "") or "",
                    turn_id=getattr(context.agent, "_current_turn_id", "") or "",
                    api_request_id=getattr(context.agent, "_current_api_request_id", "") or "",
                    skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
                    skip_tool_execution_middleware=True,
                    enabled_tools=list(session_tool_names(context)),
                    enabled_toolsets=getattr(context.agent, "enabled_toolsets", None),
                    disabled_toolsets=getattr(context.agent, "disabled_toolsets", None),
                )

    start = time.monotonic()
    import model_tools
    worker = executor._registered_tool_worker(context.agent) if is_code else nullcontext()
    hooks = model_tools.allow_post_tool_call_hook() if is_code else nullcontext()
    with bind_call_context(context), worker, hooks:
        try:
            managed = executor._execute_policy_pipeline(
                context.agent, function_name=name, function_args=arguments,
                effective_task_id=context.task_id, tool_call_id=context.call_id,
                execute=execute, **pipeline_options,
            )
        except Exception as exc:
            if is_code:
                managed = executor._ManagedToolResult(
                    result=json.dumps({"error": str(exc)}), args=arguments,
                    middleware_trace=[], blocked=False, dispatched=True,
                )
                _finish_code_call(context, name, managed, time.monotonic() - start)
            raise
        if is_code:
            _finish_code_call(context, name, managed, time.monotonic() - start)
    return managed


def session_tool_names(context):
    from agent.tool_executor import _tool_search_scoped_names
    agent = context.agent
    direct = frozenset(getattr(agent, "valid_tool_names", ()) or ())
    # An explicitly empty surface stays empty, including deferred tools.
    return direct | (_tool_search_scoped_names(agent) if direct else frozenset())


def child_call_context(parent, *, call_id=None):
    return CallContext(
        agent=parent.agent, task_id=parent.task_id,
        call_id=call_id or "code_" + uuid.uuid4().hex, origin="code",
        parent_call_id=parent.call_id, cancelled=parent.cancelled,
        deadline=parent.deadline,
        evidence=parent.evidence,
    )


def _finish_code_call(context, name, managed, duration):
    from agent import tool_executor as executor
    from agent.display import _detect_tool_failure
    ref = executor._ToolCallRef(name, managed.args, context.task_id, context.call_id, managed.middleware_trace)
    if not managed.blocked:
        ref.emit_post(context.agent, managed.result, duration_ms=int(duration * 1000))
        failed, _ = _detect_tool_failure(name, managed.result)
        managed.result = context.agent._append_guardrail_observation(
            name, managed.args, managed.result, failed=failed, tool_call_id=context.call_id,
        )
        context.agent._record_file_mutation_result(
            name, managed.args, managed.result, failed, task_id=context.task_id,
        )
    # Audit is separate from the model transcript and survives caught exceptions.
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "logs" / "code-invocations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"call_id": context.call_id, "parent_call_id": context.parent_call_id,
               "session_id": getattr(context.agent, "session_id", ""), "tool": name,
               "blocked": managed.blocked, "dispatched": context.dispatched,
               "duration_seconds": duration, **invocation_provenance(context.call_id)}
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt) + "\n")
