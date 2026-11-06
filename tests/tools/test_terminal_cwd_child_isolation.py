"""Delegated cwd ownership is separate from inherited approval/container identity.

Exercise registered terminal dispatch, real cwd persistence and the production
shell wrapper; backend acquisition and approval UI are bounded test doubles.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent.delegation_context import delegated_child_context
from tools import terminal_tool as tt
from tools.approval import get_current_session_key
from tools.approval_context import reset_current_session_key, set_current_session_key
from tools.environments.base_session_env import _split_cwd_marker, _wrap_command_script
from tools.registry import registry


@contextmanager
def approval_owner(key):
    token = set_current_session_key(key)
    try:
        yield
    finally:
        reset_current_session_key(token)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    for name in (
        "_session_cwd", "_task_env_overrides", "_container_aliases",
        "_active_environments", "_last_activity", "_calls_in_flight",
    ):
        monkeypatch.setattr(tt, name, {})
    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
    monkeypatch.setattr(tt, "_current_session_key", lambda: "")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)


class ShellEnvironment:
    """Run the actual wrapper over bash stdin, without login/init side effects."""
    env = {}

    def __init__(self, root):
        self.root = root
        self.cwd = str(root)
        self.calls = []

    def execute(self, command, *, cwd, timeout, **kwargs):
        self.calls.append((command, cwd))
        marker = "__CHILD_CWD_TEST__"
        script = _wrap_command_script(
            command, quoted_cwd=shlex.quote(cwd), quoted_snap="unused",
            snap_tmp_template="unused", passthrough_names=(), snapshot_ready=False,
            cwd_marker=marker, persist_env=False,
        )
        bash = shutil.which("bash")
        assert bash is not None, "shell-wrapper regressions require bash"
        proc = subprocess.run(
            [bash, "--noprofile", "--norc"], input=script,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=self.root, timeout=timeout, check=False,
        )
        parsed = _split_cwd_marker(proc.stdout, marker)
        result = {"output": proc.stdout, "returncode": proc.returncode}
        if parsed:
            self.cwd, result["output"] = parsed
            result.update(cwd=self.cwd, cwd_observed=True)
        return result


@pytest.fixture
def terminal_pipeline(monkeypatch, tmp_path):
    backend = ShellEnvironment(tmp_path)
    approval_keys = []
    monkeypatch.setattr(tt, "_acquire_env", lambda *_a, **_kw: backend)
    monkeypatch.setattr(tt, "_pre_exec_block", lambda *_a, **_kw: None)
    monkeypatch.setattr(tt, "_yield_kwargs", lambda *_a, **_kw: {})
    monkeypatch.setattr(tt, "_get_env_config", lambda: {
        "env_type": "local", "cwd": str(tmp_path), "timeout": 10,
        "lifetime_seconds": 3600,
    })

    def approve(*_args, **_kwargs):
        approval_keys.append(get_current_session_key())
        return {"approved": True}

    monkeypatch.setattr(tt, "_check_all_guards", approve)

    def run(task_id, command="pwd", **kwargs):
        entry = registry.get_entry("terminal")
        assert entry is not None
        return json.loads(entry.handler(
            {"command": command, **kwargs}, task_id=task_id,
        ))

    return SimpleNamespace(run=run, env=backend, approval_keys=approval_keys)


@pytest.mark.platforms("posix")
class TestDelegatedTerminalCwd:
    def test_siblings_and_nested_children_keep_their_own_cd_state(self, terminal_pipeline, tmp_path):
        p = terminal_pipeline
        worktree = tmp_path / "worktree"
        nested_dir = tmp_path / "nested"
        worktree.mkdir()
        nested_dir.mkdir()
        owner = "agent:main:discord:dm:cwd-test"
        for key in (owner, "child-a", "child-b", "grandchild"):
            tt.record_session_cwd(key, str(tmp_path))
        tt.register_container_alias("child-a", "parent-task")
        tt.register_container_alias("child-b", "parent-task")
        tt.register_container_alias("grandchild", "child-a")

        with approval_owner(owner):
            with delegated_child_context("child-a"):
                assert p.run("child-a", f"cd {shlex.quote(str(worktree))}")["exit_code"] == 0
                with delegated_child_context("grandchild"):
                    assert p.run("grandchild", f"cd {shlex.quote(str(nested_dir))}")["exit_code"] == 0
                assert p.run("child-a")["output"] == str(worktree)
            with delegated_child_context("child-b"):
                assert p.run("child-b")["output"] == str(tmp_path)
            assert p.run("parent-task")["output"] == str(tmp_path)
            assert get_current_session_key() == owner

        assert tt.get_session_cwd("child-a") == str(worktree)
        assert tt.get_session_cwd("grandchild") == str(nested_dir)
        assert tt.get_session_cwd("child-b") == str(tmp_path)
        assert tt.get_session_cwd(owner) == str(tmp_path)
        assert p.approval_keys and set(p.approval_keys) == {owner}
        assert tt._resolve_container_alias("grandchild") == "parent-task"

    def test_deleted_child_cwd_does_not_poison_later_sibling(self, terminal_pipeline, tmp_path):
        p = terminal_pipeline
        deleted = tmp_path / "deleted-worktree"
        deleted.mkdir()
        owner = "parent-owner"
        tt.record_session_cwd(owner, str(tmp_path))
        tt.record_session_cwd("child-a", str(tmp_path))
        with approval_owner(owner):
            with delegated_child_context("child-a"):
                assert p.run("child-a", f"cd {shlex.quote(str(deleted))}")["exit_code"] == 0
            deleted.rmdir()
            tt.record_session_cwd("child-b", str(tmp_path))
            with delegated_child_context("child-b"):
                for _ in range(2):
                    assert p.run("child-b")["output"] == str(tmp_path)
        assert tt.get_session_cwd(owner) == str(tmp_path)
        assert tt.get_session_cwd("child-a") == str(deleted)

    def test_same_child_recovers_deleted_record_before_wrapper_and_keeps_repair(
        self, terminal_pipeline, tmp_path, caplog,
    ):
        p = terminal_pipeline
        workspace = tmp_path / "declared-workspace"
        deleted = tmp_path / "old-worktree"
        workspace.mkdir()
        deleted.mkdir()
        tt.register_task_env_overrides("child", {"cwd": str(workspace)})
        tt.record_session_cwd("child", str(deleted))
        deleted.rmdir()
        with approval_owner("parent"), delegated_child_context("child"):
            for _ in range(2):
                result = p.run("child")
                assert result["exit_code"] == 0
                assert result["output"] == str(workspace)
        assert tt.get_session_cwd("child") == str(workspace)
        assert all(cwd == str(workspace) for _, cwd in p.env.calls)
        assert "recovering to" in caplog.text

    def test_deleted_record_and_default_recover_to_valid_ancestor(self, terminal_pipeline, tmp_path):
        p = terminal_pipeline
        deleted = tmp_path / "deleted"
        deleted.mkdir()
        tt.record_session_cwd("child", str(deleted))
        deleted.rmdir()
        with delegated_child_context("child"):
            result = p.run("child")  # planner default is itself the stale record
        assert result["exit_code"] == 0
        assert result["output"] == str(tmp_path)
        assert tt.get_session_cwd("child") == str(tmp_path)

    def test_explicit_workdir_neither_persists_nor_repairs_record(self, terminal_pipeline, tmp_path):
        p = terminal_pipeline
        original = tmp_path / "original"
        one_off = tmp_path / "one-off"
        original.mkdir()
        one_off.mkdir()
        tt.record_session_cwd("child", str(original))
        with delegated_child_context("child"):
            assert p.run("child", workdir=str(one_off))["output"] == str(one_off)
            assert tt.get_session_cwd("child") == str(original)
            original.rmdir()
            assert p.run("child", workdir=str(one_off))["output"] == str(one_off)
            assert tt.get_session_cwd("child") == str(original)
            missing = str(tmp_path / "explicit-missing")
            result = p.run("child", "printf 'MUST-NOT-RUN'", workdir=missing)
        assert result["exit_code"] == 126
        assert "MUST-NOT-RUN" not in result["output"]
        assert tt.get_session_cwd("child") == str(original)
        assert p.env.calls[-1][1] == missing

    def test_user_exit_126_is_not_retried(self, terminal_pipeline, tmp_path):
        p = terminal_pipeline
        tt.record_session_cwd("child", str(tmp_path))
        with delegated_child_context("child"):
            result = p.run("child", "exit 126")
        assert result["exit_code"] == 126
        assert len(p.env.calls) == 1


def test_top_level_prefers_routing_key_and_child_without_task_retains_fallback():
    with approval_owner("routing-owner"):
        assert tt._terminal_cwd_session_key("session-task") == "routing-owner"
        with delegated_child_context("child-session"):
            assert tt._terminal_cwd_session_key("child-task") == "child-task"
            assert tt._terminal_cwd_session_key(None) == "routing-owner"
            assert get_current_session_key() == "routing-owner"
    with approval_owner(""):
        assert tt._terminal_cwd_session_key("session-task") == "session-task"


@pytest.mark.parametrize("env_type", ["ssh", "docker", "modal", "daytona", "vercel_sandbox"])
def test_remote_recorded_paths_are_not_host_stat_checked(monkeypatch, env_type):
    recorded = "/workspace/remote-only"
    tt.record_session_cwd("child", recorded)

    actual_isdir = tt.os.path.isdir

    def checked_isdir(path):
        if tt.os.fspath(path) == recorded:
            raise AssertionError("remote cwd was checked on the host")
        return actual_isdir(path)

    monkeypatch.setattr(tt.os.path, "isdir", checked_isdir)
    assert tt._resolve_command_cwd(
        workdir=None, default_cwd="/workspace", session_key="child", env_type=env_type,
    ) == recorded
    assert tt.get_session_cwd("child") == recorded


def test_container_aliases_share_backend_not_cwd(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")
    tt.register_container_alias("child-a", "parent")
    tt.register_container_alias("grandchild", "child-a")
    for key, cwd in (("parent", "/workspace"), ("child-a", "/workspace/a"),
                     ("grandchild", "/workspace/nested")):
        tt.record_session_cwd(key, cwd)
    assert tt._resolve_container_task_id("child-a") == tt._resolve_container_task_id("parent")
    assert tt._resolve_container_task_id("grandchild") == tt._resolve_container_task_id("parent")
    with approval_owner("parent"), delegated_child_context("grandchild"):
        assert tt._resolve_command_cwd(
            workdir=None, default_cwd="/workspace", env_type="docker",
            session_key=tt._terminal_cwd_session_key("grandchild"),
        ) == "/workspace/nested"


@pytest.mark.parametrize("deleted", [False, True])
def test_background_child_uses_child_cwd_without_rebinding_approval(
    terminal_pipeline, monkeypatch, tmp_path, deleted,
):
    import tools.process_registry as process_module

    calls = []

    class ProcessRegistry:
        pending_watchers = []

        def spawn_local(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(id="proc_child_cwd", pid=123)

    monkeypatch.setattr(process_module, "process_registry", ProcessRegistry())
    tt.record_session_cwd("parent", str(tmp_path / "parent"))
    tt.record_session_cwd("child", str(tmp_path))
    if deleted:
        vanished = tmp_path / "vanished"
        vanished.mkdir()
        tt.record_session_cwd("child", str(vanished))
        vanished.rmdir()
    with approval_owner("parent"), delegated_child_context("child"):
        result = terminal_pipeline.run("child", "pwd", background=True)
        assert get_current_session_key() == "parent"
    assert result["session_id"] == "proc_child_cwd"
    assert len(calls) == 1
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["owner_task_id"] == "child"
    assert calls[0]["session_key"] == "child"
    assert terminal_pipeline.approval_keys == ["parent"]
