"""Retirement serializes with peer/parent input, claim and late settlement (Astra A1).

The scheduling seam pauses immediately BEFORE the real SQLite writer transaction;
all reads, authorizations, writes and return values remain production code.
"""
import threading
from types import SimpleNamespace

import pytest

from agent.sessions_api import SessionAdmissionError, SessionsAPI
from hermes_state import SessionDB
from hermes_state_runtime import get_session_admission, list_session_admissions
from tools.delegate_session_store import DelegateSessionError, DelegateSessionStore


@pytest.fixture
def rig(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    store = DelegateSessionStore(db)
    for sid in ("a", "b"):
        db.create_session(sid, "subagent", parent_session_id="parent")
        store.register("parent", sid, model="test", provider="test", assignment="fixture")
    yield SimpleNamespace(db=db, store=store, api=SessionsAPI(db))
    db.close()


def _pause_write(monkeypatch, db, name):
    ready, proceed = threading.Event(), threading.Event()
    real = db._execute_write
    reached = False

    def gated(fn, **kwargs):
        nonlocal reached
        if threading.current_thread().name == name and not reached:
            reached = True
            ready.set()
            assert proceed.wait(10), "writer gate was not released"
        return real(fn, **kwargs)

    monkeypatch.setattr(db, "_execute_write", gated)
    return ready, proceed


def _call(fn, outputs):
    try:
        outputs.append(fn())
    except (DelegateSessionError, SessionAdmissionError) as exc:
        outputs.append(exc.code)
    except BaseException as exc:  # surfaced by the caller assertion
        outputs.append(exc)


@pytest.mark.parametrize("claim_before_close", [False, True])
def test_close_vs_peer_drain_has_one_atomic_winner(rig, monkeypatch, claim_before_close):
    # Same two schedules as the code-replayed TLC witness: close's preflight saw idle;
    # peer admission arrives before the final write, and claim lands on either side of it.
    ready, proceed = _pause_write(monkeypatch, rig.db, "retire")
    outputs = []
    worker = threading.Thread(target=_call, name="retire", args=(lambda: rig.store.close("parent", "b"), outputs))
    worker.start()
    try:
        assert ready.wait(10)
        peer = rig.api.admit("a", "b", "ordinary sibling input")
        claim = rig.store.claim("b") if claim_before_close else None
    finally:
        proceed.set()
        worker.join(20)
    assert not worker.is_alive()
    if claim_before_close:
        assert outputs == ["busy"]
        assert rig.store.get("parent", "b")["status"] == "running"
        assert rig.store.release("b", claim["token"])
        assert rig.store.close("parent", "b")["status"] == "closed"
        assert not rig.store.release("b", claim["token"])  # late duplicate cleanup
    else:
        assert len(outputs) == 1 and isinstance(outputs[0], dict), outputs
        assert outputs[0]["status"] == "closed"
        assert rig.store.claim("b") is None
        row = get_session_admission(rig.db, admission_id=peer["admission_id"])
        assert (row["status"], row["outcome"]) == ("terminal", "cancelled")
    assert rig.store.get("parent", "b")["status"] == "closed"
    with pytest.raises(DelegateSessionError, match="closed"):
        rig.store.admit("parent", "b", "no resurrection")
    with pytest.raises(SessionAdmissionError) as refused:
        rig.api.admit("a", "b", "no peer resurrection")
    assert refused.value.code == "closed"
    assert list_session_admissions(rig.db, session_id="b") == []


@pytest.mark.parametrize("sender", ["parent", "peer"])
def test_admission_rechecks_retirement_after_its_preflight(rig, monkeypatch, sender):
    ready, proceed = _pause_write(monkeypatch, rig.db, "admit")
    # Ensure the writer being paused is admission, not the lazy epoch initialization.
    from tools.delegate_session_store import runtime_epoch
    runtime_epoch(rig.db)
    outputs = []
    admit = (lambda: rig.store.admit("parent", "b", "late parent")) if sender == "parent" else (
        lambda: rig.api.admit("a", "b", "late peer"))
    worker = threading.Thread(target=_call, name="admit", args=(admit, outputs))
    worker.start()
    try:
        assert ready.wait(10)
        assert rig.store.close("parent", "b")["status"] == "closed"
    finally:
        proceed.set()
        worker.join(20)
    assert not worker.is_alive() and outputs == ["closed"], outputs
    assert list_session_admissions(rig.db, session_id="b") == []


def test_late_terminal_callback_cannot_reopen_retirement(rig):
    from tools.delegate_session_store import _settled_write
    rig.store.close("parent", "b")
    # Replaying a delayed bookkeeping callback needs no started turn or fabricated DB row.
    rig.db._execute_write(lambda conn: _settled_write("b", "idle")(conn, {"admission_id": "old"}))
    assert rig.store.get("parent", "b")["status"] == "closed"
