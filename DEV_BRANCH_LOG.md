# Dev Branch Local Change Log

This file tracks intentional local changes carried by the long-lived `dev`
branch. Use it during upstream merges to decide whether to keep local behavior,
accept upstream replacements, or reconcile both.

The default rule is: keep upstream changes unless they remove one of the local
behaviors listed here. When a conflict touches a listed file, preserve the local
behavior and adapt it to the upstream structure.

## Current Upstream Update

Merged into `dev` on 2026-05-15:

- `9fb40e6a3` upstream: restrict TUI fast-echo bypass to ASCII so
  Vietnamese/CJK/IME input renders correctly.
- `d5416284f` upstream: add autonomous background process completion
  notifications in the TUI.
- `bcba91253` local merge: merge refreshed `main` into `dev`.

The larger integration branch also brought in upstream work around provider
plugins, Teams/MS Graph/Google Chat messaging, web search providers, computer
use, profile distributions, TUI/session improvements, kanban updates, skills
reorganization, and docs/site updates. Treat those as upstream unless they
overlap with the local entries below.

## Local Features To Preserve

### Discord Reaction Tool Support

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

Merge rule:
- Keep local `discord.add_reaction` action, session defaulting, and tests.
- Keep `_reactions_enabled()` disabled for automatic gateway processing
  indicators unless we explicitly decide to re-enable them.
- Accept upstream Discord adapter changes around command sync, lazy deps,
  auth, UI views, and voice handling when they do not re-enable automatic
  processing reactions or remove the explicit reaction tool.

### Discord And Cron Media Delivery

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

Merge rule:
- Keep local Discord voice-first media sending and standalone media delivery.
- Accept upstream send-message refactors if the voice-first and fallback
  semantics stay covered by tests.

### Honcho Memory And Observation Behavior

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

Merge rule:
- Keep the local no-per-turn-injection default.
- Keep bidirectional observation mode and user-alias observation context.
- Accept upstream Honcho API/client/session refactors when the local defaults
  and tests are preserved.
- If upstream introduces a cleaner equivalent, prefer upstream implementation
  but keep local config compatibility.

### Provider Replay And Gemini Reasoning

Commits: `800f84615`, `422b49c57`, `e206176fb`, `2ea753369`, `672da089b`

Behavior:
- Gemini native and Gemini Cloud Code reasoning traces are preserved.
- Gemini Cloud Code tool result names are fixed for replay.
- Provider replay metadata is normalized through `provider_data`, including
  Gemini content, Codex reasoning/message items, reasoning details, and
  OpenAI-compatible extra content.
- Native Gemini profile defaults are preserved.

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

Merge rule:
- Keep reasoning/replay fidelity fields unless upstream has an exact
  replacement with tests for the same providers.
- Preserve `provider_data` namespace compatibility; do not collapse local
  Google/Codex replay metadata back into one-off top-level fields only.
- For conflicts, prefer upstream structure but re-add local replay fields and
  tests.

### Native Browser Screenshot Tool Path

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

Merge rule:
- Keep the native screenshot path unless upstream fully replaces it with a
  tested equivalent.
- Accept upstream browser backend changes if screenshot path handling remains
  correct.

### Local Workstation Runtime Extra

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

### Agent-Facing Time Context

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

Merge rule:
- Preserve the guarantee that the agent has at least the current time.
- Prefer upstream behavior if it provides always-on current time or timestamps
  on all messages.
- Keep 24-hour agent-facing timestamp formatting where local code controls the
  presentation.

### TUI Profile Branding

Commits: `6d5388a79`

Behavior:
- TUI banner can reflect profile branding.

Main files:
- `ui-tui/src/components/branding.tsx`
- `ui-tui/src/theme.ts`
- `ui-tui/packages/hermes-ink/src/ink/dom.ts`

Merge rule:
- Keep profile-branded banner support.
- Accept upstream TUI layout/rendering changes when profile branding remains
  wired.

## Merge Checklist

1. Update clean `main` from `origin/main` and push `fork/main`.
2. Merge any integration branch into `dev` before bringing in fresh `main`
   updates, if that integration branch contains unfinished local work.
3. Merge `main` into `dev`.
4. For conflicts, consult the entries above before choosing either side.
5. Regenerate dependencies with `uv sync --extra local`.
6. Run focused tests for any listed local feature touched by the merge.
7. Commit and push `dev` to `fork/dev`.
8. Restart Hermes gateways and dashboards from the updated `hermes-dev`
   environment.
