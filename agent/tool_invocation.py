"""Host-owned context for the agent tool call currently executing.

Approval provenance reads the calling agent from here so a prompt is bound to
the actual actor that requested it, never to an ambient/global fallback.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


_CURRENT = ContextVar("hermes_tool_call_context", default=None)


@dataclass
class CallContext:
    agent: object
    task_id: str
    call_id: str
    origin: str = "model"
    approval_origin: object | None = None
    approval_workflow: tuple[str, str] | None = None


def current_call_context():
    return _CURRENT.get()


@contextmanager
def bind_call_context(context):
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


@contextmanager
def bind_agent_tool_call(agent, task_id, call_id, tool):
    """Capture the immutable approval origin once, before policy and dispatch."""
    context = CallContext(agent, task_id or "", call_id or "")
    from agent.approval_provenance import capture_origin
    try:
        context.approval_origin = capture_origin(context, tool)
    except TypeError:  # agent without instance attributes: origin stays unknown (never coalesces)
        context.approval_origin = None
    with bind_call_context(context):
        yield context
