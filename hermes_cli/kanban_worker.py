"""Persistent headless kanban worker runtime.

This is deliberately separate from the gateway. A worker daemon is scoped to a
single Hermes profile, keeps the process/tool runtime warm, and claims tasks
for that profile from the shared kanban DB. Each claimed task still gets a
fresh agent/session context.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import kanban_db as kb


TaskRunner = Callable[[kb.Task, str, Optional[str]], None]


class _WakeSocket:
    """Small loopback UDP listener used to wake a worker from another process."""

    def __init__(self, wake_event: threading.Event):
        self._wake_event = wake_event
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self.host, self.port = self._sock.getsockname()
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(
            target=self._serve,
            name="kanban-worker-wake",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            self._wake_event.set()


def _task_env(task: kb.Task, workspace: str, *, profile: str, board: Optional[str]) -> dict[str, str]:
    """Build the same task-scoped env shape used by subprocess workers."""
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_arg = normalize_profile_name(profile)
    env = dict(os.environ)
    try:
        env["HERMES_HOME"] = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        pass
    env["HERMES_PROFILE"] = profile_arg
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    env["HERMES_KANBAN_DB"] = str(kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(kb.workspaces_root(board=board))
    env["HERMES_KANBAN_BOARD"] = board or kb.get_current_board()
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    terminal_timeout = kb._worker_terminal_timeout_env(  # noqa: SLF001
        task.max_runtime_seconds,
        env.get("TERMINAL_TIMEOUT"),
    )
    if terminal_timeout is not None:
        env["TERMINAL_TIMEOUT"] = terminal_timeout
    foreground_timeout = kb._worker_terminal_timeout_env(  # noqa: SLF001
        task.max_runtime_seconds,
        env.get("TERMINAL_MAX_FOREGROUND_TIMEOUT"),
    )
    if foreground_timeout is not None:
        env["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = foreground_timeout
    return env


@contextlib.contextmanager
def _scoped_task_process(env: dict[str, str], workspace: str):
    old_env: dict[str, Optional[str]] = {}
    for key, value in env.items():
        old_env[key] = os.environ.get(key)
        os.environ[key] = value
    token = set_hermes_home_override(env.get("HERMES_HOME"))
    old_cwd = os.getcwd()
    try:
        if workspace and os.path.isdir(workspace):
            os.chdir(workspace)
        yield
    finally:
        try:
            os.chdir(old_cwd)
        except OSError:
            pass
        reset_hermes_home_override(token)
        for key, old in old_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _current_task_status(task_id: str, *, board: Optional[str]) -> Optional[str]:
    with kb.connect_closing(board=board) as conn:
        task = kb.get_task(conn, task_id)
        return task.status if task else None


def _record_open_task_after_run(
    task: kb.Task,
    result: Any,
    *,
    board: Optional[str],
    protocol_violation: bool = False,
) -> None:
    status = _current_task_status(task.id, board=board)
    if status not in {"running", "ready"}:
        return

    error = "worker returned without calling kanban_complete or kanban_block"
    failure_limit = 1 if protocol_violation else None
    if isinstance(result, dict):
        if result.get("failed") and result.get("failure_reason") in {"rate_limit", "billing"}:
            with kb.connect_closing(board=board) as conn:
                kb.record_worker_rate_limited(
                    conn,
                    task.id,
                    str(result.get("error") or result.get("failure_reason") or "rate-limited"),
                    metadata={"source": "persistent_worker"},
                )
            return
        if result.get("failed"):
            error = str(result.get("error") or "worker failed")
            protocol_violation = False
            failure_limit = None
    with kb.connect_closing(board=board) as conn:
        kb.record_worker_runtime_failure(
            conn,
            task.id,
            error,
            outcome="crashed",
            failure_limit=failure_limit,
            event_payload_extra={
                "source": "persistent_worker",
                "protocol_violation": bool(protocol_violation),
            },
        )


class PersistentTaskRuntime:
    """Daemon-local runtime cache for the built-in in-process task runner."""

    def __init__(self) -> None:
        self.cli: Any = None
        self._lock = threading.Lock()

    def close(self) -> None:
        cli = self.cli
        self.cli = None
        agent = getattr(cli, "agent", None) if cli is not None else None
        if agent is not None and hasattr(agent, "close"):
            with contextlib.suppress(Exception):
                agent.close()

    def _get_cli(self) -> Any:
        if self.cli is None:
            from cli import HermesCLI

            self.cli = HermesCLI()
        return self.cli

    def run(self, task: kb.Task, workspace: str, board: Optional[str] = None) -> None:
        with self._lock:
            _run_task_with_cli(task, workspace, board=board, cli_provider=self._get_cli)


def run_task_in_process(task: kb.Task, workspace: str, board: Optional[str] = None) -> None:
    """Run one already-claimed kanban task in the current process."""
    PersistentTaskRuntime().run(task, workspace, board=board)


def _run_task_with_cli(
    task: kb.Task,
    workspace: str,
    *,
    board: Optional[str],
    cli_provider: Callable[[], Any],
) -> None:
    profile = task.assignee or os.environ.get("HERMES_PROFILE") or ""
    env = _task_env(task, workspace, profile=profile, board=board)
    prompt = f"work kanban task {task.id}"

    log_dir = kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = kb.worker_log_rotation_config()
    kb._rotate_worker_log(log_path, rotate_bytes, backup_count)  # noqa: SLF001

    with open(log_path, "ab", buffering=0) as raw_log:
        text_log = os.fdopen(os.dup(raw_log.fileno()), "a", encoding="utf-8", buffering=1)
        try:
            with contextlib.redirect_stdout(text_log), contextlib.redirect_stderr(text_log):
                print(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"persistent worker handling {task.id} as {profile}",
                    flush=True,
                )
                result: Any = None
                with _scoped_task_process(env, workspace):
                    cli = cli_provider()
                    cli.tool_progress_mode = "off"
                    if getattr(cli, "agent", None) is not None and hasattr(cli, "new_session"):
                        cli.new_session(silent=True)
                    else:
                        cli.conversation_history = []
                    if not cli._ensure_runtime_credentials():  # noqa: SLF001
                        result = {
                            "failed": True,
                            "error": "runtime credentials unavailable",
                            "failure_reason": "credentials",
                        }
                    else:
                        turn_route = cli._resolve_turn_agent_config(prompt)  # noqa: SLF001
                        if not cli._init_agent(  # noqa: SLF001
                            model_override=turn_route["model"],
                            runtime_override=turn_route["runtime"],
                            request_overrides=turn_route.get("request_overrides"),
                        ):
                            result = {
                                "failed": True,
                                "error": "agent initialization failed",
                                "failure_reason": "agent_init",
                            }
                        else:
                            cli.agent.quiet_mode = True
                            cli.agent.suppress_status_output = True
                            cli.agent.stream_delta_callback = None
                            cli.agent.tool_gen_callback = None
                            result = cli.agent.run_conversation(
                                user_message=prompt,
                                conversation_history=[],
                            )
                            if os.environ.get("HERMES_KANBAN_GOAL_MODE") == "1":
                                _run_goal_loop(cli, task, result, board=board)
                _record_open_task_after_run(
                    task,
                    result,
                    board=board,
                    protocol_violation=not (isinstance(result, dict) and result.get("failed")),
                )
        except BaseException as exc:
            traceback.print_exc(file=text_log)
            with kb.connect_closing(board=board) as conn:
                kb.record_worker_runtime_failure(
                    conn,
                    task.id,
                    f"persistent worker exception: {type(exc).__name__}: {exc}",
                    outcome="crashed",
                    event_payload_extra={"source": "persistent_worker"},
                )
        finally:
            text_log.close()


def _run_goal_loop(cli: Any, task: kb.Task, result: Any, *, board: Optional[str]) -> None:
    from hermes_cli.goals import DEFAULT_MAX_TURNS, run_kanban_goal_loop
    from tools.kanban_tools import _handle_block  # noqa: SLF001

    first_response = ""
    if isinstance(result, dict):
        first_response = str(result.get("final_response") or "")
    goal_text = task.title
    if task.body:
        goal_text += "\n\n" + task.body
    try:
        max_turns = int(os.environ.get("HERMES_KANBAN_GOAL_MAX_TURNS", "") or DEFAULT_MAX_TURNS)
    except (TypeError, ValueError):
        max_turns = DEFAULT_MAX_TURNS

    def _run_turn(prompt: str) -> str:
        out = cli.agent.run_conversation(
            user_message=prompt,
            conversation_history=[],
        )
        if isinstance(out, dict):
            return str(out.get("final_response") or "")
        return str(out or "")

    def _status() -> Optional[str]:
        return _current_task_status(task.id, board=board)

    def _block(reason: str) -> None:
        _handle_block({"task_id": task.id, "reason": reason})

    run_kanban_goal_loop(
        task_id=task.id,
        goal_text=goal_text,
        run_turn=_run_turn,
        task_status_fn=_status,
        block_fn=_block,
        max_turns=max_turns,
        first_response=first_response,
        log=lambda msg: print(msg, flush=True),
    )


def run_worker_daemon(
    *,
    profile: str,
    board: Optional[str] = None,
    interval: float = 60.0,
    failure_limit: int = kb.DEFAULT_SPAWN_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    once: bool = False,
    stop_event: Optional[threading.Event] = None,
    task_runner: TaskRunner = run_task_in_process,
    worker_id: Optional[str] = None,
    on_tick: Optional[Callable[[kb.DispatchResult], None]] = None,
) -> None:
    """Run a persistent worker daemon for one assignee profile."""
    from hermes_cli.profiles import normalize_profile_name

    profile = normalize_profile_name(profile)
    if stop_event is None:
        stop_event = threading.Event()
    wake_event = threading.Event()
    wake_socket = _WakeSocket(wake_event)
    wake_socket.start()

    worker_id = worker_id or f"{kb._host_name()}:{os.getpid()}:{profile}:{secrets.token_hex(4)}"  # noqa: SLF001
    resolved_board = board or kb.get_current_board()

    def _handle_stop(_signum, _frame):
        stop_event.set()
        wake_event.set()

    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle_stop)
                except (ValueError, OSError):
                    pass

    def _register() -> None:
        with kb.connect_closing(board=board) as conn:
            kb.register_worker_daemon(
                conn,
                profile=profile,
                worker_id=worker_id,
                pid=os.getpid(),
                board=resolved_board,
                wake_host=wake_socket.host,
                wake_port=wake_socket.port,
                metadata={"runner": "in_process"},
            )

    def _heartbeat() -> None:
        with kb.connect_closing(board=board) as conn:
            if not kb.heartbeat_worker_daemon(
                conn,
                worker_id=worker_id,
                wake_host=wake_socket.host,
                wake_port=wake_socket.port,
            ):
                kb.register_worker_daemon(
                    conn,
                    profile=profile,
                    worker_id=worker_id,
                    pid=os.getpid(),
                    board=resolved_board,
                    wake_host=wake_socket.host,
                    wake_port=wake_socket.port,
                    metadata={"runner": "in_process"},
                )
            kb.prune_stale_worker_daemons(conn)

    _register()
    persistent_runtime = (
        PersistentTaskRuntime()
        if task_runner is run_task_in_process
        else None
    )
    try:
        while not stop_event.is_set():
            _heartbeat()
            started: dict[str, Any] = {}

            def _run_task(task: kb.Task, workspace: str, board: Optional[str]) -> None:
                if persistent_runtime is not None:
                    persistent_runtime.run(task, workspace, board=board)
                else:
                    task_runner(task, workspace, board)

            def _spawn_inline(task: kb.Task, workspace: str, *, board: Optional[str] = None) -> int:
                thread = threading.Thread(
                    target=_run_task,
                    args=(task, workspace, board),
                    name=f"kanban-worker-{task.id}",
                    daemon=False,
                )
                started["thread"] = thread
                started["task_id"] = task.id
                thread.start()
                return os.getpid()

            with kb.connect_closing(board=board) as conn:
                res = kb.dispatch_once(
                    conn,
                    spawn_fn=_spawn_inline,
                    max_spawn=1,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    board=board,
                    assignee_filter=profile,
                    prefer_worker_daemons=False,
                    count_existing_running_for_max_spawn=False,
                )
            if on_tick is not None:
                try:
                    on_tick(res)
                except Exception:
                    pass

            thread = started.get("thread")
            if thread is not None:
                while thread.is_alive() and not stop_event.is_set():
                    thread.join(timeout=1.0)
                    _heartbeat()
                if thread.is_alive():
                    thread.join()
                if once:
                    break
                continue

            if once:
                break
            wake_event.wait(timeout=max(0.1, float(interval)))
            wake_event.clear()
    finally:
        if persistent_runtime is not None:
            persistent_runtime.close()
        with contextlib.suppress(Exception):
            with kb.connect_closing(board=board) as conn:
                kb.unregister_worker_daemon(conn, worker_id=worker_id)
        wake_socket.close()


__all__ = [
    "PersistentTaskRuntime",
    "run_task_in_process",
    "run_worker_daemon",
]
