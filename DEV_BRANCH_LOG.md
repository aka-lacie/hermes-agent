# Dev Branch Local Change Log

This file tracks intentional local changes carried by the long-lived `dev`
branch. Use it during upstream merges to decide whether to keep local behavior,
accept upstream replacements, or reconcile both.

The default rule is: keep upstream changes unless they remove one of the local
behaviors listed here. When a conflict touches a listed file, preserve the local
behavior and adapt it to the upstream structure.

## Current Branch Audit

Audited on 2026-06-04 after merging upstream `main` commit `d29caf382` into
`dev` as `a6b35159a`.

Current branch shape:
- `dev` is current with `origin/main` but carries local integration history.
- Live fork footprint versus upstream is roughly 61 files and 4.4k added lines.
- Highest-conflict surface remains core runtime/provider files, especially
  `run_agent.py`, `agent/conversation_loop.py`,
  `agent/transports/chat_completions.py`, `hermes_state.py`, and gateway config.
- `git rerere` is enabled locally so repeated conflict resolutions can be
  remembered on future merges.

Prune/isolate status:
- `keep-isolate`: feature is still useful but should be moved out of hot core
  files or kept in a small module/tool boundary where practical.
- `audit`: upstream now carries part of the behavior; compare exact semantics
  before preserving all local code.
- `keep`: small local workstation behavior that is still required.
- `prune-candidate`: likely obsolete or mostly upstreamed; avoid preserving old
  conflict rules unless tests or runtime config prove the behavior is still
  missing upstream.

## Local Features To Preserve Or Prune

### Discord Reaction Tool Support

Status: `keep-isolate`

Commits: `ba911ac75`

Behavior:
- The user-facing `discord` tool exposes `add_reaction`.
- `add_reaction` can default to the active Discord session channel and the
  current user's latest message.
- Automatic gateway processing reactions are intentionally disabled, while the
  explicit tool action remains available.
- Discord tool token discovery can fall back to Hermes `.env` files for profile
  subprocesses and tests.

Main files:
- `tools/discord_tool.py`
- `gateway/platforms/discord.py`
- `gateway/config.py`
- `tests/tools/test_discord_tool.py`
- `tests/gateway/test_discord_reactions.py`

Current audit note:
- Still fork-only in `tools/discord_tool.py`; upstream Discord adapter has
  internal reaction helpers, but not the user-facing tool action.
- Prefer extracting this behind a small tool/plugin boundary before rebuilding
  `dev-v2`.

Merge rule:
- Keep local `discord.add_reaction` action, session defaulting, and tests.
- Keep `_reactions_enabled()` disabled for automatic gateway processing
  indicators unless we explicitly decide to re-enable them.
- Accept upstream Discord adapter changes around command sync, lazy deps,
  auth, UI views, and voice handling when they do not re-enable automatic
  processing reactions or remove the explicit reaction tool.

### Discord And Cron Media Delivery

Status: `keep-isolate`

Commits: `465d6fc9b`, `f54b8a9a9`

Behavior:
- Cron delivery supports Telegram voice media and Discord standalone media.
- Discord media send path tries Discord voice delivery for voice media before
  falling back to regular attachments.
- Discord message text avoids extra local part tags.

Main files:
- `tools/send_message_tool.py`
- `cron/scheduler.py`
- `gateway/platforms/discord.py`
- `tests/tools/test_send_message_tool.py`
- `tests/cron/test_scheduler.py`

Current audit note:
- Still a live fork delta, concentrated in `tools/send_message_tool.py` plus
  delivery tests.
- Keep for now, but isolate behind media-delivery helpers if this continues to
  conflict with upstream send-message refactors.

Merge rule:
- Keep local Discord voice-first media sending and standalone media delivery.
- Accept upstream send-message refactors if the voice-first and fallback
  semantics stay covered by tests.

### Honcho Memory And Observation Behavior

Status: `prune-candidate`

Commits: `467f2e8fa`, `28220aa5f`, `8197fa6a1`, `790a3c109`, `dd17afaae`,
`544d624c9`, `4096e3a46`

Behavior:
- Honcho supports configurable bidirectional observation mode.
- Per-turn Honcho injection is disabled locally; observation context is kept
  first-turn / explicit rather than injected every turn.
- Gateway user aliases feed observation context.
- Honcho local behavior has been aligned with the newer upstream plugin shape.

Main files:
- `plugins/memory/honcho/__init__.py`
- `plugins/memory/honcho/session.py`
- `plugins/memory/honcho/cli.py`
- `plugins/memory/honcho/client.py`
- `plugins/memory/honcho/README.md`
- `gateway/config.py`
- `gateway/platforms/base.py`
- `hermes_cli/config.py`
- `hermes_cli/main.py`
- `run_agent.py`
- `tests/honcho_plugin/test_session.py`
- `tests/honcho_plugin/test_client.py`
- `tests/gateway/test_config.py`
- `tests/gateway/test_platform_base.py`

Current audit note:
- The listed `plugins/memory/honcho/*` files are no longer in the live fork
  diff, and upstream now has bidirectional-observation compatibility comments.
- Treat this as mostly upstreamed. Preserve only the remaining `user_aliases`
  behavior if runtime config still depends on it.

Merge rule:
- Keep the local no-per-turn-injection default only if upstream no longer
  provides the same default.
- Keep bidirectional observation mode and user-alias observation context.
- Accept upstream Honcho API/client/session refactors when the local defaults
  and tests are preserved.
- If upstream introduces a cleaner equivalent, prefer upstream implementation
  but keep local config compatibility.

### Provider Replay And Gemini Reasoning

Status: `audit`

Commits: `800f84615`, `422b49c57`, `e206176fb`, `2ea753369`, `672da089b`

Behavior:
- Gemini native and Gemini Cloud Code reasoning traces are preserved.
- Gemini Cloud Code tool result names are fixed for replay.
- Provider replay metadata is normalized through `provider_data`, including
  Gemini content, Codex reasoning/message items, reasoning details, and
  OpenAI-compatible extra content.
- Native Gemini profile defaults are preserved.
- Loopback native Gemini proxy endpoints exposing `/v1beta` are treated as
  native Gemini, not generic OpenAI-compatible local servers, so local
  context-length probing is skipped for those URLs.

Main files:
- `agent/gemini_native_adapter.py`
- `agent/gemini_cloudcode_adapter.py`
- `agent/gemini_content_utils.py`
- `agent/transports/types.py`
- `agent/transports/chat_completions.py`
- `agent/transports/codex.py`
- `gateway/run.py`
- `gateway/session.py`
- `hermes_state.py`
- `run_agent.py`
- `plugins/model-providers/gemini/__init__.py`
- `hermes_cli/doctor.py`
- `hermes_cli/model_switch.py`
- `hermes_cli/providers.py`
- `tests/agent/test_gemini_cloudcode.py`
- `tests/agent/test_gemini_native_adapter.py`
- `tests/agent/transports/test_chat_completions.py`
- `tests/agent/transports/test_types.py`
- `tests/run_agent/test_provider_parity.py`
- `tests/run_agent/test_streaming.py`
- `tests/test_hermes_state.py`

Current audit note:
- Upstream now has first-class `provider_data` in transport types and provider
  adapters. Local code still adds namespaced Google/Codex compatibility,
  `gemini_content` persistence, and route-specific preservation.
- This is the highest-conflict area. Audit exact tests before deciding whether
  to keep all local DB/runtime changes or reduce to upstream behavior plus a
  small compatibility shim.

Merge rule:
- Keep reasoning/replay fidelity fields unless upstream has an exact
  replacement with tests for the same providers.
- Preserve `provider_data` namespace compatibility; do not collapse local
  Google/Codex replay metadata back into one-off top-level fields only.
- Preserve native Gemini base URL detection for loopback `/v1beta` proxy
  endpoints and skip generic local-server probes for those endpoints.
- For conflicts, prefer upstream structure but re-add local replay fields and
  tests.

### Native Browser Screenshot Tool Path

Status: `keep-isolate`

Commits: `bfd57f467`, `71c281e00`

Behavior:
- Adds a native browser screenshot tool path and fixes screenshot path handling.

Main files:
- `tools/browser_tool.py`
- `tools/browser_camofox.py`
- `run_agent.py`
- `agent/display.py`
- `agent/prompt_builder.py`
- `model_tools.py`
- `toolsets.py`
- `tests/run_agent/test_run_agent.py`
- `tests/tools/test_browser_console.py`

Current audit note:
- Upstream already stores browser screenshots under Hermes cache paths. The
  remaining fork value is the separate `browser_screenshot` tool and the
  vision-capable auto-follow-up path in `run_agent.py`.
- Prefer extracting the tool path and minimizing the `run_agent.py` hook before
  rebuilding the branch.

Merge rule:
- Keep the native screenshot path unless upstream fully replaces it with a
  tested equivalent.
- Accept upstream browser backend changes if screenshot path handling remains
  correct.

### Local Workstation Runtime Extra

Status: `keep`

Commits: `4cda99fbf`, `fb23b2b41`

Behavior:
- Adds the canonical `local` extra used by this workstation runtime.
- `local` aliases the extras expected by the active shared environment:
  `dev`, `cli`, `messaging`, `cron`, `honcho`, `pty`, `google`, and `web`
  as currently represented in `pyproject.toml`.

Main files:
- `pyproject.toml`
- `uv.lock`
- `AGENTS.md`

Merge rule:
- Always keep the `local` extra.
- When upstream changes dependency groups or extras, update `local` to include
  any extra required by configured local profiles, plugins, platforms, or
  dashboards.
- Regenerate `uv.lock` with `uv sync --extra local` after conflicts rather
  than hand-editing lockfile entries.

### Kanban Gateway Orchestration Wakeups

Status: `keep-isolate`

Commits: `535976a01`, `b7933e81b`, `b1c8d8e19`

Behavior:
- Gateway-created kanban tasks are stamped with the originating gateway
  `session_id` from task-local session context.
- Terminal kanban task events for session-stamped tasks wake the parent
  gateway agent with a synthetic internal message, instead of only sending a
  human-facing platform notification.
- Session-id wakeups are also tracked through `kanban_agent_notify_cursors`
  for tasks without a human-facing chat subscription. Chat-backed task
  deliveries advance the agent cursor too, so terminal events are not replayed
  after the chat subscription is removed.
- Worker handoff text surfaced into gateway agent context is quoted and
  labelled as untrusted data so child summaries cannot become parent-system
  instructions.
- Session-stamped `kanban_create`, `kanban_unblock`, and `kanban_link`
  nudge the embedded gateway dispatcher immediately instead of waiting for the
  next `dispatch_interval_seconds` poll.
- Gateway-origin follow-up mutations subscribe the active chat to future
  terminal events. Follow-up subscriptions start at the current event cursor
  to avoid replaying old completed/blocked events.
- `kanban_comment` attaches the current session/chat for future notification
  context but remains passive: it does not make a task runnable or wake the
  dispatcher by itself.

Main files:
- `gateway/run.py`
- `hermes_cli/kanban_db.py`
- `gateway/session_context.py`
- `gateway/kanban_dispatch_signal.py`
- `tools/kanban_tools.py`
- `tests/gateway/test_kanban_notifier.py`
- `tests/gateway/test_kanban_dispatch_signal.py`
- `tests/tools/test_kanban_tools.py`

Current audit note:
- The dispatcher nudge is intentionally process-local. It wakes the embedded
  dispatcher when the kanban tool call runs in the gateway process, while still
  routing all actual claiming/spawning through `kanban_db.dispatch_once`.
- The preferred follow-up model for already-done work is a new
  `kanban_create` card with `parents=[old_task_id]`, not reopening the done
  card.

Merge rule:
- Preserve session-id stamping through `gateway.session_context`; do not fall
  back to process-global env-only session routing for concurrent gateway turns.
- Preserve synthetic parent-agent wakeups for session-stamped terminal kanban
  events, including agent-only cursors and untrusted-data quoting for worker
  handoffs.
- Preserve immediate dispatcher nudges for session-stamped create/unblock/link
  mutations, but keep task execution inside the normal dispatcher path.
- Preserve "from now" notification cursors for follow-up subscriptions so old
  terminal events are not replayed.
- Accept upstream kanban dispatcher or notifier refactors when these semantics
  stay covered by focused tests.

### Profile Identity Prompt Overlay

Status: `keep-isolate`

Commits: `6558e16ec`

Behavior:
- Profiles may carry a root-level `IDENTITY.md` in addition to `SOUL.md`.
- `IDENTITY.md` is loaded only from the active Hermes profile root, not from
  the current workspace, and is appended to the stable system prompt after the
  SOUL/default identity block.
- Profile cloning copies `IDENTITY.md` alongside `config.yaml`, `.env`, and
  `SOUL.md`.

Main files:
- `agent/prompt_builder.py`
- `agent/system_prompt.py`
- `hermes_cli/profiles.py`
- `run_agent.py`
- `tests/agent/test_prompt_builder.py`
- `tests/agent/test_system_prompt.py`

Merge rule:
- Preserve `IDENTITY.md` as a profile-root-only stable prompt overlay.
- Do not let workspace-local `IDENTITY.md` files become context files unless a
  separate explicit feature adds that behavior.
- Keep `SOUL.md` as the primary identity slot; `IDENTITY.md` is additive.

### Chat Completion Streaming Gemini Replay Fix

Status: `keep`

Commits: `b12b177f4`

Behavior:
- Imports `copy` for the chat-completion streaming path that deep-copies
  Gemini provider parts before mutating accumulated tool-call state.

Main files:
- `agent/chat_completion_helpers.py`

Merge rule:
- Preserve the `copy.deepcopy` import/use when keeping the Gemini provider
  replay accumulation path.

### Agent-Facing Time Context

Status: `keep-isolate`

Commits: `d330f5c88`, `369341adc`, `2df216078`

Behavior:
- Agent receives current time context during conversations.
- Local implementation injects current time into user turns with a `system_time`
  tag.
- Agent-facing timestamps use 24-hour time.
- Stronger upstream behavior is acceptable, including always-on current time or
  timestamps on all messages.

Main files:
- `run_agent.py`
- `hermes_cli/config.py`
- `cli-config.yaml.example`
- `tests/run_agent/test_run_agent.py`

Current audit note:
- Still a live config/runtime delta. If pre-LLM hooks can inject this reliably,
  move it there to avoid touching `run_agent.py` and conversation assembly.

Merge rule:
- Preserve the guarantee that the agent has at least the current time.
- Prefer upstream behavior if it provides always-on current time or timestamps
  on all messages.
- Keep 24-hour agent-facing timestamp formatting where local code controls the
  presentation.

### TUI Profile Branding

Status: `keep`

Commits: `6d5388a79`

Behavior:
- TUI banner can reflect profile branding.

Main files:
- `ui-tui/src/components/branding.tsx`
- `ui-tui/src/theme.ts`
- `ui-tui/packages/hermes-ink/src/ink/dom.ts`

Current audit note:
- Tiny live delta in `ui-tui/src/theme.ts`; low update cost. Keep unless
  upstream adds equivalent branding fields.

Merge rule:
- Keep profile-branded banner support.
- Accept upstream TUI layout/rendering changes when profile branding remains
  wired.

## Pruning Plan

1. Mark each status above as final: `keep`, `isolate`, or `remove`.
2. For `audit` items, compare upstream tests and runtime behavior before
   preserving local code.
3. Build a fresh `dev-v2` from `origin/main` by cherry-picking only final
   `keep` items and extracting `keep-isolate` items behind smaller boundaries.
4. Keep the current `dev` branch intact until `dev-v2` has passed runtime sync,
   focused tests, and gateway/dashboard restart checks.
5. Once `dev-v2` is proven, move `fork/dev` to it intentionally.

## Merge Checklist

1. Confirm `git config rerere.enabled` is `true` in the Hermes worktrees.
2. Update clean `main` from `origin/main` and push `fork/main`.
3. Merge any integration branch into `dev` before bringing in fresh `main`
   updates, if that integration branch contains unfinished local work.
4. Merge `main` into `dev`.
5. For conflicts, consult the entries above before choosing either side.
6. If `rerere` applies a resolution, still inspect the hunk and run focused
   tests before committing.
7. Regenerate dependencies with `uv sync --extra local`.
8. Run focused tests for any listed local feature touched by the merge.
9. Commit and push `dev` to `fork/dev`.
10. Restart Hermes gateways and dashboards from the updated `hermes-dev`
    environment.
