"""Deterministic interleavings of the delegate runner (review hDAghtsq R2, R3, R10).

Each case pauses REAL code at the exact point a review replay found (no sleeps): a hook wraps the
store primitive the runner calls at that point, blocks the runner thread on an Event, lets the test
thread act, then releases it. Child execution is the only thing replaced (``launch_resumed``).

- R2 (TLC ``WakeCoverage``): drain observes an empty head -> an admission commits -> the producer
  records its wake while the drain still owns the session -> drain cleanup. The input must still run,
  exactly once.
- R3: a claim's queued->started transition and its runner stamp are one publication; a concurrent
  reconcile (claim/get/observe) during an in-flight claim never interrupts it, and a stale
  reservation's cleanup never overwrites the next claim's state.
- R10: an exact ``request_id`` retry through ``ctx.sessions`` returns the original receipt; a
  different request under the same id is a structured refusal, never a raw store error.
"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_state_runtime
from hermes_state import SessionDB
from hermes_state_runtime import list_session_admissions
from tests.waiting import wait_until
from tools.delegate_session_store import DelegateSessionStore

DEADLINE = 10


class _Parent:
    """The live parent agent a drain builds children for (weakref-able, like an AIAgent)."""

    def __init__(self, db):
        self.session_id = "parent"
        self._session_db = db


@pytest.fixture
def rig(tmp_path):
    from tools import delegate_sessions as ds
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    store = DelegateSessionStore(db)
    for sid in ("a", "b"):
        db.create_session(sid, "subagent", parent_session_id="parent", model_config={"_delegate_from": "parent"})
        store.register("parent", sid, model="probe", provider="custom", assignment=f"assignment {sid}")
    parent = _Parent(db)
    yield SimpleNamespace(db=db, store=store, parent=parent, key=(str(Path(db.db_path).resolve()), "b"))
    try:
        wait_until(lambda: not any(k[1] == "b" for k in ds._draining), timeout=DEADLINE, what="drain exit")
    finally:
        with ds._pending_lock:
            ds._live_parents.pop("parent", None)
            ds._pending_launch.clear()
            for k in [k for k in ds._redrain if k[1] == "b"]:
                ds._redrain.pop(k, None)
        db.close()


def _fake_launch(rig, launched):
    """Stand-in for running the child: records the claim, writes its held notices, settles it."""
    from gateway.delivery_targets import write_held_events
    from tools import delegate_sessions as ds

    def launch(parent, record, claim, reservation, **kwargs):
        launched.append({"admission_id": claim["admission_id"], "kinds": sorted(ds.claim_sender_kinds(claim)),
                         "held": [h["admission_id"] for h in claim.get("held") or []], **kwargs})
        write_held_events(rig.db, "b", claim)
        reservation["transferred"] = True
        assert DelegateSessionStore(rig.db).release("b", claim["token"])
        return json.dumps({"results": [{"status": "completed", "summary": "ok", "session_id": "b"}]})
    return launch


# ── R2: the lost wake ────────────────────────────────────────────────────────

@pytest.mark.parametrize("arrival", ["peer", "parent", "held_peer_plus_parent_steer"])
def test_a_wake_landing_after_the_drains_empty_check_still_runs_exactly_once(rig, monkeypatch, arrival):
    from agent.sessions_api import SessionsAPI
    from tools import delegate_sessions as ds
    from tools import delegate_tool
    launched = []
    monkeypatch.setattr(delegate_tool, "launch_resumed", _fake_launch(rig, launched))
    real_list = hermes_state_runtime.list_session_admissions
    reached, proceed = threading.Event(), threading.Event()
    paused = {}

    def gated_list(db, *, session_id, pending_only=True):
        rows = real_list(db, session_id=session_id, pending_only=pending_only)
        me = threading.current_thread()
        if (session_id == "b" and me.name.startswith("delegate-drain-") and not paused
                and not any(r["status"] == "queued" and r.get("wake_policy") is None for r in rows)):
            # Step 1: the drain has read an empty head and is about to leave. Hold it right here.
            paused["thread"] = me
            reached.set()
            assert proceed.wait(DEADLINE), "test never released the drain"
        return rows

    monkeypatch.setattr(hermes_state_runtime, "list_session_admissions", gated_list)
    ds.note_parent(rig.parent)
    assert ds.schedule_drain(rig.key[0], "b", rig.parent)
    assert reached.wait(DEADLINE), "drain never reached its empty-head check"

    # Steps 2-3: the input commits and its producer sees the drain still owned, recording a wake.
    held = None
    if arrival == "peer":
        receipt = SessionsAPI(rig.db).admit("a", "b", "peer question")
        expected_kinds = ["peer"]
    elif arrival == "parent":
        receipt = SessionsAPI(rig.db).admit("parent", "b", "new parent assignment", sender_kind="parent")
        expected_kinds = ["parent"]
    else:
        held = SessionsAPI(rig.db).admit("a", "b", "held peer FYI", wake_policy="batch_with_next_user_turn")
        receipt = json.loads(ds.steer(rig.parent, rig.store, rig.store.get("parent", "b"), "parent plus held peer"))
        assert receipt["status"] == "queued_next_turn", receipt
        expected_kinds = ["parent", "peer"]
    with ds._pending_lock:
        assert rig.key in ds._draining and rig.key in ds._redrain, "the producer must have recorded its wake"
    if arrival != "held_peer_plus_parent_steer":
        assert receipt["delivered"] and receipt["status"] == "delivered", receipt

    # Step 4: drain cleanup. The recorded wake must be honoured: the input runs, once.
    proceed.set()
    wait_until(lambda: launched and rig.key not in ds._draining, timeout=DEADLINE,
               what="the accepted input to run (a stranded admission means a lost wake)")
    paused["thread"].join(DEADLINE)
    assert not paused["thread"].is_alive()
    assert len(launched) == 1, launched
    assert launched[0]["admission_id"] == receipt["admission_id"]
    assert launched[0]["kinds"] == expected_kinds
    if held is not None:
        assert launched[0]["held"] == [held["admission_id"]]
    rows = list_session_admissions(rig.db, session_id="b", pending_only=False)
    assert rows and all(r["status"] == "terminal" for r in rows), rows
    with ds._pending_lock:
        assert rig.key not in ds._draining and rig.key not in ds._redrain
    assert rig.store.running_claim("b") is None


def test_every_drain_exit_goes_through_one_ownership_decision(rig, monkeypatch):
    """A pass that raises, or finds nobody to build the child, still honours a wake recorded meanwhile."""
    from tools import delegate_sessions as ds
    calls = []

    def failing_pass(db_path, session_id, parent):
        calls.append(parent)
        if len(calls) == 1:
            ds.schedule_drain(db_path, session_id, "second-parent")  # a producer while owned
            raise RuntimeError("pass failed")

    monkeypatch.setattr(ds, "_drain_pass", failing_pass)
    with ds._pending_lock:
        ds._draining.add(rig.key)
    ds._drain(rig.key[0], "b", "first-parent")
    assert calls == ["first-parent", "second-parent"]
    with ds._pending_lock:
        assert rig.key not in ds._draining and rig.key not in ds._redrain


# ── R3: claim publication ────────────────────────────────────────────────────

def _pause_after(monkeypatch, module, name, *, when):
    """Wrap ``module.name``: the first call for which ``when(result)`` holds blocks before returning."""
    real = getattr(module, name)
    reached, proceed = threading.Event(), threading.Event()
    state = {}

    def gated(*args, **kwargs):
        result = real(*args, **kwargs)
        if not state and threading.current_thread().name == "first-claimant" and when(result):
            state["paused"] = True
            reached.set()
            assert proceed.wait(DEADLINE), "test never released the claimant"
        return result

    monkeypatch.setattr(module, name, gated)
    return reached, proceed


def test_a_concurrent_reconcile_never_interrupts_an_in_flight_claim(rig, monkeypatch):
    first = rig.store.admit("parent", "b", "first assignment")
    second = rig.store.admit("parent", "b", "second assignment")
    # Hold the first claimant right after its claim transaction returns, before any later write of its own.
    reached, proceed = _pause_after(monkeypatch, hermes_state_runtime, "claim_session_input", when=bool)
    out = {}
    worker = threading.Thread(target=lambda: out.update(claim=rig.store.claim("b")), name="first-claimant")
    worker.start()
    try:
        assert reached.wait(DEADLINE)
        # The claimant is alive and mid-claim. Every reconciling reader must leave its claim alone.
        assert rig.store.get("parent", "b")["status"] == "running"
        rig.store.observe("parent", "b")
        rig.store.list("parent")
        assert rig.store.claim("b") is None, "a second reservation while the first claim is live"
        rows = {r["admission_id"]: (r["status"], r["outcome"])
                for r in list_session_admissions(rig.db, session_id="b", pending_only=False)}
        assert rows[first["admission_id"]] == ("started", None), rows
        assert rows[second["admission_id"]] == ("queued", None), rows
    finally:
        proceed.set()
        worker.join(DEADLINE)
    claim1 = out["claim"]
    assert claim1 and claim1["admission_id"] == first["admission_id"]
    assert rig.store.release("b", claim1["token"])
    assert not rig.store.release("b", claim1["token"])
    claim2 = rig.store.claim("b")
    assert claim2["admission_id"] == second["admission_id"]
    assert rig.store.release("b", claim2["token"])


def test_a_stale_reservations_cleanup_never_overwrites_the_next_claim(rig, monkeypatch):
    """Settlement and its bookkeeping (stamp drop, idle status) are one transaction: a claim landing right
    after the settlement commit keeps ``running``."""
    first = rig.store.admit("parent", "b", "first")
    second = rig.store.admit("parent", "b", "second")
    claim1 = rig.store.claim("b")
    assert claim1["admission_id"] == first["admission_id"]
    reached, proceed = _pause_after(monkeypatch, hermes_state_runtime, "settle_session_input", when=lambda r: True)
    out = {}
    worker = threading.Thread(target=lambda: out.update(ok=rig.store.release("b", claim1["token"])),
                              name="first-claimant")
    worker.start()
    try:
        assert reached.wait(DEADLINE)
        claim2 = rig.store.claim("b")
        assert claim2 and claim2["admission_id"] == second["admission_id"]
    finally:
        proceed.set()
        worker.join(DEADLINE)
    assert out["ok"] is True
    row = rig.db._read_one("SELECT status FROM sessions WHERE id = 'b'")
    assert row["status"] == "running", "the first release's late cleanup clobbered the live claim"
    stamp = rig.db._read_one("SELECT value FROM state_meta WHERE key = ?", ("delegate.runner.v1." + claim2["admission_id"],))
    assert stamp is not None
    assert rig.store.release("b", claim2["token"])


def test_a_dead_stamped_claim_is_recovered_and_its_token_refused(rig):
    rig.store.admit("parent", "b", "abandoned")
    claim = rig.store.claim("b")
    rig.db._write_sql("UPDATE state_meta SET value = ? WHERE key = ?",
                      (json.dumps({"pid": 2 ** 22 + 4321, "pidns": None, "start_ticks": 1}),
                       "delegate.runner.v1." + claim["admission_id"]))
    assert rig.store.get("parent", "b")["status"] == "idle"
    assert [r["outcome"] for r in list_session_admissions(rig.db, session_id="b", pending_only=False)] == ["interrupted"]
    assert not rig.store.release("b", claim["token"])
    assert rig.db._read_one("SELECT 1 FROM state_meta WHERE key LIKE 'delegate.runner.%'") is None


def test_a_stampless_started_claim_is_recovered_only_after_the_legacy_grace(rig, monkeypatch):
    """No atomic claim leaves a started row without its stamp; one that exists is a pre-fix leftover
    (or a non-runner owner's) and is never interrupted on sight."""
    from hermes_state_runtime import claim_session_input
    from tools import delegate_session_store as dss
    rig.store.admit("parent", "b", "legacy claim")
    legacy = claim_session_input(rig.db, epoch=dss.runtime_epoch(rig.db), session_id="b")  # no stamp
    assert rig.store.get("parent", "b")["status"] in ("idle", "running")
    status = {r["admission_id"]: r["status"] for r in list_session_admissions(rig.db, session_id="b")}
    assert status[legacy["admission_id"]] == "started"
    marker = "delegate.runner.unstamped.v1." + legacy["admission_id"]
    seen = rig.db._read_one("SELECT value FROM state_meta WHERE key = ?", (marker,))
    assert seen is not None
    # Still stampless once the grace has passed since its first sighting: abandoned.
    rig.db._write_sql("UPDATE state_meta SET value = ? WHERE key = ?",
                      (json.dumps(float(json.loads(seen[0])) - dss.LEGACY_UNSTAMPED_GRACE_S - 1), marker))
    assert rig.store.get("parent", "b")["status"] == "idle"
    assert [r["outcome"] for r in list_session_admissions(rig.db, session_id="b", pending_only=False)] == ["interrupted"]
    assert rig.db._read_one("SELECT 1 FROM state_meta WHERE key = ?", (marker,)) is None


def test_a_stampless_claim_on_a_session_the_runner_does_not_own_is_never_touched(rig):
    """A gateway-owned session (a webhook session delivering to the parent) running a parent_message turn."""
    from hermes_state_runtime import admit_session_input, claim_session_input
    from tools import delegate_session_store as dss
    rig.db.create_session("hook", "webhook")
    rig.db._write_sql("UPDATE sessions SET delivery_target = ? WHERE id = 'hook'",
                      (json.dumps({"kind": "session", "session_id": "parent", "attach": True}),))
    epoch = dss.runtime_epoch(rig.db)
    admit_session_input(rig.db, epoch=epoch, principal_id="parent:session:parent", session_id="hook",
                        request_id="r1", payload={"text": "from parent", "sender_kind": "parent"})
    owner_claim = claim_session_input(rig.db, epoch=epoch, session_id="hook")
    rig.store._reconcile("hook")
    rig.store._reconcile("hook")
    row = {r["admission_id"]: r["status"] for r in list_session_admissions(rig.db, session_id="hook")}
    assert row[owner_claim["admission_id"]] == "started"


# ── R10: exact request_id retry through the facade ───────────────────────────

def test_an_exact_request_id_retry_returns_the_original_receipt(rig):
    from agent.sessions_api import SessionAdmissionError, SessionsAPI
    api = SessionsAPI(rig.db)
    first = api.admit("a", "b", "peer question", request_id="stable-request")
    again = api.admit("a", "b", "peer question", request_id="stable-request")
    assert again["admission_id"] == first["admission_id"] and again["delivered"]
    rows = [r for r in list_session_admissions(rig.db, session_id="b", pending_only=False)
            if r["request_id"] == "stable-request"]
    assert len(rows) == 1
    with pytest.raises(SessionAdmissionError) as refused:
        api.admit("a", "b", "a different question", request_id="stable-request")
    assert refused.value.code == "request_conflict"
    assert refused.value.to_dict()["reason"] == "admission_conflict"
