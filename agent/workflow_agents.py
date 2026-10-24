"""Structured, retained workflow delegation through the normal host runner.

No plugin dependencies and no second agent loop. Public service entry points are
mixed into SubagentLifecycleService via SubagentSessionMethods.
"""
from contextlib import nullcontext
from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass, field
import logging
import threading
import time


logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    code = "agent_failed"

    def __init__(self, message, *, session_id=None, partial=None):
        super().__init__(message)
        self.session_id = session_id
        self.partial = partial

    def to_dict(self):
        return {"type": type(self).__name__, "code": self.code, "message": str(self),
                "session_id": self.session_id, "partial": self.partial}


class AgentOutputError(AgentError):
    code = "agent_output_invalid"


class AgentAdmissionError(AgentError):
    code = "agent_admission_denied"


class AgentInputError(AgentError):
    code = "input_delivery_unsupported"


class AgentCancelledError(AgentError):
    code = "agent_cancelled"


class AgentTimeoutError(AgentError):
    code = "agent_timeout"


class Cancellation:
    """Read-only union: fail-fast never cancels unrelated parent cell work."""
    def __init__(self, *events):
        self.events = tuple(e for e in events if e is not None)

    def is_set(self):
        return any(e.is_set() for e in self.events)


@dataclass
class WorkflowRequest:
    code_context: object
    output_schema: dict | None
    parent: object
    cancellation: object
    deadline: float
    history: list | None = None
    artifact: object = None
    session_id: str | None = None
    child: object = None
    lease: object = None
    attached: bool = False
    finished: threading.Event = field(default_factory=threading.Event)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise AgentTimeoutError("Ancestor workflow deadline expired.", session_id=self.session_id)
        if self.cancellation.is_set() or not self.code_context.authority.active:
            raise AgentCancelledError("Originating workflow was cancelled or retired.", session_id=self.session_id)
        try:
            self.code_context.call_context.validate()
        except Exception as exc:
            raise AgentCancelledError(str(exc), session_id=self.session_id) from exc

    def attach(self, child):
        self.check()
        self.child = child
        self.session_id = getattr(child, "_delegate_session_id", None) or child.session_id
        # Transfer allocation to actual resource teardown, including delayed
        # cleanup when an interrupted provider is still unwinding.
        child._workflow_admission_lease = self.lease
        self.attached = True
        child._workflow_request = self
        child._workflow_deadline = self.deadline
        child._workflow_cancelled = self.cancellation
        if self.artifact is not None:
            if "execute_code" not in (getattr(child, "valid_tool_names", None) or ()):
                raise AgentAdmissionError("Child has no execute_code grant for reading the workflow input artifact.", session_id=self.session_id)
            from agent.code_artifacts import grant_artifact
            grant_artifact(self.artifact, child.session_id, owner_session_id=(self.parent.session_id, getattr(self.parent, "_delegate_session_id", "")))
            if self.session_id != child.session_id:
                grant_artifact(self.artifact, self.session_id, owner_session_id=(self.parent.session_id, getattr(self.parent, "_delegate_session_id", "")))
        self.check()
        from agent.memory_provider import spawn_context_thread
        spawn_context_thread(self._watch, name="workflow-child-cancel").start()

    def _watch(self):
        while not self.finished.wait(.05):
            try:
                self.check()
            except AgentError:
                from agent.interrupt_compat import request_hard_interrupt
                try:
                    request_hard_interrupt(self.child, "Ancestor workflow cancelled or expired",
                                           tool_reason="workflow cancellation")
                except Exception:
                    logger.exception("Could not interrupt cancelled workflow child")
                return


def _validate_context(service, code_context):
    from agent.tool_invocation import CallContext
    context = getattr(code_context, "call_context", None)
    authority = getattr(code_context, "authority", None)
    if not isinstance(context, CallContext) or authority is None:
        raise AgentAdmissionError("Workflow agents require an active CodeCallContext.")
    try:
        context.validate()
    except Exception as exc:
        error = AgentTimeoutError if context.deadline is not None and time.monotonic() >= context.deadline else AgentCancelledError
        raise error(str(exc)) from exc
    if authority.parent_context is not context or not authority.active:
        raise AgentAdmissionError("Workflow context does not own an active cell authority.")
    parent = service._parent_agent_resolver()
    if parent is None or context.agent is not parent:
        raise AgentAdmissionError("Workflow context does not belong to the active parent.")
    return parent


def _validate_assignment(assignment, session_id, fork_context, provider, output_schema):
    from agent.workflow_agents_output import schema_validator
    if not isinstance(assignment, str) or not assignment.strip():
        raise AgentAdmissionError("assignment must be a non-empty string.")
    if type(fork_context) is not bool:
        raise AgentAdmissionError("fork_context must be a boolean.")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise AgentAdmissionError("session_id must be a retained child ID.")
    if session_id and fork_context:
        raise AgentAdmissionError("A continuation uses its own history; fork_context cannot be combined with session_id.")
    if session_id and provider is not None:
        raise AgentAdmissionError("A continuation preserves its retained provider route; only model may be overridden.")
    schema_validator(output_schema)  # Missing validator fails before any child starts.


def _workflow_route(model, provider):
    """Explicit workflow routing uses the host credential boundary, like /review."""
    if model is None and provider is None:
        return None
    from copy import deepcopy
    from tools.delegate_tool import _load_config
    for name, value in (("model", model), ("provider", provider)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise AgentAdmissionError(f"{name} must be a non-empty string.")
    if provider is not None and (model is None or provider.strip().lower() == "auto"):
        raise AgentAdmissionError("provider requires model and a named provider other than auto.")
    route = deepcopy(_load_config())
    if provider is not None:
        for key in ("base_url", "api_key", "api_mode", "request_overrides", "command", "args"):
            route.pop(key, None)
        route["fallback_providers"] = []
        route["provider"] = provider.strip()
    if model is not None:
        route["model"] = model.strip()
    return route


def run_workflow_agent(service, assignment, input, output_schema, *, fork_context=False,
                       session_id=None, model=None, provider=None, code_context, _cancel=None):
    from agent.workflow_agents_admission import HOST_ADMISSION, profile_limits
    parent = _validate_context(service, code_context)
    _validate_assignment(assignment, session_id, fork_context, provider, output_schema)
    context, authority = code_context.call_context, code_context.authority
    cancellation = Cancellation(context.cancelled, authority.cancel, _cancel)
    deadline = min(authority.deadline, context.deadline if context.deadline is not None else float("inf"))
    request = WorkflowRequest(code_context, output_schema, parent, cancellation, deadline,
                              history=context.capture_fork_boundary() if fork_context else [], session_id=session_id)
    request.check()
    from agent.workflow_agents_input import prepare_input
    input_context, request.artifact = prepare_input(input, parent=parent, authority=authority)
    parent_lease = (vars(parent) if parent is not None else {}).get("_workflow_admission_lease")
    suspended = parent_lease.suspend(request.check) if parent_lease else nullcontext()
    try:
        with suspended:
            request.lease = HOST_ADMISSION.acquire(request.check, nested=parent_lease is not None, **profile_limits(parent))
            if session_id:
                # New input and contract must be in the continuation message; the
                # retained context remains the original child assignment context.
                from tools.delegation_output_schema import append_output_contract
                message = assignment + "\n\n" + append_output_contract(input_context, output_schema)
                result = service.resume_session(session_id, message, model=model, background=False,
                                                _workflow_request=request)
            else:
                result = service.launch_session(goal=assignment, context=input_context, fork_context=fork_context,
                                                _credentials_cfg=_workflow_route(model, provider), background=False,
                                                _workflow_request=request, output_schema=output_schema)
            try:
                request.check()
            except AgentError as exc:
                exc.partial = result
                raise
            entries = result.get("results") or []
            if len(entries) != 1:
                raise AgentError("Delegation did not return exactly one child result.", session_id=request.session_id, partial=result)
            entry = entries[0]
            stable_id = entry.get("session_id") or request.session_id
            if entry.get("workflow_error"):
                err = entry["workflow_error"]
                cls = {c.code: c for c in (AgentOutputError, AgentAdmissionError, AgentInputError, AgentCancelledError, AgentTimeoutError)}.get(err.get("code"), AgentError)
                raise cls(err.get("message", "Workflow child failed."), session_id=stable_id, partial=err.get("partial"))
            if entry.get("incomplete_teardown"):
                raise AgentError("Child completed but remote teardown is unconfirmed; resident allocation retained.",
                                 session_id=stable_id, partial=entry)
            if entry.get("status") != "completed" or entry.get("exit_reason") != "completed":
                raise AgentError(entry.get("error") or "Child did not complete successfully.", session_id=stable_id, partial=entry)
            if entry.get("schema_valid") is not True or "workflow_output" not in entry:
                raise AgentOutputError("Child did not produce validated workflow output.", session_id=stable_id, partial=entry)
            return {"output": entry["workflow_output"], "session_id": stable_id, "status": "completed"}
    except AgentError:
        raise
    except Exception as exc:
        raise AgentError(str(exc), session_id=request.session_id) from exc
    finally:
        request.finished.set()
        if request.lease and not request.attached:
            request.lease.close()


def run_workflow_agents(service, items, max_concurrency, *, fail_fast=False, code_context):
    """Rolling bounded scheduler. Every input keeps its ordered terminal outcome."""
    from agent.workflow_agents_admission import HOST_ADMISSION
    from tools.daemon_pool import DaemonThreadPoolExecutor
    _validate_context(service, code_context)
    if type(max_concurrency) is not int or not 1 <= max_concurrency <= HOST_ADMISSION.active_limit:
        raise AgentAdmissionError(f"max_concurrency must be 1..{HOST_ADMISSION.active_limit}.")
    if not isinstance(items, list) or len(items) > HOST_ADMISSION.queue_limit:
        raise AgentAdmissionError(f"items must be a list of at most {HOST_ADMISSION.queue_limit} assignments.")
    if type(fail_fast) is not bool:
        raise AgentAdmissionError("fail_fast must be a boolean.")
    targets = []
    allowed = {"assignment", "input", "output_schema", "fork_context", "session_id", "model", "provider"}
    for item in items:
        if not isinstance(item, dict) or not {"assignment", "input", "output_schema"} <= item.keys() or item.keys() - allowed:
            raise AgentAdmissionError("Every map item must specify assignment, input, output_schema and supported agent options.")
        if item.get("session_id"):
            targets.append(item["session_id"])
    if len(set(targets)) != len(targets):
        raise AgentAdmissionError("Duplicate continuation targets cannot be activated in the same map.")
    outcomes = [None] * len(items)
    cancel = threading.Event()
    reservation = HOST_ADMISSION.reserve_items(len(items))
    pool = DaemonThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="workflow-map")
    pending, next_index = {}, 0
    parent = code_context.call_context.agent
    parent_lease = (vars(parent) if parent is not None else {}).get("_workflow_admission_lease")
    # Suspend the parent before submitting workers; worker-level suspensions are
    # reference-counted so no completion can reacquire it while siblings wait.
    def check_parent():
        code_context.call_context.validate()
    try:
        with parent_lease.suspend(check_parent) if parent_lease else nullcontext():
            while pending or next_index < len(items):
                while not cancel.is_set() and next_index < len(items) and len(pending) < max_concurrency:
                    idx = next_index
                    next_index += 1
                    reservation.consume()
                    pending[pool.submit(run_workflow_agent, service, **items[idx], code_context=code_context, _cancel=cancel)] = idx
                if not pending:
                    break
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    idx = pending.pop(future)
                    try:
                        outcomes[idx] = future.result()
                    except AgentError as exc:
                        outcomes[idx] = {"status": "cancelled" if isinstance(exc, AgentCancelledError) else "failed",
                                         "session_id": exc.session_id, "error": exc.to_dict()}
                        if fail_fast:
                            cancel.set()
                    except Exception as exc:
                        logger.exception("Workflow map item failed unexpectedly")
                        outcomes[idx] = {"status": "failed", "session_id": None, "error": AgentError(str(exc)).to_dict()}
                        if fail_fast:
                            cancel.set()
            for idx in range(next_index, len(items)):
                error = AgentCancelledError("Item was not started because fail_fast stopped the map.", session_id=items[idx].get("session_id"))
                outcomes[idx] = {"status": "cancelled", "session_id": error.session_id, "error": error.to_dict()}
    finally:
        reservation.close()
        pool.shutdown(wait=False)
    return outcomes
