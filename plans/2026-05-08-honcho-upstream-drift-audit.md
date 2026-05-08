# Honcho Upstream Drift Audit - 2026-05-08

## Records Found

No completed upstream drift audit was found in this checkout.

The only Honcho-specific record found was `plans/honcho-first-turn-continuity-handoff.md`. That file is a design handoff for first-turn continuity behavior, not an audit against upstream.

`plans/local-fork-notes` was not present.

## Refs Audited

- Local branch: `dev`
- Local HEAD before this audit: `142df041c2793240024b26a92577e2f628c2cd71`
- Upstream: `origin/main`
- Upstream HEAD after fetch: `242da9db9`
- Merge base: `601e5f1d57cfd4ceefee50a6df05a860a1a602e8`
- Divergence: 37 local-only commits, 505 upstream-only commits
- Worktree before audit changes: dirty, with an existing unrelated modification in `hermes_cli/gateway.py`

Because the upstream gap is large and the worktree already had a local edit, this audit did not start a full merge.

## High-Level Result

Upstream has completed the important architectural move: Honcho is a memory provider plugin under `plugins/memory/honcho`, and `run_agent.py` talks to it only through `MemoryManager` / `MemoryProvider`.

Local `dev` had partially aligned with that architecture, but also retained old core-Honcho code in `run_agent.py`. That code was not needed, was not referenced by the active memory-manager path, and duplicated plugin responsibilities.

## Cleanup Applied During Audit

Removed the dead legacy Honcho block from `run_agent.py`:

- `_register_honcho_exit_hook`
- `_queue_honcho_prefetch`
- `_honcho_prefetch`
- `_honcho_save_user_observation`
- `_honcho_sync`
- an unreachable old activation snippet after `is_interrupted`

This keeps the active Honcho integration on the upstream plugin architecture and does not remove active usage behavior, because active Honcho calls now flow through `_memory_manager`.

## Verification During Audit

- `python -m py_compile run_agent.py`: passed
- `rg -n "_honcho|honcho_" run_agent.py`: only the generic memory-provider tool comment remains
- `git diff --check`: passed
- `uv run pytest tests/honcho_plugin`: failed, 8 failed / 192 passed

The failing Honcho plugin tests are in the current local plugin surface, not in the `run_agent.py` cleanup. The failures match the drift findings below:

- `cmd_status` assumes a fake config without `observation_mode`
- empty-profile hint tests hit local `honcho_profile` JSON serialization errors from mocked card results
- these failures should be resolved during the upstream Honcho plugin merge/alignment rather than by adding more core-agent compatibility code

## Drift Findings

### 1. Core Agent Boundary

Upstream:

- `run_agent.py` initializes `MemoryManager`
- selected memory provider plugins are initialized through the provider interface
- Honcho-specific behavior stays in `plugins/memory/honcho`

Local before cleanup:

- `run_agent.py` had stale `_honcho_*` methods and activation code
- those methods referenced attributes that are no longer part of the active path
- the code duplicated responsibilities now owned by the plugin

Decision:

- Prefer upstream architecture.
- Keep `run_agent.py` Honcho-agnostic except generic memory-manager calls.

### 2. Honcho Plugin Surface

Upstream has a broader plugin surface than local `dev`:

- `honcho_reasoning` is a separate LLM-backed dialectic tool
- `honcho_context` is raw/session context rather than the synthesized reasoning path
- tools accept `peer` where relevant
- peer card update and conclusion deletion exist in the plugin tool layer
- dialectic depth, reasoning levels, liveness, backoff, stale-result handling, and trivial-prompt gating are implemented in the plugin

Local `dev` simplified this surface:

- `honcho_context` is the LLM-backed synthesized query
- no separate `honcho_reasoning`
- fewer peer-targeting paths
- `prefetch()` and `queue_prefetch()` are short-circuited with unconditional `return`

Decision:

- Prefer upstream plugin surface and tool split.
- Do not preserve local simplifications unless a specific usage regression is identified.
- If first-turn-only behavior remains desired, implement it through config/defaults, not unconditional returns.

### 3. First-Turn Continuity / Noisy Per-Turn Injection

Local feature to preserve:

- Avoid noisy per-turn Honcho dialectic injection.
- Keep continuity useful at session start.
- Preserve prompt-cache friendliness.

Upstream status:

- Upstream supports `injectionFrequency` and gates first-turn behavior in the plugin.
- Upstream defaults remain oriented around `every-turn` unless config says otherwise.
- Upstream injects live context through the memory-provider prefetch path, not legacy core code.

Recommended alignment:

- Use upstream `prefetch()` and `queue_prefetch()` implementation.
- Set desired local policy through `injectionFrequency: first-turn` for local/default setup if this remains a product decision.
- Avoid hard-disabling `queue_prefetch()`, because that also prevents upstream's context cache, cadence, stale result handling, and diagnostics from working.
- If the continuity handoff query is still desired, port it as a small plugin-level customization around the upstream prewarm query.

### 4. Gateway User Identity / Aliases

Local feature to preserve:

- Per-user memory scoping for gateway platforms.
- Ability to keep memory unified under a chosen identity when desired.

Upstream now covers the same architecture more cleanly:

- `runtime_user_peer_name` is passed into `HonchoSessionManager`
- `pinPeerName` controls whether configured `peerName` wins over runtime gateway identity
- `gateway_session_key` participates in Honcho session-name resolution

Decision:

- Prefer upstream mechanism.
- Drop local mutation of `cfg.peer_name` in provider initialization.
- Keep local gateway alias configuration only if it still adds user-facing alias mapping that upstream does not provide; pass the resolved alias as runtime identity rather than editing Honcho config.

### 5. Observation Model

Local feature to preserve:

- Directional or bidirectional observation controls.

Upstream status:

- Upstream has granular `observation` config, `observationMode`, peer targeting, and observer/target routing.
- Upstream is more complete than local `dev` here.

Decision:

- Prefer upstream implementation.
- Reapply only tests or docs that cover locally important gateway aliases or default behavior.

### 6. Queue Health Warnings

Local `dev` has Honcho queue health warning logic that upstream does not appear to retain in the same form.

Decision:

- Treat this as optional local value, not an architecture blocker.
- If preserved, keep it entirely inside `plugins/memory/honcho/session.py` and expose it through plugin prompt/tool output. Do not reintroduce core-agent Honcho references.

## Recommended Merge Strategy

Do not squash-merge stale local Honcho files over upstream. That would silently revert upstream plugin work.

Recommended sequence:

1. Commit or stash the unrelated `hermes_cli/gateway.py` work.
2. Create an integration branch from current `dev`.
3. Merge `origin/main`.
4. For Honcho conflicts, start from upstream for:
   - `run_agent.py`
   - `agent/memory_manager.py`
   - `agent/memory_provider.py`
   - `plugins/memory/honcho/__init__.py`
   - `plugins/memory/honcho/session.py`
   - `plugins/memory/honcho/client.py`
   - `plugins/memory/honcho/cli.py`
   - `plugins/memory/honcho/README.md`
5. Reapply only local usage-preserving deltas:
   - first-turn/no noisy per-turn policy through config/defaults
   - gateway alias resolution if not fully covered by upstream
   - optional queue health warning diagnostics
6. Run focused tests before broader suite:
   - `tests/honcho_plugin`
   - memory-manager tests
   - gateway alias/session-key tests
   - `tests/run_agent` cases covering memory-provider injection

## Bottom Line

Prefer upstream for the Honcho architecture and most plugin implementation. The local fork should keep only narrowly scoped behavior that affects actual usage: first-turn continuity policy, local gateway alias semantics, and possibly queue health diagnostics. Those should live in the Honcho plugin or gateway config path, not in `run_agent.py`.

## Merge Follow-Up - 2026-05-08

Integration branch `integrate/upstream-main-2026-05-08` merged `origin/main` after this audit.

Conflict decisions:

- `run_agent.py`: kept upstream message-sequence repair and `transform_llm_output` hook; retained local Gemini-native request preparation; no Honcho-specific core code reintroduced.
- `agent/transports/chat_completions.py`: kept upstream provider-profile architecture; retained local strict replay sanitization and native-Gemini thinking support in the legacy fallback.
- `agent/auxiliary_client.py`: kept upstream provider-profile default headers; retained local native-Gemini custom client routing.
- `plugins/memory/honcho/session.py`: kept upstream broader context shape (`summary`, user context, AI self context); retained local observation keys and queue-health warning diagnostics for compatibility with the local first-turn prompt block.
- `tools/browser_tool.py`: kept upstream Lightpanda-to-Chrome vision screenshot fallback; retained local standalone `browser_screenshot` tool.
- `uv.lock`: accepted upstream lockfile shape and dropped the local `[options] exclude-newer` block.

Focused verification after conflict resolution:

- `python -m py_compile agent/auxiliary_client.py agent/transports/chat_completions.py plugins/memory/honcho/session.py run_agent.py tools/browser_tool.py`: passed
- `git diff --check`: passed
- `uv run pytest tests/agent/transports/test_chat_completions.py tests/agent/test_auxiliary_client.py tests/run_agent/test_message_sequence_repair.py tests/test_transform_llm_output_hook.py tests/tools/test_browser_lightpanda.py tests/honcho_plugin/test_session.py`: 311 passed

## Additional Honcho Realignment - 2026-05-08

After reviewing remaining drift from `origin/main`, the largest Honcho divergence was still in the plugin surface itself. The local branch had kept a simplified tool model and short-circuited per-turn plugin prefetch behavior, while upstream now has a richer plugin-native architecture:

- separate `honcho_reasoning` for synthesized dialectic answers
- `honcho_context` for raw/session context
- peer-aware profile/search/context/reasoning/conclusion operations
- plugin-managed prefetch cadence, stale-thread handling, and diagnostics

Decision:

- Restore upstream versions of `plugins/memory/honcho/__init__.py`, `README.md`, `cli.py`, `client.py`, `session.py`, and the corresponding Honcho plugin tests.
- Do not preserve the local short-circuit behavior in code. If first-turn-only or quieter injection remains desired, use upstream configuration knobs such as `injectionFrequency` and cadence settings rather than bypassing plugin logic.
