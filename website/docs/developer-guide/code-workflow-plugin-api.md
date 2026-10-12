---
title: Code workflow plugin API
description: Public cell lifetime, code namespaces and static workflow publication.
---

# Code workflow plugin API

The host owns authority, execution, cancellation, transport, artifacts and retained
children. Plugins can provide saved workflows and decision helpers without replacing
the agent loop. These capabilities currently require **in-process** plugins.

## Public registrations

`ctx.register_cell_entry_point(name, schema, handler)` registers a model-facing tool.
The handler receives `(arguments, code_context)`. Core validates the originating
`CallContext`, accepts one cell per session, captures its conversation boundary,
and enforces the absolute workflow deadline from acceptance, including preflight.
This registration replaces the ordinary sequential-tool timeout with the cell
controller; no tool-name exemption is needed. Cancellation interrupts registered
workers and reports unfinished teardown. An unfinished entry keeps admission busy.

`ctx.register_code_namespace(name, stub_source, handlers, contract_version=1,
description="")` installs a Python module under `hermes_tools`. Stub code calls
`hermes_tools._call("namespace.method", arguments)`; the current host registration
dispatches to `handlers[method](arguments, code_context)`. A namespace cannot
replace `tools`. The optional description teaches the API in `execute_code`'s
schema. Registration follows profile ownership and disappears on plugin unload;
saved kernel functions cannot continue calling an unloaded host handler.

`ctx.register_workflow_validator(handler)` installs a static package checker.
`handler(snapshot_path)` must validate the immutable candidate without importing
or executing its code. Raise an actionable error to reject it. Returning `False`
or `{"success": false}` also rejects publication. Core verifies that validation
did not change the candidate. No checker means no executable-package publication.

## Code context

`code_context.call_context` identifies the originating agent, session task and
call, approval state, cancellation and absolute deadline. It must not be synthesized
from global state or reused after its cell. `code_context.authority` identifies the
accepted cell. A child uses its own context and kernel, inheriting ancestor limits.

The context exposes:

- `execute(source)`: execute in the already accepted cell.
- `describe_tool(name)` and `preflight_tools(expectations)`: current session schemas
  and validation-relevant fingerprints, before lossy model-provider schema
  presentation. Discovery does not grant capabilities.
- `snapshot_skill(name)`: a visible skill's immutable `{path, revision}` under its
  mutation lock. `pin_package(name, prepare)` retains a selection for the cell.
- `register_workflow_contract(name, revision, expectations)`: cooperative drift
  detection for saved code, including compatibility wrappers and supported threads.
- `subagent_lifecycle.run_workflow_agent(...)` and `run_workflow_agents(...)`: the
  normal retained-delegation admission path, structured output and shared limits.

`CallContext` and `invoke_tool` in `agent.tool_invocation` form the common policy
seam for model and code calls. Nested hooks carry `origin="code"`, `parent_call_id`
and `cell_id`. Code calls do not create synthetic model transcript messages.
The bridge refuses missing or invalid context; a valid context retains ordinary
session approval behavior, including configured unattended operation.

## Results and recovery

`tools.call` returns data, text, media references, an optional artifact and metadata.
Known failures raise `ToolCallError` with a serializable record. RPC request IDs
are retained for at-most-once dispatch within the live host; after host-state loss,
an unknown outcome requires reconciliation before another possible write.

Results above 256 KiB use immutable profile/session-scoped artifacts. Local kernels
can read or stream them. Remote kernels receive complete files up to 8 MiB before
a usable handle is exposed; cross-environment streaming is unsupported. Existing
MCP limits still apply: exceeding 2,000,000 text characters is an explicit incomplete
failure. Artifacts never bypass resource limits or silently certify partial data.

Threads inherit cell authority and workflow bindings; executor submissions capture
the submitting context. Retained old threads cannot acquire a new cell's authority.
The absolute deadline includes queueing and host waits. Accepted host waits pause
only the separate local-code watchdog. Active-work slots can be released by waiting
parents; resident child allocations remain held until actual cleanup. Defaults are
8 active children, 32 resident children and 256 queued items per host process; the
profile delegation cap can reduce active capacity. Waiting ancestors retain their
resident allocations, and nested work fails promptly when that ceiling prevents
progress. The workflow deadline defaults to one hour; local Python work retains
the separate 300-second budget. Reconnect acceptance records survive for ten
minutes after cell retirement; they are not durable across a host restart.

A delegated child keeps one resident interpreter across transcript compaction.
Remote runners own an OS session; teardown persists descendant process identities
before signaling and checks surviving session members after the runner exits.
Backend or tool-set changes retire its previous kernel before replacement. Remote
runners publish a private startup receipt before accepting code; teardown checks
that process identity, escalates termination when needed, and confirms exit. If
the transport cannot confirm cleanup, the host reports `incomplete_teardown`,
retains the child handle and resident allocation, and retries through the kernel
reaper when access returns. The retained session remains unavailable for resume
until cleanup completes; script execution is never retried by this recovery.

## Managed package publication

`skill_manage` supports root `workflows.json` and `examples/` alongside scripts and
instructions. Workflow edits require `expected_revision`: a hash for one skill or
a name-to-hash mapping for a batch. `skill_view` returns the locked
`workflow_revision`. New packages use the SHA-256 of the empty tree. Publish related
changes in one operations batch; stale reviewers must read and reconcile again.

Core constructs a candidate without executing it, invokes registered static checks,
then uses existing ownership, cancellation, read-before-write, locks and rollback
protections to publish exactly the validated hash. Publication notices list declared
tool additions/removals and effect changes; they are informational. Existing review
authorization governs publication. No new foreground approval or fixture execution
is required. Explicit foreground execution is a separate operation.

Snapshots are profile-scoped and retained for pinning and rollback; v1 does not
automatically prune them. Restore a prior
tree through the same validated CAS batch, within the existing 20-operation limit.
Snapshots contain code and expectations, never frozen tool grants or credentials.
