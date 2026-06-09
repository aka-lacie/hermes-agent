from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from gateway.kanban_dispatch_signal import nudge_kanban_dispatch
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker as kw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _wait_until(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_dispatch_defers_to_healthy_worker_daemon(kanban_home, all_assignees_spawnable):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="warm task", assignee="worker")
        kb.register_worker_daemon(
            conn,
            profile="worker",
            worker_id="test-worker",
            pid=os.getpid(),
            wake_host="127.0.0.1",
            wake_port=9,
        )

        def _should_not_spawn(_task, _workspace):
            raise AssertionError("fallback spawn should not run")

        res = kb.dispatch_once(conn, spawn_fn=_should_not_spawn)
        task = kb.get_task(conn, tid)

    assert res.spawned == []
    assert res.deferred_to_daemon == [(tid, "worker")]
    assert task is not None
    assert task.status == "ready"


def test_dispatch_falls_back_when_worker_daemon_is_stale(kanban_home, all_assignees_spawnable):
    spawned: list[str] = []
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cold task", assignee="worker")
        kb.register_worker_daemon(
            conn,
            profile="worker",
            worker_id="stale-worker",
            pid=os.getpid(),
            wake_host="127.0.0.1",
            wake_port=9,
        )
        conn.execute(
            "UPDATE kanban_worker_daemons SET last_heartbeat = ? WHERE worker_id = ?",
            (int(time.time()) - kb.DEFAULT_WORKER_DAEMON_HEALTH_SECONDS - 5, "stale-worker"),
        )
        conn.commit()

        def _spawn(task, _workspace):
            spawned.append(task.id)
            return 12345

        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)

    assert spawned == [tid]
    assert res.spawned and res.spawned[0][0] == tid
    assert res.deferred_to_daemon == []
    assert task is not None
    assert task.status == "running"


def test_worker_daemon_claims_only_matching_profile(kanban_home, all_assignees_spawnable):
    handled: list[str] = []

    def _runner(task, _workspace, board=None):
        handled.append(task.id)
        with kb.connect_closing(board=board) as conn:
            kb.complete_task(conn, task.id, summary="done")

    with kb.connect_closing() as conn:
        worker_id = kb.create_task(conn, title="worker task", assignee="worker")
        other_id = kb.create_task(conn, title="other task", assignee="other")

    kw.run_worker_daemon(
        profile="worker",
        interval=0.05,
        once=True,
        task_runner=_runner,
        worker_id="claim-test",
    )

    with kb.connect_closing() as conn:
        worker_task = kb.get_task(conn, worker_id)
        other_task = kb.get_task(conn, other_id)

    assert handled == [worker_id]
    assert worker_task is not None and worker_task.status == "done"
    assert other_task is not None and other_task.status == "ready"


def test_worker_daemon_back_to_back_tasks_get_fresh_conversation_state(
    kanban_home,
    all_assignees_spawnable,
    monkeypatch,
):
    import cli as cli_module

    stop = threading.Event()
    calls: list[dict[str, object]] = []

    class FakeAgent:
        def __init__(self, owner):
            self.owner = owner
            self.quiet_mode = False
            self.suppress_status_output = False
            self.stream_delta_callback = None
            self.tool_gen_callback = None

        def run_conversation(self, *, user_message, conversation_history=None, **_kwargs):
            task_id = os.environ["HERMES_KANBAN_TASK"]
            calls.append(
                {
                    "task_id": task_id,
                    "history": list(conversation_history or []),
                    "cli_history": list(self.owner.conversation_history),
                    "message": user_message,
                }
            )
            with kb.connect_closing() as conn:
                kb.complete_task(conn, task_id, summary="done")
            if len(calls) >= 2:
                stop.set()
            return {"final_response": "done"}

    class FakeHermesCLI:
        instances = 0

        def __init__(self):
            type(self).instances += 1
            self.tool_progress_mode = "auto"
            self.conversation_history = ["should be cleared"]
            self.agent = None

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, _prompt):
            return {"model": None, "runtime": None, "request_overrides": None}

        def _init_agent(self, **_kwargs):
            self.agent = FakeAgent(self)
            return True

        def new_session(self, silent=False):
            self.conversation_history = []

    monkeypatch.setattr(cli_module, "HermesCLI", FakeHermesCLI)

    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="worker")
        second = kb.create_task(conn, title="second", assignee="worker")

    t = threading.Thread(
        target=kw.run_worker_daemon,
        kwargs={
            "profile": "worker",
            "interval": 0.05,
            "stop_event": stop,
            "worker_id": "state-test",
            "task_runner": kw.run_task_in_process,
        },
        daemon=True,
    )
    t.start()
    t.join(timeout=5)

    assert not t.is_alive()
    assert [c["task_id"] for c in calls] == [first, second]
    assert [c["history"] for c in calls] == [[], []]
    assert [c["cli_history"] for c in calls] == [[], []]
    assert all(str(c["message"]).startswith("work kanban task ") for c in calls)
    assert FakeHermesCLI.instances == 1


def test_worker_daemon_wakes_on_nudge_without_poll_delay(
    kanban_home,
    all_assignees_spawnable,
):
    handled = threading.Event()
    stop = threading.Event()
    handled_ids: list[str] = []

    def _runner(task, _workspace, board=None):
        handled_ids.append(task.id)
        with kb.connect_closing(board=board) as conn:
            kb.complete_task(conn, task.id, summary="done")
        handled.set()
        stop.set()

    t = threading.Thread(
        target=kw.run_worker_daemon,
        kwargs={
            "profile": "worker",
            "interval": 30.0,
            "stop_event": stop,
            "task_runner": _runner,
            "worker_id": "wake-test",
        },
        daemon=True,
    )
    t.start()

    def _registered() -> bool:
        with kb.connect_closing() as conn:
            return bool(kb.list_worker_daemons(conn, profile="worker", healthy_only=True))

    assert _wait_until(_registered)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="wake task", assignee="worker")

    nudge_kanban_dispatch(task_id=tid)

    assert handled.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert handled_ids == [tid]

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None and task.status == "done"
