# DEV2 → DEV3 parity ledger

## Verdict: source remediation landed; deployment HOLD — parity not yet complete

Baseline dev3: `e4ec680ce518e2ff2305afdd5dd188001803fb2d`. Dev2: `218299900abff8abdbd02861792d1f3255913a5b`. Upstream: `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`.
Backup ref: `backup/dev3-pre-dev2-parity` pins the exact baseline. This ledger classifies the **before** state; disposition identifies repairs or unresolved decisions. It does not relabel a missing before-state feature as already present after porting.

All 68 commits enumerated, including 20 merges. Buckets: 1=29, 2=16, 3=23. No commit is silently excluded.

## Remaining explicit decisions (release blockers)

- **D1 — input policy:** dev2 prioritizes human > requested-task feedback > external notifications, permits trusted requested-task feedback into a live steer-mode turn, keeps ordinary human steers parent-only, and unmutes a diagnostic turn on human intervention. Current dev3 uses durable FIFO, queues internal feedback, and upstream automatically fans human steers into active children. Typed provenance and lossless ticketed transfers do not replace these policies. Restoring the whole policy needs an explicit decision: preserve dev2 priority/live-feedback semantics within the unified admission model, or deliberately accept the changed user experience. `gateway/run_busy.py`, `gateway/session_authority.py`, `gateway/session_automation.py`, `agent/notification_presentation.py`. These paths were not changed by this remediation.
- **D2 — webhook discoverability:** dev2 auto-registered internal-turn webhook workers as retained delegates. The requested new default is explicitly `attach:false`; routing still works, but those workers are not automatically in the receiving conversation’s default delegate list. Decide whether historical worker attachments are preserved during DB migration (and new ones opt-in), or deliberately retired. Delivery-store migration does not rewrite session/attachment rows.
- **D3 — explicit empty fallback:** dev2 treated route `fallback_providers: []` as inheriting platform defaults; unified canonical routes treat it as an explicit disabled chain. Absence/null inherits defaults. Audit private effective configuration and either materialize the old inherited chain for affected routes in the one-time migration or explicitly accept disable semantics. No live configuration was inspected. The delivery converter preserves chain values and reports this as an operator gate; it does not guess a replacement chain.
- **D4 — legacy plugin/control protocol:** dev2 exposed `ctx.internal_turns.enqueue/enqueue_home`, `query_gateway_control(..., payload=...)`, and socket verb `enqueue_internal_turn`. All are absent from dev3. Cron/webhook built-in callers are migrated, but this does not preserve an external/plugin caller contract. The pinned typed companion contains no references to those old entry points (Python source search), not proof no other caller exists. The old protocol bypassed inbound-user authorization using local socket trust; unified typed admission has actor/profile authority. Decide to add a narrowly authorized compatibility adapter (not resurrect the parallel service), or explicitly retire the protocol after consumer inventory/migration. No old permissive verb was reintroduced without authority review. Sources: dev2 `gateway/internal_turns.py`, `gateway/control_socket.py`, `gateway/run_bootstrap.py`; no matches in pinned dev3 outside migration documentation.
- **Narrowed legacy addressing:** internal cron session delivery now requires one resolvable conversation. Broadcast/multi-target internal addresses and cross-profile Bot Chat/room tokens fail closed instead of partial delivery. Direct fan-out remains. A private-record inventory must identify any such old entries and choose canonical targets; do not describe rejection as full capability parity.

## Restored feature inventory and verification

- Cron `job_type=agent|script|reminder`, `no_agent` alias, literal whitespace/SILENT reminder payload, no detached reminder model/worker, default immediate session delivery, explicit attach and canonical targets; fields roundtrip through tool, import, CLI, REST and dashboard. Runtime unresolved/malformed internal targets fail closed. Script worker session handoffs use the gateway durable queue. Tests: `tests/cron/test_reminder_session_parity.py`, `tests/hermes_cli/test_cron_reminder_surfaces.py`, `web/src/lib/cron-job.test.ts` plus focused scheduler suites.
- Webhook compatibility: rendered legacy destination → immediate session target, final-only handoff, no raw platform fallback after missing/failed stamp/read/dispatch; keyed per-delivery targets and exact ledger recovery identity; detached silence, legacy retry pins, role-scoped worker instructions, frozen model/provider/default/chain, retry-primary behavior, CLI/dashboard fields. Tests: `tests/gateway/test_webhook_legacy_handoff.py`, `test_webhook_route_fallback.py`, existing keyed/wire/destination suites and `tests/hermes_cli/test_webhook_handoff_surfaces.py`.
- Process completion: delegate-owned implicit wakes refused without handoff, explicit resumed-child/compression ownership retained, live handoff target supersedes watcher snapshot, inherited fork markers no longer break compression, deterministic no-authority bad routes consume bounded attempts. Tests: `test_delegate_notification_route.py`, `test_legacy_notification_route_retry.py`, `test_compression_lineage_guard.py`, process handoff/watch suites.
- Delegation result display: child session identity/title/resume metadata and bounded goal/steer snippets; full structured missed-steer values retained. Test: `tests/tools/test_delegation_completion_snippets.py`.
- One-time migration: `scripts/migrate_delivery_targets.py`, dry-run default, two-store preflight, explicit target mappings for ambiguous/group/template destinations, no ambient configuration or DB reads, exclusive 0600 byte backups, atomic file replacement/readback, rollback after partial publication, idempotent rerun, optional explicit pause-all. It **retains** compatibility delivery_mode to preserve webhook final-only behavior while adding canonical targets. Test: `tests/scripts/test_migrate_delivery_targets.py`.

## Execution evidence and boundaries

Canonical Python tests always use `nice -n 15 scripts/run_tests.sh -j 1`. The home-I/O guard prevents certain imports inside profile-owned scratch; source-matched disposable copies under `/home/sonya/ykhos-ws/.cache` were used and removed, without weakening the guard. Later runs pin an existing interpreter and source-matched activation to forbid provisioning.
Focused receipts are retained in `/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/`: cron surface run 394 passes/16 files; noncron unique latest-file union 482 passes/14 files; webhook comprehensive 372 passes/31 files, then final target/runtime rechecks. These overlap: **do not sum them as unique tests**. Final integrated regression receipt is `migration/surface-tests.log`: 219 passes across 18 files on the final source tree, covering all new behavior families together. Earlier 217-pass integration and 34-pass reminder/migration recheck receipts are retained separately (overlapping). Web helper run 22 passes; app TypeScript including both changed pages passes. Stock typecheck remains blocked by pre-existing missing `@rolldown/plugin-babel` in Vite configuration. No full suite/build/browser/live provider canary claim.
Execution caveat: the first noncron runner inherited stale activation and invoked setup/PM; its log reports Python/tool/test-environment dependency installation succeeded before the outer launcher failed “no bootstrap Python.” Unintended provisioning therefore occurred, not merely a dry-run attempt. Exact effects beyond the installation log were not independently inventoried; do not remove shared generations to conceal it. Receipt: `audit/implementation-existing-tests.log`. No intentional service operation or live jobs/state/plugin write was performed. Later tests used explicit non-provisioning activation.

## Exact commit ledger

Bucket 1 and 2 evidence below refers exclusively to pinned baseline e4ec680ce5, not a moving checkout. Merge rows additionally have remerge-resolution and upstream ancestry evidence in the detailed JSON. Internal adapter names can be superseded; dropped observable policy is explicitly bucket 3.

### 01. `9bb41d504e3ad7c3a1be145e1024a604558d5faa` — feat(update): preserve configured integration branches
**Bucket 1: Already in dev3**. Configured remote/branch/integration lane is implemented in the split updater; later merge repairs are folded into the port, not a patch-identical cherry-pick.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}]
Original-to-port evidence: {'original': '9bb41d504e3ad7c3a1be145e1024a604558d5faa', 'port': '6756bf2be3967f2b4b77ce759a4816a975eced33', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/9bb41d504e3a.port-range-diff.txt', 'diff_chars': 174}

### 02. `c4ba0f1b14635d1101d49890e45a59dd28e2e27a` — feat: deliver async jobs as internal turns
**Bucket 3: Missing or regressed on pinned dev3**. Cron reminder/session-delivery/tool/CLI/dashboard contracts were absent; now ported. InternalTurnService is replaced by typed unified admissions. Diagnostic scheduling remains D1; historical automatic webhook listing remains D2. Legacy ctx.internal_turns / enqueue_internal_turn control protocol is absent: D4.
Disposition: Cron/webhook ported; D1/D2 still open. D4 legacy API compatibility is OPEN.

### 03. `372243cd79536ac10a8d9d5b838c1fbf5ac4dd2e` — Merge remote-tracking branch 'origin/main' into dev2
**Bucket 2: Intentionally superseded with capability parity**. No remerge-resolution delta; upstream parent is reachable from pinned dev3.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 04. `7656b1e3d45ad2a8d65ace5786568672531a8e36` — Merge remote-tracking branch 'origin/main' into dev2
**Bucket 2: Intentionally superseded with capability parity**. No remerge-resolution delta; upstream parent is reachable from pinned dev3.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 05. `4355cb1e17bdd5cd63770f61e56397ebf4819923` — Merge remote-tracking branch 'origin/main' into dev2
**Bucket 3: Missing or regressed on pinned dev3**. Non-cron custom test seam for the integration updater is retained; control-socket request-handler composition and cron args are reconciled in the cron/webhook feature inventory. Legacy ctx.internal_turns / enqueue_internal_turn control protocol is absent: D4.
Disposition: Ported in this parity patch; see feature/tests below. D4 legacy API compatibility is OPEN.
Baseline evidence: [{'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}]

### 06. `4a5c68f0d9a545e92b247e7c7866c0212c4ad893` — Merge remote-tracking branch 'origin/main' into dev2
**Bucket 2: Intentionally superseded with capability parity**. No remerge-resolution delta; upstream parent is reachable from pinned dev3.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 07. `8f20d0a5707c9bac56eeea4d982f30389b91eb9c` — Merge remote-tracking branch 'origin/main' into dev2
**Bucket 1: Already in dev3**. Selected-remote update checking/authoritative non-fork path is preserved by the modern target selection; internal_notification forwarding are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}]

### 08. `000e2fac1c6d38291e7b2233995ec6595c23135f` — Merge remote-tracking branch 'origin/main' into dev2
**Bucket 2: Intentionally superseded with capability parity**. No remerge-resolution delta; upstream parent is reachable from pinned dev3.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 09. `9b39d5f9450e8c802ff357b8aa301c68ab585059` — Merge origin/main into dev2; port internal delivery and integration updater to split modules
**Bucket 3: Missing or regressed on pinned dev3**. Manually ported integration updater into main_install_repair/update_cmd_git/web_server_config remains implemented. Source caching, profile authorization, typing post-delivery cleanup are current upstream implementations. Internal-turn split-module propagation are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps. Legacy ctx.internal_turns / enqueue_internal_turn control protocol is absent: D4.
Disposition: Ported in this parity patch; see feature/tests below. D4 legacy API compatibility is OPEN.
Baseline evidence: [{'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}, {'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 10. `a61ec3baebb9ab302ca7511936fe1a7388adba77` — Merge origin/main into dev2; compose wake receipts and paused cron creation
**Bucket 3: Missing or regressed on pinned dev3**. Updater fixtures remain; user-ahead-of-background pending FIFO policy is not retained (same surviving non-cron policy gap as be949dc). Paused-cron and wake-receipt specifics are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps.
Disposition: OPEN — D1 policy decision; do not call parity complete.
Baseline evidence: [{'group': 'input', 'verdict': 'missing', 'paths': ['gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'agent/notification_presentation.py', 'gateway/run_turn_runner.py', 'gateway/session_automation.py', 'gateway/session_authority.py', 'gateway/run_turn.py', 'gateway/run_turn.py']}, {'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}]

### 11. `64f30ac17bb22428aaf2efeeac783ee11292b457` — Merge remote-tracking branch 'origin/main' into dev2
**Bucket 2: Intentionally superseded with capability parity**. No remerge-resolution delta; upstream parent is reachable from pinned dev3.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 12. `53ddcfb2784f066728bfc505d9a31c219d362d86` — fix(webhook): preserve internal-turn routing across worker dispatch
**Bucket 3: Missing or regressed on pinned dev3**. Webhook internal delivery routing was dropped; now preserved in immutable per-delivery target snapshots and fail-closed adapter sends.
Disposition: Ported in this parity patch; see feature/tests below.

### 13. `f273c5662637131a765ae3309924b1e144812b69` — Merge origin/main into dev2; preserve internal context through queued turns and conversation wrapper
**Bucket 2: Intentionally superseded with capability parity**. Legacy internal_notification parameter threading is superseded by typed system_event parts, retained producer admissions and native envelopes. No separate InternalTurnService API is resurrected; presentation scheduling is separately blocked in D1.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.

### 14. `1554be731c1f94ba1b2aa882f1f16c83bf035101` — fix: allow passive background memory consolidation
**Bucket 1: Already in dev3**. Background additions/consolidating patch follow write_approval; raw remove stages whole batch. Upstream renamed replace to patch, intentionally preserved by port.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'memory', 'verdict': 'already_present', 'paths': ['tools/memory_tool.py', 'tools/memory_tool.py']}]
Original-to-port evidence: {'original': '1554be731c1f94ba1b2aa882f1f16c83bf035101', 'port': '83b27eb4c67b7f8e1d317f06a805aeaf1715b39f', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/1554be731c1f.port-range-diff.txt', 'diff_chars': 17810}

### 15. `0b2dd6779bd8cc1a656eee9df0c2fed754ec1ea8` — Merge origin/main into dev2; preserve internal delivery and passive memory behavior
**Bucket 1: Already in dev3**. Passive-memory behavior survives; rescan-profiles control verb and lockfile metadata are upstream/superseded. Internal-turn/webhook forwarding are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'memory', 'verdict': 'already_present', 'paths': ['tools/memory_tool.py', 'tools/memory_tool.py']}, {'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 16. `f67af81d23cdaea52c192afacac7fac760e7596a` — Merge origin/main into dev2 preserving internal delivery and update handoff policy
**Bucket 3: Missing or regressed on pinned dev3**. Fresh-process post-swap completion now consumes the pinned expected_sha; all integration-lane Git moves happen before handoff, so old remote/in_place_update finish kwargs are no longer needed. Original tests adapted in current integration/update-finish tests. Control request composition are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps. Legacy ctx.internal_turns / enqueue_internal_turn control protocol is absent: D4.
Disposition: Ported in this parity patch; see feature/tests below. D4 legacy API compatibility is OPEN.
Baseline evidence: [{'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}]

### 17. `0f30bbf1811c2c608b2c0bb73a730162c4b5b311` — fix(gateway): align standalone completion keys and bound bad-route retries
**Bucket 3: Missing or regressed on pinned dev3**. Normal standalone namespace is superseded by RoutingIdentity, but the still-active no-authority adapter route-mismatch branch is not marked permanent and its refused delivery refunds retries forever. Modern authority refusal codes cover other cases, not this fallback.
Disposition: Ported in this parity patch; see feature/tests below.
Baseline evidence: [{'group': 'route_retry', 'verdict': 'missing', 'paths': ['gateway/platforms/base.py', 'gateway/platforms/base.py', 'gateway/wake.py', 'gateway/run_notifications.py', 'gateway/run_notifications.py']}]

### 18. `b29fca7e93659bda022363008de725723a8d64d7` — fix(mcp): preserve bounded structured recovery details on tool errors
**Bucket 1: Already in dev3**. Structured tool-error recovery fields are capped/redacted recursively and forwarded alongside generic error prose.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'mcp_errors', 'verdict': 'already_present', 'paths': ['tools/mcp_tool_handlers.py', 'tools/mcp_tool_handlers.py']}]
Original-to-port evidence: {'original': 'b29fca7e93659bda022363008de725723a8d64d7', 'port': '4171e13eac2ee76727bc900454a90fe7cafbf656', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/b29fca7e9365.port-range-diff.txt', 'diff_chars': 2822}

### 19. `87ebb5fd7fda27c3f1a0b22943dc45d296acc61f` — fix(mcp): identify native client sessions as Hermes
**Bucket 2: Intentionally superseded with capability parity**. Upstream now sets Hermes client_info conditionally for SDK compatibility and protocol fallback; original unconditional kwargs patch is obsolete, behavior retained.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'mcp_identity', 'verdict': 'fully_superseded', 'paths': ['tools/mcp_tool_transport.py', 'tools/mcp_tool_transport.py']}]

### 20. `ad2c44cd6ff41a7c3c4b59f8121ac2854fc03fd8` — Merge remote-tracking branch 'origin/main' into HEAD
**Bucket 2: Intentionally superseded with capability parity**. No remerge-resolution delta; upstream parent is reachable from pinned dev3.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 21. `d419fca6adcd15ffd436ccd2130bcf722c217eb5` — feat(delegation): inherit conversation context with role-specific instructions
**Bucket 1: Already in dev3**. Profile-local main/subagent instructions and assignment scope are preserved. Original default filtered fork policy was intentionally replaced by the later dev2 plugin-owned opt-in formatter (9309113,7127952), also in dev3.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'roles', 'verdict': 'already_present', 'paths': ['agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/system_prompt.py']}, {'group': 'fork', 'verdict': 'already_present', 'paths': ['agent/subagent_context.py', 'agent/subagent_extension.py', 'agent/subagent_fork_validation.py', 'tools/delegate_tool_progress.py']}]
Original-to-port evidence: {'original': 'd419fca6adcd15ffd436ccd2130bcf722c217eb5', 'port': '62e92cb01ee7ef655722f32cb936c72ed67b8ca2', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/d419fca6adcd.port-range-diff.txt', 'diff_chars': 34619}

### 22. `70bb6f237047f3255be225e78187fa0515b08144` — fix(internal-turns): give handoff workers scoped instructions
**Bucket 3: Missing or regressed on pinned dev3**. Worker subagent instruction selection restored in cron scheduler and authorized webhook worker construction; receiving session retains ordinary instructions.
Disposition: Ported in this parity patch; see feature/tests below.

### 23. `deba0dd672e14a55efc5e318f94598d6783ef6f7` — feat(delegation): route individual tasks by configured difficulty
**Bucket 2: Intentionally superseded with capability parity**. Later dev2 9309113 removed core-owned difficulty routing in favor of the delegation-provider backend route and plugin schema; dev3 retains that architecture. External plugin policy is outside this repository audit.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'routing', 'verdict': 'fully_superseded', 'paths': ['agent/delegation_provider.py', 'tools/delegate_tool_routes.py', 'tools/delegate_tool.py', 'agent/subagent_sessions.py']}]

### 24. `a3abb4e60647f257e0d53e1ac7062cd00b34eb3f` — fix(gateway): accept explicit silence after confirmed response delivery
**Bucket 1: Already in dev3**. Only confirmed same-home/destination tool/stream delivery authorizes explicit silence; standalone blank diagnostics and substantive final output remain distinct.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'silence', 'verdict': 'already_present', 'paths': ['gateway/response_delivery.py', 'gateway/stream_consumer.py', 'gateway/run_turn_persistence.py', 'tools/send_message_tool.py']}]
Original-to-port evidence: {'original': 'a3abb4e60647f257e0d53e1ac7062cd00b34eb3f', 'port': 'f99918e3efcf9f94ee01238de1014f7af98fd07f', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/a3abb4e60647.port-range-diff.txt', 'diff_chars': 11622}

### 25. `4a8b70b24aa7f2b69d2d777096373ec1fd864fe4` — feat(tools): allow eager local tool schemas for delegated agents
**Bucket 1: Already in dev3**. Opt-in delegated children inline permitted local schemas; does not grant new tools or bypass remote discovery.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'eager', 'verdict': 'already_present', 'paths': ['tools/tool_search.py', 'tools/tool_search.py', 'tools/tool_search.py']}]
Original-to-port evidence: {'original': '4a8b70b24aa7f2b69d2d777096373ec1fd864fe4', 'port': '5ab7a58d90a2ed8ef572dfe523eb53d3fa921e70', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/4a8b70b24aa7.port-range-diff.txt', 'diff_chars': 4970}

### 26. `ebeab049cba0a57ad16812697faf8ccbc325b7cb` — fix(delegation): reject unsupported difficulty transport overrides
**Bucket 2: Intentionally superseded with capability parity**. Difficulty transport overrides were removed from core by later dev2 9309113. Core TaskRoute schema rejects unsupported fields; plugin backend settings validation is plugin-owned and not inspected.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'routing', 'verdict': 'fully_superseded', 'paths': ['agent/delegation_provider.py', 'tools/delegate_tool_routes.py', 'tools/delegate_tool.py', 'agent/subagent_sessions.py']}]

### 27. `be949dc8162397fe65dfe5effedb8fece8381873` — fix(gateway): prioritize user and task feedback with lossless turn handoffs
**Bucket 3: Missing or regressed on pinned dev3**. Typed rendering is superseded by MessagePart/renderer, but the user/task/external scheduling priorities, parent-only human steering, trusted task-result live steering, and diagnostic unmute upon human intervention are not preserved.
Disposition: OPEN — D1 policy decision; do not call parity complete.
Baseline evidence: [{'group': 'input', 'verdict': 'missing', 'paths': ['gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'agent/notification_presentation.py', 'gateway/run_turn_runner.py', 'gateway/session_automation.py', 'gateway/session_authority.py', 'gateway/run_turn.py', 'gateway/run_turn.py']}]

### 28. `6514134d6c246b240ca9698807b3cf5ec8368152` — feat(mcp): allow expired startup schemas for opt-in lazy servers
**Bucket 1: Already in dev3**. Opt-in lazy_cache_ignore_ttl accepts expired startup schemas but never bypasses connection fingerprint; later discovery still connects/refreshes.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'mcp_lazy', 'verdict': 'already_present', 'paths': ['tools/mcp_schema_cache.py', 'tools/mcp_tool_discovery.py']}]
Original-to-port evidence: {'original': '6514134d6c246b240ca9698807b3cf5ec8368152', 'port': 'b5ca88f8a477b9551566cae5812f5d0f19cbf6c6', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/6514134d6c24.port-range-diff.txt', 'diff_chars': 4277}

### 29. `36106362b7371b0754f7670a8b1d446efee4ca90` — Merge origin/main into dev2 preserving delivery, routing, and memory
**Bucket 1: Already in dev3**. Merge added child load_soul_identity=True with context skipped; current constructor preserves it. Side-agent/image scope and delivery-adapter restoration are upstream equivalents. Internal-turn propagation are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'roles', 'verdict': 'already_present', 'paths': ['agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/system_prompt.py']}, {'group': 'eager', 'verdict': 'already_present', 'paths': ['tools/tool_search.py', 'tools/tool_search.py', 'tools/tool_search.py']}, {'group': 'mcp_errors', 'verdict': 'already_present', 'paths': ['tools/mcp_tool_handlers.py', 'tools/mcp_tool_handlers.py']}, {'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 30. `01fb54ab29054ed809ff35550d9165aeeea759a2` — Merge remote-tracking branch 'origin/main' into test/model-update-20260922
**Bucket 2: Intentionally superseded with capability parity**. No remerge-resolution delta; upstream parent is reachable from pinned dev3.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'upstream', 'verdict': 'fully_superseded', 'paths': []}]

### 31. `b5335615ba16d953a59e6cde401aea3518486ec3` — Merge September 30 upstream, preserving personal runtime behavior
**Bucket 3: Missing or regressed on pinned dev3**. PM/update-channel integration and memory remove gate are retained; current ordinary silence receipts retained. The merge's user-input-over-muted-wake presentation policy is missing with be949dc. Internal-turn/webhook and cron specifics are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps.
Disposition: OPEN — D1 policy decision; do not call parity complete.
Baseline evidence: [{'group': 'input', 'verdict': 'missing', 'paths': ['gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'agent/notification_presentation.py', 'gateway/run_turn_runner.py', 'gateway/session_automation.py', 'gateway/session_authority.py', 'gateway/run_turn.py', 'gateway/run_turn.py']}, {'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}, {'group': 'memory', 'verdict': 'already_present', 'paths': ['tools/memory_tool.py', 'tools/memory_tool.py']}, {'group': 'silence', 'verdict': 'already_present', 'paths': ['gateway/response_delivery.py', 'gateway/stream_consumer.py', 'gateway/run_turn_persistence.py', 'tools/send_message_tool.py']}]

### 32. `9c47293e8773f853f87c9949e2d255706c2a92bf` — feat(delegation): allow per-task model and provider overrides
**Bucket 2: Intentionally superseded with capability parity**. Later dev2 9309113 intentionally removed core task model/provider surface. Explicit child selectors are supplied by the selected delegation backend and rejected when unsupported; resume re-resolves stored routes.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'routing', 'verdict': 'fully_superseded', 'paths': ['agent/delegation_provider.py', 'tools/delegate_tool_routes.py', 'tools/delegate_tool.py', 'agent/subagent_sessions.py']}]

### 33. `3e7464d942974a1f8c563340fe2c9397cce32f30` — feat(tools): allow selective eager tool schemas for delegated children
**Bucket 1: Already in dev3**. eager_subagent_tools is a validated set intersected with incoming permitted schemas and applies to children only.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'eager', 'verdict': 'already_present', 'paths': ['tools/tool_search.py', 'tools/tool_search.py', 'tools/tool_search.py']}]
Original-to-port evidence: {'original': '3e7464d942974a1f8c563340fe2c9397cce32f30', 'port': '22751ea156ffc860af843d3acea9391b5dd7caec', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/3e7464d94297.port-range-diff.txt', 'diff_chars': 3749}

### 34. `1e1dc722724821e00d9a2b69099d9e9f588ea89b` — Merge upstream main into dev2 and preserve personal runtime contracts
**Bucket 1: Already in dev3**. Assignment-scoped execution guidance remains; current live-log metadata follows backend routes. Cron/webhook router validation are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'roles', 'verdict': 'already_present', 'paths': ['agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/system_prompt.py']}, {'group': 'routing', 'verdict': 'fully_superseded', 'paths': ['agent/delegation_provider.py', 'tools/delegate_tool_routes.py', 'tools/delegate_tool.py', 'agent/subagent_sessions.py']}]

### 35. `5bcb399f5e6f17903f0f87467d03080ab9d06d97` — Merge October 11 upstream preserving personal runtime contracts
**Bucket 1: Already in dev3**. Pinned integration merge target SHA, configured remote tracking refspec, unchanged role-specific instruction loading, opt-in lazy schema/eager exceptions, and patch/remove memory policy survive. Legacy route result metadata replaced by backend route report and unified session identity.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}, {'group': 'roles', 'verdict': 'already_present', 'paths': ['agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/system_prompt.py']}, {'group': 'eager', 'verdict': 'already_present', 'paths': ['tools/tool_search.py', 'tools/tool_search.py', 'tools/tool_search.py']}, {'group': 'mcp_lazy', 'verdict': 'already_present', 'paths': ['tools/mcp_schema_cache.py', 'tools/mcp_tool_discovery.py']}, {'group': 'memory', 'verdict': 'already_present', 'paths': ['tools/memory_tool.py', 'tools/memory_tool.py']}]

### 36. `795b3e9b249c256b77a85a25a4cd91ecab467edd` — fix(steering): preserve human intervention through typed input admission
**Bucket 2: Intentionally superseded with capability parity**. Old enqueue_inputs API removed. Current InterruptControlMixin.steer/redirect set _turn_user_intervened on human admission, and turn result reports it; task-only inputs are typed system events instead of human steers.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'human_intervention', 'verdict': 'fully_superseded', 'paths': ['agent/interrupt_control.py', 'agent/interrupt_control.py', 'agent/conversation_loop.py']}]

### 37. `c12867e873794ed7567244bec258329a7633f1ca` — test(update): preserve torn-switch coverage with configured remotes
**Bucket 1: Already in dev3**. Torn-switch updater test passes explicit origin to the remote-aware seam; patch-equivalent port recorded.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}]
Original-to-port evidence: {'original': 'c12867e873794ed7567244bec258329a7633f1ca', 'port': '2967da654759e29abdeece99e67e9c702a780844', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/c12867e87379.port-range-diff.txt', 'diff_chars': 550}

### 38. `562d354497ab6d720caa2fb8f6707b84a952eab5` — style: align personal runtime additions with upstream lint rules
**Bucket 2: Intentionally superseded with capability parity**. Annotation/typing/import-only changes. Surviving code uses current annotations; deleted personal APIs have no remaining runtime contract. No behavior patch required.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'style', 'verdict': 'fully_superseded', 'paths': ['tools/delegation_live_log.py']}]

### 39. `0a9b9a2c381021e1f38d7386934ad2de3071a524` — feat(delegation): retain child sessions through the plugin lifecycle
**Bucket 2: Intentionally superseded with capability parity**. Core retention/extension hooks remain; separate delegate_sessions table and lock tokens replaced by sessions/session_admissions, durable generation claims and leased ACLs. Delegated skills review cancellation/read-version guards remain. This is not an absent filename finding.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'retention', 'verdict': 'fully_superseded', 'paths': ['tools/delegate_sessions.py', 'tools/delegate_sessions.py', 'tools/delegate_session_store.py', 'tools/delegate_session_store.py', 'tools/delegate_session_store.py']}, {'group': 'review', 'verdict': 'already_present', 'paths': ['agent/delegated_review.py', 'agent/delegated_review.py', 'tools/skill_manager_guards.py', 'tools/skill_manager_guards.py', 'tools/skill_manager_guards.py']}, {'group': 'fork', 'verdict': 'already_present', 'paths': ['agent/subagent_context.py', 'agent/subagent_extension.py', 'agent/subagent_fork_validation.py', 'tools/delegate_tool_progress.py']}]
Original-to-port evidence: {'original': '0a9b9a2c381021e1f38d7386934ad2de3071a524', 'port': '484dead573111bb90f601d2b8e74e6e6cfd238e4', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/0a9b9a2c3810.port-range-diff.txt', 'diff_chars': 41030}

### 40. `8fef41dff1bf40126dcd062427ee8cf269934bea` — fix(delegation): preserve host-selected background mode through extensions
**Bucket 1: Already in dev3**. Host-selected background value overwrites model-facing extension request before dispatch.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'host_background', 'verdict': 'already_present', 'paths': ['tools/delegate_tool.py']}]
Original-to-port evidence: {'original': '8fef41dff1bf40126dcd062427ee8cf269934bea', 'port': '210f4f0c015faef35ceb3cac32fc763f4e3448f7', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/8fef41dff1bf.port-range-diff.txt', 'diff_chars': 480}

### 41. `e78cf7d462d3009239d34347a5985bad2997985f` — feat: integrate guarded native Anthropic provider support
**Bucket 1: Already in dev3**. Guarded provider-aware native factory, provenance-keyed clients, terminal ProviderContractError propagation, thinking replay and auxiliary routing are ported; upstream retry/stream/compression lifecycle changes composed, not overwritten.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'native', 'verdict': 'already_present', 'paths': ['providers/anthropic.py', 'agent/anthropic_adapter.py', 'agent/anthropic_thinking_policy.py', 'agent/client_lifecycle.py', 'agent/turn_api_error.py']}]
Original-to-port evidence: {'original': 'e78cf7d462d3009239d34347a5985bad2997985f', 'port': '7836791ac0cbab6d0377d9d86e0e0c772718fdfb', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/e78cf7d462d3.port-range-diff.txt', 'diff_chars': 12298}

### 42. `a5c484ce90b1c15661352a1a483e83071026fbcc` — feat: let providers own Anthropic cache breakpoints
**Bucket 1: Already in dev3**. Loaded profile anthropic_prompt_cache_upstream_owned opts out of Hermes cache breakpoints independently of native signed-thinking support.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'native_cache', 'verdict': 'already_present', 'paths': ['agent/agent_runtime_helpers.py', 'providers/base.py']}]
Original-to-port evidence: {'original': 'a5c484ce90b1c15661352a1a483e83071026fbcc', 'port': '28d471abec01009a1b2a64d1d926b3f5f446295f', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/a5c484ce90b1.port-range-diff.txt', 'diff_chars': 1728}

### 43. `25fceaf158bb1e936464a51c720ab75dfa01e7dc` — Merge upstream main into dev2 with runtime contract repairs
**Bucket 3: Missing or regressed on pinned dev3**. Safe-worker role guards, memory patch/remove policy and retained lifecycle/review hooks are present. Merge's received-user-input silence override and notification_turn(on_resume=...) are absent with the generic user/task policy gap. Cron/internal-turn/webhook specifics are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps.
Disposition: OPEN — D1 policy decision; do not call parity complete.
Baseline evidence: [{'group': 'input', 'verdict': 'missing', 'paths': ['gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'agent/notification_presentation.py', 'gateway/run_turn_runner.py', 'gateway/session_automation.py', 'gateway/session_authority.py', 'gateway/run_turn.py', 'gateway/run_turn.py']}, {'group': 'memory', 'verdict': 'already_present', 'paths': ['tools/memory_tool.py', 'tools/memory_tool.py']}, {'group': 'review', 'verdict': 'already_present', 'paths': ['agent/delegated_review.py', 'agent/delegated_review.py', 'tools/skill_manager_guards.py', 'tools/skill_manager_guards.py', 'tools/skill_manager_guards.py']}, {'group': 'roles', 'verdict': 'already_present', 'paths': ['agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/prompt_builder.py', 'agent/system_prompt.py']}]

### 44. `01f472587be9446dad8e6980edf36a048c977165` — fix(gateway): retain task feedback across durable automation admission
**Bucket 3: Missing or regressed on pinned dev3**. Typed producer identity is superseded by typed_sender + system_event and persists, but task_feedback restoration/live-steer contract is absent: all internal work feedback still queues rather than joining a live steer-mode turn. Webhook instruction-role test hunk are reconciled by the typed-admission/cron/webhook feature inventory; see D1–D4 for remaining contract gaps.
Disposition: OPEN — D1 policy decision; do not call parity complete.
Baseline evidence: [{'group': 'input', 'verdict': 'missing', 'paths': ['gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'gateway/run_busy.py', 'agent/notification_presentation.py', 'gateway/run_turn_runner.py', 'gateway/session_automation.py', 'gateway/session_authority.py', 'gateway/run_turn.py', 'gateway/run_turn.py']}]

### 45. `ad424490e27253d476c364b660e1d97738e7e59d` — feat(webhook): pin detached worker model and provider
**Bucket 3: Missing or regressed on pinned dev3**. Worker model/provider/default pins, precommit frozen routes, fallback chains and CLI/dashboard fields restored through webhook_route_runtime and session execution.
Disposition: Ported in this parity patch; see feature/tests below.

### 46. `d2ea5b3ae45537e7a20db1b55147ea914bdedf74` — fix(delegation): preserve tool context in bounded observations (#4)
**Bucket 2: Intentionally superseded with capability parity**. Old JSON tool_calls/target/command_preview shape is superseded by agent.session_views: target-first tool argument summary (120 chars), capped head-tail result/prose views and bounded SQL newest-N. Tool context preserved with current regression tests, not the legacy output schema.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'observation', 'verdict': 'fully_superseded', 'paths': ['agent/session_views.py', 'agent/session_views.py', 'agent/session_views.py', 'agent/session_directory.py', 'tests/tools/test_delegate_observation.py']}]
Original-to-port evidence: {'original': 'd2ea5b3ae45537e7a20db1b55147ea914bdedf74', 'port': '86eb6f3e6316f44668ebb79075e247813bc17767', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/d2ea5b3ae455.port-range-diff.txt', 'diff_chars': 492}

### 47. `bf7a4eff0893d41b985fb34750cb0a96dfd8c601` — Merge upstream main into dev2 with delegation and durable input repairs
**Bucket 1: Already in dev3**. Configured-remote damaged-fetch diagnostics, retained-profile-route refusal, plugin-owned task route preflight, child runtime/workspace stamping and ticket-aware steer cleanup survive or are superseded by unified admission. Old core task model/provider/difficulty overlay is intentionally removed by later dev2 9309113. Durable task-feedback policy treated separately in 27/44.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'routing', 'verdict': 'fully_superseded', 'paths': ['agent/delegation_provider.py', 'tools/delegate_tool_routes.py', 'tools/delegate_tool.py', 'agent/subagent_sessions.py']}, {'group': 'retention', 'verdict': 'fully_superseded', 'paths': ['tools/delegate_sessions.py', 'tools/delegate_sessions.py', 'tools/delegate_session_store.py', 'tools/delegate_session_store.py', 'tools/delegate_session_store.py']}, {'group': 'update', 'verdict': 'already_present', 'paths': ['hermes_cli/main_install_repair.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd.py', 'hermes_cli/update_cmd_git.py']}, {'group': 'human_intervention', 'verdict': 'fully_superseded', 'paths': ['agent/interrupt_control.py', 'agent/interrupt_control.py', 'agent/conversation_loop.py']}, {'group': 'mcp_identity', 'verdict': 'fully_superseded', 'paths': ['tools/mcp_tool_transport.py', 'tools/mcp_tool_transport.py']}]

### 48. `a8a872f2a6615a5f3dcd0bd10358eb9f6c0cf6a5` — test: align integration fixtures with upstream build and routing contracts
**Bucket 1: Already in dev3**. Current fixtures accept upstream build hooks, routing completion model, and MCP session client_info; complete patch-equivalence evidence retained where applicable.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'integration_tests', 'verdict': 'already_present', 'paths': ['tests/tools/test_approval_gateway_wait_coalesce_race.py']}]
Original-to-port evidence: {'original': 'a8a872f2a6615a5f3dcd0bd10358eb9f6c0cf6a5', 'port': 'fe5d0cbe11def7fb7ebcf760652a678fb8307c16', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/a8a872f2a661.port-range-diff.txt', 'diff_chars': 214}

### 49. `9309113caa93ae35b90729b7a0801c56d954a146` — refactor(delegation): sync retained sessions with plugin-owned routing
**Bucket 1: Already in dev3**. Plugin-owned routes, no core default fork, per-task child options, task_parameters reserved-key guard and stable route reconstruction all remain, with unified runner replacing legacy registry operations.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'routing', 'verdict': 'fully_superseded', 'paths': ['agent/delegation_provider.py', 'tools/delegate_tool_routes.py', 'tools/delegate_tool.py', 'agent/subagent_sessions.py']}, {'group': 'fork', 'verdict': 'already_present', 'paths': ['agent/subagent_context.py', 'agent/subagent_extension.py', 'agent/subagent_fork_validation.py', 'tools/delegate_tool_progress.py']}, {'group': 'retention', 'verdict': 'fully_superseded', 'paths': ['tools/delegate_sessions.py', 'tools/delegate_sessions.py', 'tools/delegate_session_store.py', 'tools/delegate_session_store.py', 'tools/delegate_session_store.py']}]
Original-to-port evidence: {'original': '9309113caa93ae35b90729b7a0801c56d954a146', 'port': '0d83365f9300d43643c35cd51fdeb3090530f56d', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/9309113caa93.port-range-diff.txt', 'diff_chars': 63257}

### 50. `7127952ab243e12f0b8ead2f471603b1be1e774e` — refactor(delegation): move fork history policy into extensions
**Bucket 1: Already in dev3**. Fork capture is detached; extension formats once per child; validator rejects unmatched/unfinished calls and clears persistence bookkeeping. Resume is not a new fork.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'fork', 'verdict': 'already_present', 'paths': ['agent/subagent_context.py', 'agent/subagent_extension.py', 'agent/subagent_fork_validation.py', 'tools/delegate_tool_progress.py']}]
Original-to-port evidence: {'original': '7127952ab243e12f0b8ead2f471603b1be1e774e', 'port': 'a2d77dfba70d1ade674c686f372dec0a004ae001', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/7127952ab243.port-range-diff.txt', 'diff_chars': 1562}

### 51. `e66b2fa7c8fe0b9ac3775f8587e1a14d6d00f56c` — fix(delegation): keep fork guidance independent of history format
**Bucket 1: Already in dev3**. Fork guidance is format-neutral; it no longer asserts user/final-only history.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'fork', 'verdict': 'already_present', 'paths': ['agent/subagent_context.py', 'agent/subagent_extension.py', 'agent/subagent_fork_validation.py', 'tools/delegate_tool_progress.py']}]
Original-to-port evidence: {'original': 'e66b2fa7c8fe0b9ac3775f8587e1a14d6d00f56c', 'port': '9c3ad5c47e47b6e6d9686a37c092417739e33dce', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/e66b2fa7c8fe.port-range-diff.txt', 'diff_chars': 582}

### 52. `8cdd0003c200c5ac3e38fa6283cb95e65f7ef138` — fix(discord): bound typing cleanup and preserve task ownership
**Bucket 1: Already in dev3**. Typing cleanup is bounded with asyncio.wait(0.5), cancellation propagates, retirement set tracks late tasks and only the slot owner removes the active task.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'typing', 'verdict': 'already_present', 'paths': ['plugins/platforms/discord/adapter.py']}]
Original-to-port evidence: {'original': '8cdd0003c200c5ac3e38fa6283cb95e65f7ef138', 'port': '116d0f79e99d9f94a865ad2dbb649f8456f2752e', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/8cdd0003c200.port-range-diff.txt', 'diff_chars': 546}

### 53. `c2d2d3f768e383b263eeb5ed81c9a9abab998313` — fix(approval): bind cards to exact requests and frozen origins
**Bucket 1: Already in dev3**. Frozen per-actor request origin, exact request/card/message binding, coalescing attribution, outcome-aware settle and child actor/title all survive. Upstream common approval provenance moved to a sibling; contracts are retained.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'approval', 'verdict': 'already_present', 'paths': ['agent/approval_provenance.py', 'gateway/platforms/approval_cards.py', 'plugins/platforms/discord/adapter_exec_approval_view.py', 'gateway/run_turn_runner_approval_settle.py', 'tools/approval_gateway_wait.py', 'tools/approval_gateway_wait.py', 'tests/tools/test_approval_gateway_wait_coalesce_race.py']}]
Original-to-port evidence: {'original': 'c2d2d3f768e383b263eeb5ed81c9a9abab998313', 'port': '65d7745006a010d13564d1c237580c0188481f9e', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/c2d2d3f768e3.port-range-diff.txt', 'diff_chars': 59326}

### 54. `6fbbd55f01adc101da36e1e70d8c3de3eb8e81ab` — fix(approval): preserve observer parity and timeout-only fallback notices
**Bucket 1: Already in dev3**. Observer hooks omit card-only request/provenance data and new fallback notices occur only for unresolved timeout; edits retain frozen origin.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'approval', 'verdict': 'already_present', 'paths': ['agent/approval_provenance.py', 'gateway/platforms/approval_cards.py', 'plugins/platforms/discord/adapter_exec_approval_view.py', 'gateway/run_turn_runner_approval_settle.py', 'tools/approval_gateway_wait.py', 'tools/approval_gateway_wait.py', 'tests/tools/test_approval_gateway_wait_coalesce_race.py']}]
Original-to-port evidence: {'original': '6fbbd55f01adc101da36e1e70d8c3de3eb8e81ab', 'port': '36a72f7e0773d9b7a35c7cc2af7689ca27aa668c', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/6fbbd55f01ad.port-range-diff.txt', 'diff_chars': 3160}

### 55. `ce0cdb59c9578b58f7ecb64c8a286b3d6619c8c2` — test(discord): add dark_grey to shared color mock
**Bucket 1: Already in dev3**. Shared Discord Color test double includes dark_grey; patch-equivalent port.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'approval', 'verdict': 'already_present', 'paths': ['agent/approval_provenance.py', 'gateway/platforms/approval_cards.py', 'plugins/platforms/discord/adapter_exec_approval_view.py', 'gateway/run_turn_runner_approval_settle.py', 'tools/approval_gateway_wait.py', 'tools/approval_gateway_wait.py', 'tests/tools/test_approval_gateway_wait_coalesce_race.py']}]
Original-to-port evidence: {'original': 'ce0cdb59c9578b58f7ecb64c8a286b3d6619c8c2', 'port': 'ec5e389d1427a75b9f84e6e00e7844e5619ba6e2', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/ce0cdb59c957.port-range-diff.txt', 'diff_chars': 440}

### 56. `f010437faa6c30ed93e2f48ff701470908d884fb` — fix(gateway): refuse delegate-owned process notification wakes
**Bucket 3: Missing or regressed on pinned dev3**. No pre-adapter delegate-process ownership gate. Existing configurable sa-owner suppression is narrower; marker-backed child process notices can be deliberately admitted to the parent by current upstream _owned_by path. Current tests explicitly pin the opposite of dev2 safety policy.
Disposition: Ported in this parity patch; see feature/tests below.
Baseline evidence: [{'group': 'delegate_process', 'verdict': 'missing', 'paths': ['gateway/run_notifications.py', 'gateway/run_notifications.py', 'gateway/run_notifications_ownership.py', 'gateway/session_automation.py', 'tools/process_registry_notifications.py', 'tests/gateway/test_automation_wake_route.py']}]

### 57. `e62c5070a3bdb8027236b794e3ad76188ac7e6a5` — fix(gateway): preserve resumed delegate and process handoff delivery
**Bucket 3: Missing or regressed on pinned dev3**. Process handoff does not retarget parent_session_id and watcher snapshot still outranks live ProcessSession. Compression edge test still rejects any inherited branch/delegate marker. Explicit resumed-child ownership has a modern equivalent, but these independent hunks are absent.
Disposition: Ported in this parity patch; see feature/tests below.
Baseline evidence: [{'group': 'handoff', 'verdict': 'missing', 'paths': ['tools/process_registry.py', 'tools/process_registry.py', 'gateway/run_notifications.py']}, {'group': 'lineage', 'verdict': 'missing', 'paths': ['hermes_state_compression.py', 'tests/gateway/test_automation_wake_route.py']}, {'group': 'delegate_process', 'verdict': 'missing', 'paths': ['gateway/run_notifications.py', 'gateway/run_notifications.py', 'gateway/run_notifications_ownership.py', 'gateway/session_automation.py', 'tools/process_registry_notifications.py', 'tests/gateway/test_automation_wake_route.py']}]

### 58. `28e9bc05e727bfd72f9f0a1300eedc3ac4ba3da4` — fix(gateway): preserve notification and approval contracts on updated host
**Bucket 3: Missing or regressed on pinned dev3**. Approval coalescing-race origin fixtures are present (moved helper import). Delegate-only pre-adapter gate absent; selected-child ownership partly superseded by shared compression proof but inherited-marker chain remains a gap.
Disposition: Ported in this parity patch; see feature/tests below.
Baseline evidence: [{'group': 'delegate_process', 'verdict': 'missing', 'paths': ['gateway/run_notifications.py', 'gateway/run_notifications.py', 'gateway/run_notifications_ownership.py', 'gateway/session_automation.py', 'tools/process_registry_notifications.py', 'tests/gateway/test_automation_wake_route.py']}, {'group': 'lineage', 'verdict': 'missing', 'paths': ['hermes_state_compression.py', 'tests/gateway/test_automation_wake_route.py']}, {'group': 'approval', 'verdict': 'already_present', 'paths': ['agent/approval_provenance.py', 'gateway/platforms/approval_cards.py', 'plugins/platforms/discord/adapter_exec_approval_view.py', 'gateway/run_turn_runner_approval_settle.py', 'tools/approval_gateway_wait.py', 'tools/approval_gateway_wait.py', 'tests/tools/test_approval_gateway_wait_coalesce_race.py']}]

### 59. `9e7fc68a4b5dfaf3ebe247c6af4331b4397001b5` — fix(gateway): retain upstream markerless completion ownership checks
**Bucket 1: Already in dev3**. Markerless source=subagent stays terminal in _lookup_completion_owner. This retained upstream protection is independent of the missing marker-backed process gate.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'delegate_process_markerless', 'verdict': 'already_present', 'paths': ['gateway/run_notifications_ownership.py', 'tests/gateway/test_automation_wake_route.py']}]

### 60. `33cb07f5516c7127e457582cf55671ddcf67f1ef` — fix(gateway): keep detached webhook silence and drain accepted steers in process
**Bucket 3: Missing or regressed on pinned dev3**. Accepted-steer losslessness has unified ticketed-transfer replacement; detached webhook silence and legacy retry compatibility were missing and are now restored.
Disposition: Ported in this parity patch; see feature/tests below.
Baseline evidence: [{'group': 'steer_lossless', 'verdict': 'fully_superseded', 'paths': ['gateway/session_queued_controls.py', 'gateway/session_queued_controls.py', 'gateway/run_busy.py', 'gateway/run_turn.py']}]

### 61. `96744367e349e86746b8963ec78354d447123ebf` — fix(gateway): limit pre-adapter process verdicts to delegate-owned targets
**Bucket 3: Missing or regressed on pinned dev3**. Dependent follow-up to the missing delegate-process gate: restoration must be delegate-only. Current dev3 has no gate to narrow; ordinary authority refusal semantics are retained, but the final dev2 cumulative safety contract is not.
Disposition: Ported in this parity patch; see feature/tests below.
Baseline evidence: [{'group': 'delegate_process', 'verdict': 'missing', 'paths': ['gateway/run_notifications.py', 'gateway/run_notifications.py', 'gateway/run_notifications_ownership.py', 'gateway/session_automation.py', 'tools/process_registry_notifications.py', 'tests/gateway/test_automation_wake_route.py']}]

### 62. `1e3ff05148a6be125fc30fcde1d8713369351bd0` — test(approval): align settle and template tests with frozen-origin cards
**Bucket 1: Already in dev3**. Frozen-origin approval timeout/template fixtures are adapted to current card text and sibling imports.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'approval', 'verdict': 'already_present', 'paths': ['agent/approval_provenance.py', 'gateway/platforms/approval_cards.py', 'plugins/platforms/discord/adapter_exec_approval_view.py', 'gateway/run_turn_runner_approval_settle.py', 'tools/approval_gateway_wait.py', 'tools/approval_gateway_wait.py', 'tests/tools/test_approval_gateway_wait_coalesce_race.py']}]
Original-to-port evidence: {'original': '1e3ff05148a6be125fc30fcde1d8713369351bd0', 'port': '46c9f32134e0946570bfe2f8348181abd726cc42', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/1e3ff05148a6.port-range-diff.txt', 'diff_chars': 11411}

### 63. `0b490e898cce2a757b03bc049af988580a5727a3` — fix(gateway): read delegate-notice owners through raw or async session stores
**Bucket 3: Missing or regressed on pinned dev3**. Dependent follow-up to the missing delegate-only gate: restored owner lookup must accept both inline SessionDB and awaitable AsyncSessionDB results. Current dev3 has no pre-adapter helper implementing that contract.
Disposition: Ported in this parity patch; see feature/tests below.
Baseline evidence: [{'group': 'delegate_process', 'verdict': 'missing', 'paths': ['gateway/run_notifications.py', 'gateway/run_notifications.py', 'gateway/run_notifications_ownership.py', 'gateway/session_automation.py', 'tools/process_registry_notifications.py', 'tests/gateway/test_automation_wake_route.py']}]

### 64. `be747064b4678abf993e40281992d8e348a9c647` — feat(webhook): plugin-keyed resumable webhook sessions and per-route fallback_providers
**Bucket 3: Missing or regressed on pinned dev3**. Keyed-session identity already existed, but platform fallback defaults/frozen worker runtime were incomplete; restored. Explicit empty-list fallback semantics require D3.
Disposition: Runtime restored; D3 empty-chain compatibility unresolved.

### 65. `253b73b3d4b43846df0e1324ca3f0abf09bab71a` — feat(delegation,webhook): retained children keep their failover chain; completions name the worker session
**Bucket 3: Missing or regressed on pinned dev3**. Retained child fallback routes already survive in sessions.route; webhook worker identity is in typed event metadata. Frozen webhook chain/default restoration now ported, with D3 unresolved.
Disposition: Runtime restored; D3 empty-chain compatibility unresolved.
Baseline evidence: [{'group': 'stored_routes', 'verdict': 'fully_superseded', 'paths': ['agent/session_routes.py', 'agent/session_routes.py', 'agent/session_routes.py', 'agent/session_routes.py', 'tests/agent/test_session_routes.py']}]

### 66. `2378e8e4a7bcaf77d5ebdf9dd3b59c41889565ca` — feat(webhook): route-owned session keys; retain internal-turn workers as resumable delegations
**Bucket 3: Missing or regressed on pinned dev3**. Script/template/plugin route keys already preserved; internal-turn final handoff restored. Automatic retained-delegate listing is not full parity under attach=false: D2 remains open.
Disposition: Routing restored; D2 historical discoverability decision unresolved.

### 67. `8fb8286035469de0e7a54cc815796f23a2b2bc14` — fix(gateway): keep Discord-note api_content sidecars on timestamped history rebuild
**Bucket 1: Already in dev3**. Timestamped history rebuild uses existing api_content when present and never reconstructs an authoritative sidecar from display text.
Disposition: Verified at e4ec680ce518e2ff2305afdd5dd188001803fb2d.
Baseline evidence: [{'group': 'sidecar', 'verdict': 'already_present', 'paths': ['gateway/run.py']}]
Original-to-port evidence: {'original': '8fb8286035469de0e7a54cc815796f23a2b2bc14', 'port': '74e9952621c0695579ed33b716f42c511f40ff91', 'range_diff': '/home/sonya/ykhos-ws/artifacts/dev3-cutover-20261106/parity-evidence/audit/8fb828603546.port-range-diff.txt', 'diff_chars': 901}

### 68. `218299900abff8abdbd02861792d1f3255913a5b` — fix(delegation): completion headers name the child, not replay its brief
**Bucket 3: Missing or regressed on pinned dev3**. Active batch/failure formatters still echo full goals without child title/session identity; results omit title/resumed metadata and missed-steer notes echo full text. All three added regression tests are absent.
Disposition: Ported in this parity patch; see feature/tests below.
Baseline evidence: [{'group': 'headers', 'verdict': 'missing', 'paths': ['tools/process_registry_notifications.py', 'tools/process_registry_notifications.py', 'tools/delegate_tool_results.py', 'tools/delegate_tool_child_run.py', 'tools/delegate_tool_child_run.py', 'tools/delegate_sessions.py']}]

## Net-diff cross-check (5894e750c3..218299900a)

Non-test diff was inspected before tests: 169 files, 6575 insertions, 705 deletions. Tests: 96 files, 8550 insertions, 241 deletions. Exact stats are preserved alongside this ledger. Historical tests were audited for presence/replacements; full original test declarations and source-symbol evidence are in `audit/dev2-parity-audit.json`. Historical test names alone were not treated as proof of behavior.

## Scope limits

No live dev2 checkout, live state.db, live jobs/subscriptions, canonical personal plugin branch, or running service was intentionally modified. The unintended provisioning incident above remains separately disclosed. No deployment or live config/DB migration ran. The companion plugin/runtime and private effective configuration still require Phase 4 rehearsal. This audit enumerates all custom commits, but D1–D4 and the narrowed internal addressing mean **all additions preserved** is not yet an honest acceptance statement.
