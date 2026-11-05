"""Persistent child contracts across processes: one FIFO claim at a time, dead runners never replayed.

The unified store (tools/delegate_session_store.py) keeps children as sessions rows driven through
``session_admissions``; real processes catch races an in-process lock or mocked database would hide.
Single-process contracts live in tests/tools/test_delegate_unified_sessions.py.
"""

import multiprocessing
from pathlib import Path

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import list_session_admissions
from tools.delegate_session_store import DelegateSessionError, DelegateSessionStore


@pytest.fixture
def state(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    store = DelegateSessionStore(db)
    db.create_session("child", "subagent", parent_session_id="parent", model_config={"_delegate_from": "parent"})
    store.register("parent", "child", model="chosen-model", provider="chosen-provider",
                   assignment="Review the implementation")
    yield db, store
    db.close()


def _claim_worker(path, ready, start, results, finish):
    db = SessionDB(db_path=Path(path))
    try:
        store = DelegateSessionStore(db)
        ready.put(True)
        start.wait(20)
        try:
            _, token = store.acquire("parent", "child", "Competing assignment")
            results.put(("acquired", token))
        except DelegateSessionError as exc:
            results.put((exc.code, None))
        finish.wait(20)
    finally:
        db.close()


def _spawn(db, count):
    ctx = multiprocessing.get_context("spawn")
    ready, results = ctx.Queue(), ctx.Queue()
    start, finish = ctx.Event(), ctx.Event()
    workers = [ctx.Process(target=_claim_worker, args=(str(db.db_path), ready, start, results, finish))
               for _ in range(count)]
    for worker in workers:
        worker.start()
    return ready, results, start, finish, workers


def _stop(ready, results, finish, workers):
    finish.set()
    for worker in workers:
        worker.join(timeout=20)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
    ready.close()
    results.close()


def test_independent_processes_cannot_both_claim_one_child(state):
    db, store = state
    ready, results, start, finish, workers = _spawn(db, 2)
    try:
        for _ in workers:
            assert ready.get(timeout=20)
        start.set()
        claims = [results.get(timeout=20) for _ in workers]
        assert sorted(code for code, _ in claims) == ["acquired", "busy"]
        winner = next(token for code, token in claims if code == "acquired")
        assert store.get("parent", "child")["status"] == "running"
        assert not store.release("child", "foreign:1:1")
        assert store.release("child", winner)
        # The loser's admission was withdrawn, never left queued to run later.
        assert [r["outcome"] for r in list_session_admissions(db, session_id="child", pending_only=False)] in (
            ["completed", "cancelled"], ["cancelled", "completed"])
    finally:
        _stop(ready, results, finish, workers)
    assert all(worker.exitcode == 0 for worker in workers)


@pytest.mark.platforms("linux")
def test_dead_process_becomes_idle_without_replaying_or_expiring_live_process(state):
    db, store = state
    ready, results, start, finish, workers = _spawn(db, 1)
    try:
        assert ready.get(timeout=20)
        start.set()
        code, old_token = results.get(timeout=20)
        assert code == "acquired"
        # A live owner keeps its claim however old its activity looks.
        assert store.get("parent", "child")["status"] == "running"
        finish.set()
        workers[0].join(timeout=20)
        assert workers[0].exitcode == 0
        recovered = store.get("parent", "child")
        assert recovered["status"] == "idle"
        assert db.get_messages("child") == []  # no automatic replay of the abandoned assignment
        assert [r["outcome"] for r in list_session_admissions(db, session_id="child", pending_only=False)] == ["interrupted"]
        _, new_token = store.acquire("parent", "child", "Explicit continuation")
        assert not store.release("child", old_token)
        assert store.release("child", new_token)
    finally:
        _stop(ready, results, finish, workers)
