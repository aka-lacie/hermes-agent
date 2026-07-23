# Dev Branch Local Change Log

This file is the source of truth for intentional differences carried by the
long-lived `dev` branch. Consult it before merging upstream into `dev`.

## Current Audit

Audited on 2026-07-22 after merging `main` at `a23e39fe6`.

The branch was pruned to remove fork-only runtime features that upstream now
supersedes or that are no longer used. The pre-prune state remains available at
`backup/dev-pre-prune-20260722-210022`.

Pre-existing uncommitted edits to `AGENTS.md` and `package-lock.json` belong to
the user and are not part of this audit.

## Decisions Applied

### Local Workstation Runtime Extra

Status: `externalized`

Commits: `4cda99fbf`, `fb23b2b41`

External manifest:
- `/home/sonya/.hermes/scripts/sync-hermes-dev-runtime.sh`

Merge rule:
- Do not restore the host-specific `local` extra to `pyproject.toml` or
  `uv.lock`.
- Update the external sync script when this workstation needs another
  optional extra.

### npm Lockfile Pinning

Status: `retired`

Commits: `45c011128`, `fd6cd5bb2`

Merge rule:
- Follow upstream's npm and workflow configuration.
- Do not restore the local composite action, root `packageManager` pin, or
  the upstream-deleted PyPI workflow.

### Request Debug Dump Enhancement

Status: `plugin`

Plugin:
- `/home/sonya/.hermes/profiles/yuri/plugins/request-debug-dumps/plugin.yaml`
- `/home/sonya/.hermes/profiles/yuri/plugins/request-debug-dumps/__init__.py`

Behavior:
- Observes upstream `pre_api_request` and `api_request_error` hooks.
- Writes the host-sanitized request payload after applying a second redaction
  pass.
- Honors `debug.request_dumps.enabled` and `keep_last`.

Merge rule:
- Keep `agent/agent_runtime_helpers.py`, `agent/conversation_loop.py`,
  `run_agent.py`, and their request-dump tests aligned with upstream.
- Maintain this behavior in the standalone Yuri plugin.

### Optional Turn-Context Callback Compatibility

Status: `retired`

Commit: `88da9ebaf`

Merge rule:
- Do not restore the unused `build_current_time_user_context` keyword to
  `build_turn_context`.

## Runtime Features Moved Outside Core

### Discord Explicit Reaction Tool

Status: `plugin`

Former core commit: `ba911ac75`

Plugin:
- `/home/sonya/.hermes/profiles/yuri/plugins/discord-reactions/plugin.yaml`
- `/home/sonya/.hermes/profiles/yuri/plugins/discord-reactions/__init__.py`

Behavior:
- Registers `discord_add_reaction` into the existing `hermes-discord` toolset.
- Defaults channel and message IDs from the active Discord session.
- Uses `DISCORD_BOT_TOKEN` and the Discord reaction endpoint directly.

Core removal commit:
- `9fffca3bb`

Merge rule:
- Do not restore the former `tools/discord_tool.py` action or gateway config
  delta. Maintain the standalone Yuri plugin instead.

## Retired Local Features

### Kanban Orchestration And Worker Daemons

Status: `retired`

Source commits:
- `535976a01`
- `b7933e81b`
- `b1c8d8e19`
- `c5fa84d32`

Removal commits:
- `53f63b02f`
- `f4e24be82`
- `5e2f0ab45`
- `01439c2a6`

Decision:
- Use upstream `delegate_task` instead of a parallel fork-only Kanban
  orchestration architecture.
- Keep upstream Kanban behavior and the existing board database.
- Yuri gateway dispatch is disabled and the local Kanban skills/toolset are no
  longer enabled.
- `hermes-kanban-worker@executor-codex.service` and
  `hermes-kanban-worker@executor-general.service` are disabled and inactive.
- The systemd template remains as dormant reference material.

Merge rule:
- Do not resurrect parent-agent wakeups, immediate dispatcher nudges, or
  persistent Kanban worker daemons.

### User-Turn Timestamp Injection And Replay

Status: `retired`

Source commits:
- `d330f5c88`
- `369341adc`
- `2df216078`
- `2d2402078`
- `c5d635cba`

Decision:
- Use upstream gateway message timestamps.
- Yuri enables `gateway.message_timestamps.enabled`.

Merge rule:
- Do not restore `_inject_current_time_in_user_turn`, timestamp sidecars, or
  replay reconstruction.

### Discord And Cron Media Delivery

Status: `retired`

Source commits:
- `465d6fc9b`
- `f54b8a9a9`

Decision:
- Adopt upstream media delivery behavior.

### Honcho Memory And Observation Fork

Status: `retired`

Source commits:
- `467f2e8fa`
- `28220aa5f`
- `8197fa6a1`
- `790a3c109`
- `dd17afaae`
- `544d624c9`
- `4096e3a46`

Decision:
- Adopt the upstream Honcho plugin and remove gateway user-alias/injection
  deltas.

### Gemini Replay And Loopback Compatibility

Status: `retired`

Source commits:
- `800f84615`
- `422b49c57`
- `e206176fb`
- `672da089b`
- `a9890ee85`
- `b12b177f4`

Decision:
- Adopt upstream provider replay and Gemini behavior. The local runtime does
  not currently use Gemini.

### Native Browser Screenshot Path

Status: `retired`

Source commits:
- `bfd57f467`
- `71c281e00`

Decision:
- Adopt upstream browser screenshot behavior.

### Profile Identity Overlay

Status: `retired`

Source commit:
- `6558e16ec`

Decision:
- Merge Yuri's identity rules into `SOUL.md`.
- The former file is retained outside the repository as
  `IDENTITY.md.retired-20260722`.
- Do not restore the core `IDENTITY.md` prompt overlay.

### TUI Profile Branding

Status: `retired`

Source commit:
- `6d5388a79`

Decision:
- Adopt upstream TUI branding and theme behavior.

## Merge Checklist

1. Confirm `main` matches `origin/main`.
2. Read this log before resolving conflicts in `dev`.
3. Merge `main` into `dev`; do not rebase the long-lived branch.
4. Preserve only entries marked `plugin`; keep workstation-only behavior in
   its documented external files.
5. Do not resurrect entries marked `retired`.
6. Run `uv lock --check` when `pyproject.toml` or `uv.lock` changes.
7. Run focused tests for every active local source delta touched by the merge.
