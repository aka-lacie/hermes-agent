# Multi-target terminal and file backends implementation plan

> For Hermes: use subagent-driven-development to implement this plan task-by-task.

Goal: let the agent explicitly choose an execution target per tool call, so it can run locally on winter-castle by default and remotely on winter-palace over SSH when needed, with terminal and file tools staying aligned.

Architecture: keep the existing backend system and environment routing intact. Add a small per-call target override layer on top of `_get_env_config()` and `_active_environments`, then thread the same override through file tools so terminal/file operations hit the same named environment. Model-facing API should expose a simple `target` parameter, while config defines named targets such as `castle` and `palace`.

Tech stack: Python, existing Hermes terminal/file tool architecture, SSHEnvironment, config.yaml / env-backed terminal config, pytest.

---

## Design summary

Today Hermes has one terminal backend per session, chosen from `TERMINAL_ENV` / `terminal.backend`, and file tools share that same backend by reusing `_active_environments` from `tools/terminal_tool.py`.

The smallest safe change is:

1. Add a named target concept to config.
2. Add optional `target` params to terminal and file tools.
3. Resolve target -> backend config at call time.
4. Key cached environments by both task_id and target so multiple environments can coexist.
5. Default behavior remains unchanged when `target` is omitted.

Example desired config:

```yaml
terminal:
  backend: local
  cwd: ~/agent-yuri
  timeout: 180
  targets:
    castle:
      backend: local
      cwd: ~/agent-yuri
    palace:
      backend: ssh
      ssh_host: winter-palace
      ssh_user: sonya
      ssh_port: 22
      ssh_key: ~/.ssh/id_ed25519
      cwd: ~/agent-yuri
```

Example desired tool calls:

```python
terminal(command="git status", target="castle")
terminal(command="nvidia-smi", target="palace")
read_file(path="~/agent-yuri/AGENTS.md", target="palace")
write_file(path="~/agent-yuri/scratch/test.txt", content="hi", target="castle")
```

Non-goals:
- no automatic fallback logic in v1
- no load balancing
- no multi-hop rsync/sync orchestration inside Hermes
- no implicit target switching based on path

For now the agent decides explicitly whether to use `castle` or `palace`.

---

## Task 1: Add a target-aware config resolver for terminal backends

Objective: resolve an optional target name into the same config shape `_get_env_config()` already returns.

Files:
- Modify: `~/hermes-agent/tools/terminal_tool.py`
- Test: `~/hermes-agent/tests/tools/test_terminal_tool_targets.py`

Step 1: write failing tests for target resolution.

Create `tests/tools/test_terminal_tool_targets.py` with tests for:
- default behavior when no target is passed
- resolving `target="palace"` from config/env
- unknown target returns a clear error
- target overrides backend, cwd, and ssh fields only for that call

Suggested test skeleton:

```python
import os
from tools import terminal_tool as tt


def test_resolve_default_env_without_target(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    cfg = tt._get_env_config()
    resolved = tt._resolve_target_config(cfg, None)
    assert resolved["env_type"] == "local"


def test_resolve_named_target_overrides_backend(monkeypatch):
    cfg = {
        "env_type": "local",
        "cwd": "/tmp/local",
        "targets": {
            "palace": {
                "backend": "ssh",
                "ssh_host": "winter-palace",
                "ssh_user": "sonya",
                "cwd": "~/agent-yuri",
            }
        },
    }
    resolved = tt._resolve_target_config(cfg, "palace")
    assert resolved["env_type"] == "ssh"
    assert resolved["ssh_host"] == "winter-palace"
    assert resolved["cwd"] == "~/agent-yuri"


def test_unknown_target_raises_clear_error():
    cfg = {"env_type": "local", "targets": {}}
    try:
        tt._resolve_target_config(cfg, "missing")
    except ValueError as e:
        assert "Unknown terminal target" in str(e)
    else:
        raise AssertionError("Expected ValueError")
```

Step 2: run the new tests and verify failure.

Run:
`source ~/hermes-agent/venv/bin/activate && pytest ~/hermes-agent/tests/tools/test_terminal_tool_targets.py -q`

Expected: fail because `_resolve_target_config` does not exist.

Step 3: implement `_resolve_target_config` in `tools/terminal_tool.py`.

Add a helper near `_get_env_config()`:

```python
def _resolve_target_config(config: Dict[str, Any], target: str | None) -> Dict[str, Any]:
    if not target:
        return dict(config)

    targets = config.get("targets") or {}
    if target not in targets:
        raise ValueError(f"Unknown terminal target: {target}")

    resolved = dict(config)
    target_cfg = dict(targets[target])

    backend = target_cfg.pop("backend", None)
    if backend:
        resolved["env_type"] = backend

    field_map = {
        "cwd": "cwd",
        "timeout": "timeout",
        "ssh_host": "ssh_host",
        "ssh_user": "ssh_user",
        "ssh_port": "ssh_port",
        "ssh_key": "ssh_key",
        "ssh_persistent": "ssh_persistent",
        "docker_image": "docker_image",
        "singularity_image": "singularity_image",
        "modal_image": "modal_image",
        "daytona_image": "daytona_image",
    }
    for src, dst in field_map.items():
        if src in target_cfg:
            resolved[dst] = target_cfg[src]

    resolved["selected_target"] = target
    return resolved
```

Step 4: extend `_get_env_config()` to include `targets` loaded from config/env bridge if already present in the runtime config plumbing.

If terminal config is already injected into env vars only, add an env-backed JSON escape hatch first:

```python
"targets": _parse_env_var("TERMINAL_TARGETS", "{}", json.loads, "valid JSON"),
```

If the runtime already injects `terminal.targets` into process config, wire that through instead of inventing a new env var.

Step 5: rerun tests.

Run:
`source ~/hermes-agent/venv/bin/activate && pytest ~/hermes-agent/tests/tools/test_terminal_tool_targets.py -q`

Expected: pass.

Step 6: commit.

```bash
git -C ~/hermes-agent add tools/terminal_tool.py tests/tools/test_terminal_tool_targets.py
git -C ~/hermes-agent commit -m "feat: add named terminal target resolution"
```

---

## Task 2: Add `target` parameter to the terminal tool

Objective: expose per-call target selection to the model and route environment creation through the resolved target config.

Files:
- Modify: `~/hermes-agent/tools/terminal_tool.py`
- Test: `~/hermes-agent/tests/tools/test_terminal_tool_targets.py`

Step 1: write failing tests for terminal calls using `target`.

Add tests for:
- `terminal_tool(..., target="palace")` chooses SSH
- environment cache keys are distinct for `default:castle` and `default:palace`
- no `target` preserves existing behavior

Suggested assertions:

```python
def test_terminal_tool_uses_target_specific_env(monkeypatch):
    # monkeypatch _get_env_config and _create_environment
    # assert env_type passed to _create_environment is ssh for palace
    ...


def test_target_isolation_changes_environment_cache_key(monkeypatch):
    ...
```

Step 2: run tests and verify failure.

Step 3: update the function signature:

```python
def terminal_tool(
    command: str,
    background: bool = False,
    timeout: Optional[int] = None,
    task_id: Optional[str] = None,
    force: bool = False,
    workdir: Optional[str] = None,
    check_interval: Optional[int] = None,
    pty: bool = False,
    target: Optional[str] = None,
) -> str:
```

Step 4: resolve config and derive a target-aware environment key.

Replace the top of the function with:

```python
config = _get_env_config()
resolved = _resolve_target_config(config, target)
env_type = resolved["env_type"]
base_task_id = task_id or "default"
effective_task_id = f"{base_task_id}:{target}" if target else base_task_id
```

Then use `resolved` instead of `config` for image/cwd/timeout/ssh settings.

Step 5: update tool schema/description so models see `target`.

Add to the terminal tool JSON schema:

```python
"target": {
    "type": "string",
    "description": "Optional named execution target from config, e.g. 'castle' or 'palace'. When omitted, uses the default terminal backend."
}
```

Also update `TERMINAL_TOOL_DESCRIPTION` with one short sentence:

`Use 'target' when multiple execution environments are configured.`

Step 6: rerun tests.

Step 7: commit.

```bash
git -C ~/hermes-agent add tools/terminal_tool.py tests/tools/test_terminal_tool_targets.py
git -C ~/hermes-agent commit -m "feat: add per-call terminal target selection"
```

---

## Task 3: Thread `target` through file tools

Objective: ensure `read_file`, `write_file`, `search_files`, and patch-adjacent file operations hit the same target environment as terminal calls.

Files:
- Modify: `~/hermes-agent/tools/file_tools.py`
- Modify: `~/hermes-agent/tools/file_operations.py` (if needed)
- Test: `~/hermes-agent/tests/tools/test_file_tools.py`
- Create: `~/hermes-agent/tests/tools/test_file_tool_targets.py`

Step 1: write failing tests.

Test cases:
- `_get_file_ops(target="palace")` uses the same target-aware environment key as terminal
- `read_file(..., target="palace")` resolves against the SSH environment
- `write_file(..., target="castle")` resolves against local

Step 2: update `_get_file_ops()` signature.

```python
def _get_file_ops(task_id: str = "default", target: str | None = None) -> ShellFileOperations:
```

Step 3: inside `_get_file_ops()`, mirror terminal behavior:
- call `_get_env_config()`
- call `_resolve_target_config(config, target)`
- derive `effective_task_id = f"{task_id}:{target}" if target else task_id`
- use resolved config for environment creation

Step 4: add `target` parameter to model-facing file tools:
- `read_file`
- `write_file`
- `search_files`
- any other file op entrypoints that call `_get_file_ops()`

Example:

```python
def read_file(path: str, offset: int = 1, limit: int = 500, target: str | None = None):
    file_ops = _get_file_ops(target=target)
    ...
```

Step 5: update tool schemas/descriptions to mention target.

Step 6: rerun file tool tests.

Run:
`source ~/hermes-agent/venv/bin/activate && pytest ~/hermes-agent/tests/tools/test_file_tool_targets.py ~/hermes-agent/tests/tools/test_file_tools.py -q`

Step 7: commit.

```bash
git -C ~/hermes-agent add tools/file_tools.py tests/tools/test_file_tool_targets.py tests/tools/test_file_tools.py
git -C ~/hermes-agent commit -m "feat: add per-call target selection for file tools"
```

---

## Task 4: Add config support for named terminal targets

Objective: make named targets configurable through Hermes config rather than requiring raw env var hacks.

Files:
- Modify: `~/hermes-agent/hermes_cli/config.py`
- Modify: `~/hermes-agent/cli.py` or config-loading bridge if needed
- Test: `~/hermes-agent/tests/hermes_cli/test_config.py`
- Test: `~/hermes-agent/tests/hermes_cli/test_set_config_value.py`

Step 1: inspect how `terminal.backend`, `terminal.cwd`, and related settings are currently bridged into env vars / runtime config.

Step 2: write failing tests for `terminal.targets` round-trip.

Desired config shape:

```yaml
terminal:
  backend: local
  targets:
    castle:
      backend: local
      cwd: ~/agent-yuri
    palace:
      backend: ssh
      ssh_host: winter-palace
      ssh_user: sonya
      cwd: ~/agent-yuri
```

Step 3: implement parsing and display support for `terminal.targets`.

At minimum:
- preserve nested dict structure in config YAML
- expose to runtime so `tools/terminal_tool.py` can read it
- show targets in `hermes config` / `hermes status` output

Step 4: if config->env projection is required, serialize to `TERMINAL_TARGETS` JSON for the tool layer.

Step 5: rerun config tests.

Step 6: commit.

```bash
git -C ~/hermes-agent add hermes_cli/config.py cli.py tests/hermes_cli/test_config.py tests/hermes_cli/test_set_config_value.py
git -C ~/hermes-agent commit -m "feat: add named terminal targets to config"
```

---

## Task 5: Add safety and UX guardrails

Objective: make the multi-target feature understandable to the model and safe for users.

Files:
- Modify: `~/hermes-agent/tools/terminal_tool.py`
- Modify: `~/hermes-agent/tools/file_tools.py`
- Modify: `~/hermes-agent/website/docs/user-guide/features/tools.md` or relevant docs
- Test: `~/hermes-agent/tests/tools/test_terminal_tool_requirements.py`

Step 1: add clear user-facing errors.

Cases:
- unknown target
- target configured with backend=ssh but missing ssh_host or ssh_user
- target configured but backend unsupported

Step 2: update terminal/file tool descriptions with one practical example each.

Step 3: update docs with the winter-castle / winter-palace use case.

Example docs snippet:

```yaml
terminal:
  backend: local
  targets:
    castle:
      backend: local
      cwd: ~/agent-yuri
    palace:
      backend: ssh
      ssh_host: winter-palace
      ssh_user: sonya
      cwd: ~/agent-yuri
```

Then:
- `terminal(command="git status", target="castle")`
- `terminal(command="nvidia-smi", target="palace")`
- `read_file(path="~/agent-yuri/AGENTS.md", target="palace")`

Step 4: rerun tests and docs checks if any.

Step 5: commit.

```bash
git -C ~/hermes-agent add tools/terminal_tool.py tools/file_tools.py website/docs/user-guide/features/tools.md tests/tools/test_terminal_tool_requirements.py
git -C ~/hermes-agent commit -m "docs: document named execution targets"
```

---

## Task 6: Verify the exact winter-castle / winter-palace workflow

Objective: prove the feature solves the real use case.

Files:
- Modify: local test config only
- Test: manual verification notes in this plan or a follow-up doc

Step 1: create a test config on the spare checkout with both targets defined.

Step 2: run these commands manually from winter-castle-hosted Hermes:

```bash
# local castle exec
hermes -p yuri
# inside agent session or test harness:
terminal(command="pwd", target="castle")
terminal(command="hostname", target="castle")

# remote palace exec
terminal(command="pwd", target="palace")
terminal(command="hostname", target="palace")
```

Expected:
- `castle` returns the local host and workspace path
- `palace` returns the SSH host and synced workspace path

Step 3: verify file tools match terminal target:

```python
read_file(path="~/agent-yuri/AGENTS.md", target="castle")
read_file(path="~/agent-yuri/AGENTS.md", target="palace")
```

Step 4: test palace-off failure mode.

Expected:
- `target="palace"` returns a clear SSH connection error
- `target="castle"` still works
- no cross-contamination of cached environments

Step 5: commit any config/docs changes that came from verification.

---

## Task 7: Point the spare checkout at Sonya's fork and dev branch

Objective: make the implementation land in the correct remote/branch before coding starts.

Files:
- No code changes required

Step 1: add Sonya's fork as a remote.

```bash
git -C ~/hermes-agent remote rename origin upstream
git -C ~/hermes-agent remote add origin <SONYA_FORK_URL>
```

Step 2: fetch all remotes.

```bash
git -C ~/hermes-agent fetch --all --prune
```

Step 3: create or switch to local dev branch tracking Sonya's fork.

```bash
git -C ~/hermes-agent checkout -B dev origin/dev
```

If `origin/dev` does not exist yet:

```bash
git -C ~/hermes-agent checkout -b dev
```

Step 4: verify remotes and branch.

```bash
git -C ~/hermes-agent remote -v
git -C ~/hermes-agent branch -vv
```

Expected:
- `upstream` = NousResearch/hermes-agent
- `origin` = Sonya's fork
- current branch = `dev`

---

## Acceptance criteria

The feature is done when all of these are true:

- `terminal(..., target="castle")` and `terminal(..., target="palace")` both work
- `read_file` / `write_file` / `search_files` accept the same `target`
- target omission preserves current behavior exactly
- local and SSH environments can coexist in cache without collisions
- unknown targets fail clearly
- config supports named targets
- docs include the castle/palace workflow
- the spare checkout tracks Sonya's fork `dev` branch

---

## Notes for implementation

- Keep v1 explicit. Do not implement automatic fallback from `palace` to `castle` yet. That belongs in a later feature once explicit targeting is stable.
- Reuse existing routing. Do not refactor the whole terminal subsystem.
- The highest-risk bug is cache collision between target environments. Make target-aware environment keys non-optional when target is present.
- Mirror terminal and file tool behavior exactly. If terminal can target palace but file tools cannot, the UX is broken.
