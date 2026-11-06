"""Held-notice transfers are bound to their own claim (review hDAghtsq R4).

Generations are per session, so two unrelated sessions routinely hold claims at the same
``(owner_epoch, generation)``. Settling one must never requeue the other's in-flight held notices,
whose acknowledgement must still match, and the notice must never be attached to a second turn. The
settlement's requeue must also stay on the ``(target_session_id, status, seq)`` index instead of
scanning the whole admission ledger inside the writer's critical section.
"""

import pytest

import hermes_state_wake
from hermes_state import SessionDB
from hermes_state_runtime import (
    admit_session_input, claim_session_input, get_session_admission, settle_session_input,
)
from hermes_state_wake import ack_held_inputs
from tools.delegate_session_store import runtime_epoch


@pytest.fixture
def two_sessions(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    for sid in ("a", "b"):
        db.create_session(sid, "cli")
    epoch = runtime_epoch(db)
    notices, claims = {}, {}
    for sid in ("a", "b"):
        notices[sid] = admit_session_input(db, epoch=epoch, principal_id="system:notice", session_id=sid,
                                           request_id="notice", payload={"text": "notice " + sid},
                                           wake_policy="batch_with_next_user_turn")
        admit_session_input(db, epoch=epoch, principal_id="human:human", session_id=sid, request_id="human",
                            payload={"text": "user " + sid, "sender_kind": "human"})
        claims[sid] = claim_session_input(db, epoch=epoch, session_id=sid)
    assert claims["a"]["generation"] == claims["b"]["generation"] == 1
    assert [h["admission_id"] for h in claims["b"]["held"]] == [notices["b"]["admission_id"]]
    yield db, epoch, notices, claims
    db.close()


def test_settling_one_session_leaves_anothers_held_transfer_alone(two_sessions):
    db, epoch, notices, claims = two_sessions
    b_notice = notices["b"]["admission_id"]
    before = get_session_admission(db, admission_id=b_notice)
    assert (before["status"], before["outcome"]) == ("terminal", "steering")

    settle_session_input(db, epoch=epoch, admission_id=claims["a"]["admission_id"], generation=1, outcome="completed")

    after = get_session_admission(db, admission_id=b_notice)
    assert (after["status"], after["outcome"], after["generation"]) == ("terminal", "steering", 1), after
    # A's own unacknowledged notice did go back to wait for A's next user turn.
    a_notice = get_session_admission(db, admission_id=notices["a"]["admission_id"])
    assert (a_notice["status"], a_notice["generation"]) == ("queued", None)
    # B's consumer wrote its notice: the acknowledgement still matches B's claim.
    assert ack_held_inputs(db, epoch=epoch, generation=1, admission_ids=[b_notice], session_id="b") == 1
    assert get_session_admission(db, admission_id=b_notice)["outcome"] == "batched"
    assert get_session_admission(db, admission_id=claims["b"]["admission_id"])["status"] == "started"

    settle_session_input(db, epoch=epoch, admission_id=claims["b"]["admission_id"], generation=1, outcome="completed")
    admit_session_input(db, epoch=epoch, principal_id="human:human", session_id="b", request_id="human2",
                        payload={"text": "next user b", "sender_kind": "human"})
    following = claim_session_input(db, epoch=epoch, session_id="b")
    assert not following.get("held"), "an acknowledged notice was attached to a second turn"


def test_an_ack_scoped_to_one_session_never_matches_anothers_transfer(two_sessions):
    db, epoch, notices, _ = two_sessions
    assert ack_held_inputs(db, epoch=epoch, generation=1, admission_ids=[notices["b"]["admission_id"]],
                           session_id="a") == 0
    assert get_session_admission(db, admission_id=notices["b"]["admission_id"])["outcome"] == "steering"


def test_the_settlement_requeue_uses_the_target_index_not_a_table_scan(two_sessions, monkeypatch):
    db, epoch, _, claims = two_sessions
    real = hermes_state_wake.requeue_unacked_held
    traced = []

    def tracing(conn, **kwargs):
        conn.set_trace_callback(traced.append)
        try:
            return real(conn, **kwargs)
        finally:
            conn.set_trace_callback(None)

    monkeypatch.setattr(hermes_state_wake, "requeue_unacked_held", tracing)
    settle_session_input(db, epoch=epoch, admission_id=claims["a"]["admission_id"], generation=1, outcome="completed")
    statements = [s for s in traced if s.lstrip().upper().startswith("UPDATE SESSION_ADMISSIONS")]
    assert len(statements) == 1, traced
    with db._read_ctx() as conn:
        plan = " | ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + statements[0]))
    assert "SCAN session_admissions" not in plan, plan
    assert "session_admissions_pending" in plan and "target_session_id=?" in plan, plan
