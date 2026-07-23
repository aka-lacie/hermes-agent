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

## Active Local Changes Pending A Decision

### Local Workstation Runtime Extra

Status: `decision-pending`

Commits: `4cda99fbf`, `fb23b2b41`

Files:
- `pyproject.toml`
- `uv.lock`
- `AGENTS.md`

Behavior:
- Adds a `local` extra that composes the `dev`, `cli`, `messaging`, `cron`,
  `honcho`, `pty`, `google`, and `web` extras used by this workstation.
- Keeps the shared runtime reproducible with
  `uv sync --locked --extra local`.

Merge rule:
- Preserve until an external workstation dependency manifest or another
  replacement is explicitly selected.
- After dependency conflicts, validate with `uv lock --check`; do not
  hand-delete the `local` markers from `uv.lock`.

### npm Lockfile Pinning

Status: `decision-pending`

Commits: `45c011128`, `fd6cd5bb2`

Files:
- `.github/actions/pin-npm/action.yml`
- `.github/workflows/deploy-site.yml`
- `.github/workflows/docs-site-checks.yml`
- `.github/workflows/upload_to_pypi.yml`
- `package.json`

Behavior:
- Pins npm for deterministic lockfile generation in selected CI workflows.
- Avoids imposing that CI pin on normal runtime installs.

Merge rule:
- Preserve pending a separate decision.
- Upstream deleted `upload_to_pypi.yml`; the local copy is intentionally
  retained only while this decision is open.

### Request Debug Dump Enhancement

Status: `decision-pending`

Files:
- `agent/agent_runtime_helpers.py`
- `run_agent.py`
- `tests/run_agent/test_run_agent_codex_responses.py`

Behavior:
- Optionally includes the assembled system prompt and message list in request
  debug dumps.
- Retains only the configured number of dump files.
- Enables dumps through `debug.request_dumps.enabled` as well as the existing
  environment switch.

Merge rule:
- Preserve pending a separate decision.
- This is core-runtime instrumentation and is not a good plugin candidate
  unless upstream exposes a request-debug hook.

### Optional Turn-Context Callback Compatibility

Status: `decision-pending`

Commit: `88da9ebaf`

File:
- `agent/turn_context.py`

Behavior:
- Keeps `build_current_time_user_context` as an optional keyword accepted by
  `build_turn_context`.
- The callback is now unused because the fork-only user-turn timestamp
  injection was retired; this is currently a one-line compatibility shim.

Merge rule:
- Preserve only until the caller-compatibility decision is made. Removing it
  is the cleanest option if no external caller still passes the keyword.

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
4. Preserve only entries still marked `decision-pending` or `plugin`.
5. Do not resurrect entries marked `retired`.
6. Run `uv lock --check` when `pyproject.toml` or `uv.lock` changes.
7. Run focused tests for every active local source delta touched by the merge.
