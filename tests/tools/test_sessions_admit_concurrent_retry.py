"""Overlapping exact ``request_id`` retries through ``ctx.sessions`` (re-review 5zCVKjSt, R10 residual).

Two identical first ``SessionsAPI.admit(..., request_id=X)`` calls can both read "no admission yet"
before either writes, so each carries its own generated ``sent_at``. The store reconciles them inside
the admission transaction: the same caller request gets the ONE original admission's receipt, while a
request that differs in any caller-supplied field (text, meta, kind, intent) is still refused
``request_conflict``. The interleaving is forced at the real seam (after the pre-write
``_exact_retry`` read, before the store call) with a barrier; return values are passed through.
"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError, admit_session_input, list_session_admissions
from tests.waiting import wait_until
from tools.delegate_session_store import DelegateSessionStore, runtime_epoch

DEADLINE = 10
REQUEST = "parallel-exact"


@pytest.fixture
def rig(tmp_path):
    from tools import delegate_sessions as ds
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    store = DelegateSessionStore(db)
    for sid in ("a", "b"):
        db.create_session(sid, "subagent", parent_session_id="parent", model_config={"_delegate_from": "parent"})
        store.register("parent", sid, model="probe", provider="custom", assignment=f"assignment {sid}")
    yield SimpleNamespace(db=db, store=store, key=(str(Path(db.db_path).resolve()), "b"))
    try:
        wait_until(lambda: not any(k[1] == "b" for k in ds._draining), timeout=DEADLINE, what="drain exit")
    finally:
        with ds._pending_lock:
            ds._live_parents.pop("parent", None)
            ds._pending_launch.clear()
            for k in [k for k in ds._redrain if k[1] == "b"]:
                ds._redrain.pop(k, None)
        db.close()


def _rows(db, request_id=REQUEST):
    return [r for r in list_session_admissions(db, session_id="b", pending_only=False) if r["request_id"] == request_id]


def _overlap(monkeypatch, rig, calls):
    """Run each ``calls[i](api)`` in its own thread; every thread passes the pre-write retry read before
    any reaches the admission transaction. Returns per-call ``{'receipt'|'error'|'unexpected': ...}``
    plus what each preflight returned."""
    from agent.sessions_api import SessionAdmissionError, SessionsAPI
    from gateway import delivery_targets
    names = {f"admit-{i}" for i in range(len(calls))}
    barrier = threading.Barrier(len(calls))
    real = delivery_targets._exact_retry
    preflights = {}

    def gated(*args, **kwargs):
        result = real(*args, **kwargs)
        name = threading.current_thread().name
        if name in names and name not in preflights:
            preflights[name] = result
            barrier.wait(DEADLINE)  # both preflights done, neither has written
        return result

    monkeypatch.setattr(delivery_targets, "_exact_retry", gated)
    outputs = {}

    def run(i, call):
        try:
            outputs[i] = {"receipt": call(SessionsAPI(rig.db))}
        except SessionAdmissionError as exc:
            outputs[i] = {"error": exc.to_dict()}
        except BaseException as exc:  # noqa: BLE001 - reported, then asserted absent
            outputs[i] = {"unexpected": repr(exc)}

    workers = [threading.Thread(target=run, args=(i, c), name=f"admit-{i}") for i, c in enumerate(calls)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(DEADLINE * 2)
    assert not any(w.is_alive() for w in workers)
    monkeypatch.setattr(delivery_targets, "_exact_retry", real)
    assert not any("unexpected" in o for o in outputs.values()), outputs
    return [outputs[i] for i in range(len(calls))], preflights


_CHANGES = {"text": {"text": "another request"}, "meta": {"meta": {"topic": "two"}},
            "kind": {"kind": "peer_note"}, "intent": {"intent": "steer"}}
_BASE = {"text": "same request", "meta": {"topic": "one"}, "kind": "peer_message", "intent": "queue"}


def _admit(request):
    def call(api):
        return api.admit("a", "b", request["text"], meta=request["meta"], kind=request["kind"],
                         intent=request["intent"], request_id=REQUEST)
    return call


@pytest.mark.parametrize("trial", range(3))
def test_overlapping_identical_first_requests_both_get_the_original_receipt(rig, monkeypatch, trial):
    from agent.sessions_api import SessionsAPI
    outputs, preflights = _overlap(monkeypatch, rig, [_admit(_BASE), _admit(_BASE)])
    assert list(preflights.values()) == [None, None], "the race was not forced: a preflight saw the admission"
    assert all("receipt" in o for o in outputs), outputs
    ids = {o["receipt"]["admission_id"] for o in outputs}
    assert len(ids) == 1 and all(o["receipt"]["delivered"] for o in outputs)
    rows = _rows(rig.db)
    assert [r["admission_id"] for r in rows] == list(ids)
    # The stored row is the winner's bytes, unchanged by the loser's replay; a serial retry still agrees.
    later = _admit(_BASE)(SessionsAPI(rig.db))
    assert later["admission_id"] in ids
    assert len(_rows(rig.db)) == 1 and _rows(rig.db)[0]["payload"] == rows[0]["payload"]


@pytest.mark.parametrize("change", sorted(_CHANGES))
def test_overlapping_first_requests_that_differ_still_conflict(rig, monkeypatch, change):
    outputs, preflights = _overlap(monkeypatch, rig, [_admit(_BASE), _admit({**_BASE, **_CHANGES[change]})])
    assert list(preflights.values()) == [None, None]
    receipts = [o["receipt"] for o in outputs if "receipt" in o]
    errors = [o["error"] for o in outputs if "error" in o]
    assert len(receipts) == 1 and len(errors) == 1, outputs
    assert errors[0]["error"] == "request_conflict"
    assert errors[0]["reason"] == "admission_conflict" and errors[0]["field"] == "request_id"
    assert [r["admission_id"] for r in _rows(rig.db)] == [receipts[0]["admission_id"]]


@pytest.mark.parametrize("change", sorted(_CHANGES))
def test_a_serial_retry_returns_the_original_and_a_changed_one_conflicts(rig, change):
    from agent.sessions_api import SessionAdmissionError, SessionsAPI
    api = SessionsAPI(rig.db)
    first = _admit(_BASE)(api)
    again = _admit(_BASE)(api)
    assert again["admission_id"] == first["admission_id"]
    with pytest.raises(SessionAdmissionError) as refused:
        _admit({**_BASE, **_CHANGES[change]})(api)
    assert refused.value.code == "request_conflict" and refused.value.to_dict()["field"] == "request_id"
    assert [r["admission_id"] for r in _rows(rig.db)] == [first["admission_id"]]


# ── the store contract the facade relies on ─────────────────────────────────

def _store_admit(rig, payload, *, intent="queue", same=None):
    return admit_session_input(rig.db, epoch=runtime_epoch(rig.db), principal_id="p", session_id="b",
                               request_id="store-req", payload=payload, intent=intent, _same_request=same)


def test_the_store_replays_only_when_the_producer_names_the_same_request(rig):
    first = _store_admit(rig, {"text": "x", "at": 1})
    seen = []

    def same(stored, fresh):
        seen.append((stored, fresh))
        return {k: v for k, v in stored.items() if k != "at"} == {k: v for k, v in fresh.items() if k != "at"}

    # No predicate: a different digest is a conflict (the pre-existing contract).
    with pytest.raises(RuntimeStoreError) as plain:
        _store_admit(rig, {"text": "x", "at": 2})
    assert plain.value.reason == "admission_conflict" and plain.value.field == "request_id"
    # Same request apart from the generated field: the original row, original bytes.
    replay = _store_admit(rig, {"text": "x", "at": 2}, same=same)
    assert replay["admission_id"] == first["admission_id"] and replay["payload"] == {"text": "x", "at": 1}
    assert seen == [({"at": 1, "text": "x"}, {"at": 2, "text": "x"})]
    # The predicate saying "different" is a genuine conflict; it is never consulted across intents.
    with pytest.raises(RuntimeStoreError) as changed:
        _store_admit(rig, {"text": "y", "at": 3}, same=same)
    assert changed.value.reason == "admission_conflict"
    seen.clear()
    with pytest.raises(RuntimeStoreError) as other_intent:
        _store_admit(rig, {"text": "x", "at": 1}, intent="steer", same=lambda s, f: True)
    assert other_intent.value.reason == "admission_conflict" and seen == []
    rows = [r for r in list_session_admissions(rig.db, session_id="b", pending_only=False)
            if r["request_id"] == "store-req"]
    assert [r["admission_id"] for r in rows] == [first["admission_id"]]
