"""Session-local eager worker schemas; never a delegation or permission grant.

The historical eager_subagents knobs cover every non-main worker. Tool assembly
and its shared memo key must read the SAME boolean, including in-process cron
and webhook construction. Bind the actual agent while building/refreshing tools;
legacy direct callers use scoped worker/session markers, not a process-global
role that can leak into concurrent interactive sessions.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_EAGER_WORKER: ContextVar[bool | None] = ContextVar("hermes_eager_worker_tools", default=None)
_WORKER_SURFACES = frozenset({"subagent", "cron", "webhook", "kanban", "oneshot", "tool"})


def is_non_main_agent_context() -> bool:
    """Exact eager-tool predicate, also part of the tool-definition cache key."""
    bound = _EAGER_WORKER.get()
    if bound is not None:
        return bound
    from agent.delegation_context import is_dispatcher_owned_worker_context, owned_kanban_task
    from agent.oneshot_footprint import is_single_query_session
    from gateway.session_context import get_session_env

    return (not is_dispatcher_owned_worker_context() or bool(owned_kanban_task())
            or get_session_env("HERMES_CRON_SESSION") == "1"
            or get_session_env("HERMES_SESSION_PLATFORM") in _WORKER_SURFACES
            or get_session_env("HERMES_SESSION_SOURCE") in _WORKER_SURFACES
            or is_single_query_session())


@contextmanager
def agent_tool_context(agent) -> Iterator[None]:
    """Freeze worker identity on first build and retain it on out-of-turn refresh.

    An explicitly interactive agent masks inherited ambient worker markers. Cron's
    default instruction_role='main' is guidance, NOT proof it is the human chat:
    its actual cron surface still makes it a worker, as do webhook and Kanban.
    """
    worker = getattr(agent, "_eager_worker_tools", None)
    if not isinstance(worker, bool):
        from agent.delegation_context import owned_kanban_task
        from agent.oneshot_footprint import agent_is_oneshot

        platform = getattr(agent, "platform", None)
        worker = (getattr(agent, "instruction_role", None) == "subagent"
                  or platform in _WORKER_SURFACES
                  or bool(getattr(agent, "side_agent", False))
                  or agent_is_oneshot(agent) or bool(owned_kanban_task())
                  or (platform is None and is_non_main_agent_context()))
        agent._eager_worker_tools = worker
    token = _EAGER_WORKER.set(worker)
    try:
        yield
    finally:
        _EAGER_WORKER.reset(token)
