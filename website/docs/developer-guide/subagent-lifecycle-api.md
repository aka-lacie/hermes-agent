---
title: Public Subagent Lifecycle API
sidebar_label: Subagent lifecycle API
---

# Public Subagent Lifecycle API

Plugins can launch and supervise independent Hermes child sessions without importing
`tools.delegate_tool`, gateway internals, TUI state, or `AIAgent` fields.
The service resolves its parent from the current agent turn, so it works in
CLI, gateway, non-interactive, and kanban-worker sessions. Launching outside an
active agent turn fails closed with `No active Hermes parent session`.

`SubagentLaunchRequest.fork_context` defaults to `True`. Children inherit a private
snapshot of user turns, final assistant answers, and textual compaction handoffs;
parent tool calls/results, reasoning, and interim commentary are omitted. The goal
is a separate assignment, not a continuation of the parent's pending tool call.
Use `fork_context=False` for a fresh conversation. Plugin tools and `pre_llm_call`
hooks snapshot the current turn; other callers use the parent's saved in-memory
history. Child sessions persist their inherited prefix separately from the parent.

Both lifecycle launches and `delegate_task` load the parent's active-profile SOUL.md and
resolved workspace's project instructions. This is independent of `fork_context`.
Optional profile files `instructions/main.md` and `instructions/subagent.md` separate
main-agent operating guidance from child guidance; children never fall back to the main
file. These are startup file reads, not a copy of the parent's cached system prompt, and
cannot grant tools or override depth limits. See [shared identity and role instructions](../user-guide/features/delegation.md#shared-identity-separate-operating-instructions).

```python
from agent.subagent_lifecycle import SubagentLaunchRequest

def launch_review(ctx):
    # Call from a plugin tool or hook while an agent turn is active.
    service = ctx.subagent_lifecycle
    handle = service.launch(SubagentLaunchRequest(
        goal="Review this change for regressions.",
        context="Only inspect the supplied repository.",
        role="leaf",
        correlation_id="review-42",
        allowed_toolsets=("file",),
    ))
    # Persist handle.to_dict() if desired.
    if service.wait(handle, timeout_seconds=2).timed_out:
        return handle.to_dict()
    return service.result(handle)
```

`SubagentHandle` is serializable and carries a versioned, opaque capability.
Pass it back to `status`, `wait`, `cancel`, `result`, or `reconnect`; malformed
or forged handles return `UNKNOWN`/`UNKNOWN_HANDLE` and cannot access a child.

The stable states are `PENDING`, `STARTING`, `RUNNING`, `SUCCEEDED`, `FAILED`,
`INTERRUPTED`, `CANCEL_REQUESTED`, `CANCELLED`, and `UNKNOWN`.

`cancel(handle, reason=...)` is cooperative: it asks the child agent to
interrupt at its next safe boundary and returns `CANCEL_REQUESTED`; it never
claims completion until `wait` or `result` observes a terminal state. Terminal
results are immutable, idempotent, bounded to 32k characters, omit transcripts
and hidden reasoning, and include a stable result hash.

This API is lifecycle-managed asynchronous execution. Child construction and
completion use the same host-owned path as `delegate_task`, including parent
tool-resolution restoration, memory notification, serialized `subagent_stop`
hooks, resource cleanup, and child-cost rollup. It does not change the
synchronous `delegate_task` tool, batch delegation, or its gateway/TUI display.
The initial implementation retains metadata and terminal results in-process for
one hour.
After a process restart, `reconnect` returns `RECONNECT_UNAVAILABLE` and never
starts a replacement child. Running Python threads also cannot survive process
exit; callers must treat those handles as interrupted by process exit.

Requests are fail-closed: goal/context/metadata sizes are capped, unknown or
parent-broadening toolsets are rejected, and per-tool blocks, working-directory
overrides, and per-launch timeouts are explicitly rejected until Hermes can
support them without weakening isolation. Use `allowed_toolsets` to narrow a
child; Hermes's existing unsafe-tool block remains enforced.

## Retained conversations (opt-in)

The same host service also offers explicit retained-conversation operations:
`launch_session(goal=..., context=None, title=None, fork_context=True,
model=None, provider=None, background=False)`,
`resume_session(session_id, message, model=None, background=False)`,
`list_sessions(query=None, limit=20)`, `observe_session(session_id, limit=20)`,
`close_session(session_id)`, `steer_session(session_id, message)`, and `stop_session(session_id)`. Optional arguments are keyword-only. These return
normal delegation result dictionaries; refusals raise `SubagentLifecycleError`.
Background execution still requires a host capable of delivering later results.

Conversation methods use stable session IDs stored in the parent's profile
database. The existing immutable run handles and their reconnect/retention
contract are unchanged. Use `launch_session` when future continuation is needed.
The parent conversation owns the child across reconstruction and compression;
another conversation cannot adopt it. Each accepted activation has exclusive
admission and uses the normal transcript turn lease.

Idle children have no AIAgent instance and no automatic expiry. Explicit close
retires them without deleting history; normal session deletion still applies.
Process loss ends the active run without replaying it; the saved conversation can
be continued after the recorded process is proven dead. This is not durable
execution or crash recovery.


## Delegation extensions

`ctx.register_subagent_extension(extension)` accepts an instance of the public
`agent.subagent_extension.SubagentExtension` contract. It requires the same operator
consent as replacing a built-in tool: `plugins.entries.<plugin>.allow_tool_override: true`.
There is one active extension per profile; normal registration leases restore its
predecessor when unloaded. The callbacks receive plain data, never live agents:

- `enabled()` determines whether the extension participates.
- `extend_schema(schema)` receives a deep copy of the dynamic `delegate_task` schema
  and returns its model-facing description/parameters. Existing native fields should be preserved.
- `handle_action(action, request)` returns a JSON tool result or `None` for native
  dispatch. The active caller is bound to `ctx.subagent_lifecycle` for the callback.
  Spawn remains host-owned. Plugin-owned parameters travel through both native
  registry and inline model dispatch without adding fields to core.
- `child_options(task)` returns optional `retain` and `review_skills` booleans and
  `title`. A retained child uses its existing conversation on continuation. Review
  remains host-budgeted, skills-only, cancellable, and subject to normal eligibility.
- `format_assignment(message, session_id=..., timestamp=...)` formats the new assignment.
  It cannot change persisted parentage or acquire control over an unrelated child.

The host owns retention storage, ownership, provider reconstruction, activation
admission, live controls, transcript safety, and teardown even after plugin unload.
Raw conversation-service records contain routing identity and ownership metadata;
plugins own bounded presentation and should avoid exposing unnecessary fields to models.
Existing rows need no migration when policy moves into a plugin.

This extension currently requires `plugins.isolation: in_process`; host-isolated
registration fails explicitly because the synchronous activation-context callback
contract has not been implemented across the process boundary. There are no private
agent objects, runtime monkeypatches, or plugin-name checks in the host integration.
