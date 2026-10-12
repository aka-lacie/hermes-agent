"""Core-owned admission and lifetime for plugin-registered cell entry points."""
from contextlib import contextmanager
import json
import logging
import threading
import time


_LOCK = threading.Lock()
_ACTIVE = {}
logger = logging.getLogger(__name__)


def cell_entries():
    from hermes_cli.plugins import get_plugin_manager
    return getattr(get_plugin_manager(), "_cell_entries", {})


def is_cell_entry_point(name):
    return name in cell_entries()


@contextmanager
def accept_cell(context):
    from agent.code_bridge import bind_cell_authority, current_cell_authority
    from tools.code_kernel import CellAuthority
    from hermes_constants import get_hermes_home
    from tools.code_execution_tool import _load_config
    context.validate()
    existing = current_cell_authority()
    if existing is not None and existing.parent_context.agent is context.agent:
        raise RuntimeError("recursive_cell: compose a Python workflow or use a child kernel")
    key = (str(get_hermes_home().resolve()), getattr(context.agent, "session_id", None) or context.task_id)
    admission = object()
    with _LOCK:
        if key in _ACTIVE:
            raise RuntimeError("kernel_busy: this session already has an active execution cell")
        _ACTIVE[key] = admission
    authority = None
    try:
        duration = float(_load_config().get("workflow_timeout", 3600))
        if duration <= 0:
            raise ValueError("workflow_timeout must be positive")
        context.capture_fork_boundary()
        authority = CellAuthority(context.task_id, timeout=duration)
        authority.entry_done = threading.Event()
        authority.entry_done.set()
        authority.admission_key = key
        authority.admission_token = admission
        context.deadline = authority.deadline
        context.cancelled = authority.cancel
        with bind_cell_authority(authority):
            yield
    finally:
        if authority is not None:
            authority.retire()
        if authority is None or authority.entry_done.is_set():
            _release_admission(key, admission)


def _release_admission(key, token):
    """Late caller/worker cleanup may release only the admission it owns."""
    with _LOCK:
        if _ACTIVE.get(key) is token:
            _ACTIVE.pop(key)


def _run_cell_handler(entry, arguments, context):
    """Enforce the public lifetime even during preflight, before a kernel exists."""
    from agent.code_bridge import CodeCallContext, current_cell_authority
    from agent.tool_executor import _registered_tool_worker
    from tools.thread_context import propagate_context_to_thread
    authority = current_cell_authority()
    authority.entry_done.clear()
    outcome = {}

    def invoke():
        try:
            with _registered_tool_worker(context.agent):
                outcome["result"] = entry["handler"](arguments, CodeCallContext(context))
        except BaseException as exc:
            logger.error("Plugin cell entry point failed", exc_info=True)
            outcome["error"] = exc
        finally:
            authority.entry_done.set()
            if not authority.active:
                _release_admission(authority.admission_key, authority.admission_token)

    worker = threading.Thread(target=propagate_context_to_thread(invoke), daemon=True,
                              name="hermes-plugin-cell")
    try:
        worker.start()
    except RuntimeError:
        authority.entry_done.set()
        raise
    while True:
        done = authority.entry_done.wait(0.05)
        expired = time.monotonic() >= authority.deadline
        interrupted = (vars(context.agent).get("_interrupt_requested") is True
                       or authority._parent_cancel is not None and authority._parent_cancel.is_set())
        if expired or interrupted:
            authority.retire()
            from tools.interrupt import set_interrupt as _set_interrupt
            if not done:
                _set_interrupt(True, worker.ident)
            settled = authority.entry_done.wait(1.0)
            return _stopped_entry_result(outcome, expired, settled)
        if done:
            break
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def _stopped_entry_result(outcome, expired, settled):
    """Keep receipts/output from a settled handler while reporting the winning stop."""
    result = outcome.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {"output": result}
    result = dict(result) if isinstance(result, dict) else {}
    result.update(status="timeout" if expired else "cancelled",
                  error="workflow_deadline_exceeded: absolute workflow deadline reached" if expired else "workflow_cancelled")
    incomplete = list(result.get("incomplete_teardown") or [])
    if not settled:
        incomplete.append({"status": "entry_point_still_settling"})
    result["incomplete_teardown"] = incomplete
    return result


def dispatch_cell_entry(name, arguments):
    from agent.tool_invocation import current_call_context
    context = current_call_context()
    if context is None:
        raise ValueError("missing_call_context: invoke workflow from an active session")
    entry = cell_entries().get(name)
    if entry is None:
        raise ValueError("namespace_unavailable: cell entry point was unloaded")
    with accept_cell(context):
        result = _run_cell_handler(entry, arguments, context)
    return attach_cell_evidence(result, context)


def attach_cell_evidence(result, context):
    """One outer receipt includes nested pinned revisions and review evidence."""
    payload = json.loads(result) if isinstance(result, str) else result
    if isinstance(payload, dict):
        if context.contracts:
            payload["workflow_revisions"] = {name: revision for name, revision, _ in context.contracts.values()}
        if context.evidence:
            payload["workflow_evidence"] = context.evidence[-20:]
    return json.dumps(payload, ensure_ascii=False)
