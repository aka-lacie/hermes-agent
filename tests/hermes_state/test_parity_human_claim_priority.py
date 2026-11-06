"""User priority agrees between preflight and SQLite claim; background work stays durable."""
import pytest

from hermes_state import SessionDB
from hermes_state_runtime import (
    admit_session_input, claim_session_input, list_session_admissions, settle_session_input,
)
import hermes_state_wake
from hermes_state_wake import ack_held_inputs
from tools.delegate_session_store import runtime_epoch


@pytest.fixture
def queue(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "discord")
    epoch = runtime_epoch(db)

    def admit(label, *, human=False, held=False):
        payload = {"text": label, "native_text_v1": {"source": {"platform": "discord"}}}
        if not human:
            payload["native_text_v1"]["automation"] = {"identity": label}
        return admit_session_input(
            db, epoch=epoch, principal_id="human:u" if human else "automation:key",
            session_id="parent", request_id=label, payload=payload,
            wake_policy="batch_with_next_user_turn" if held else "immediate")

    yield db, epoch, admit
    db.close()


def test_human_claim_precedes_immediate_notices_without_losing_fifo_or_held_batch(queue):
    db, epoch, admit = queue
    admit("notice1")
    admit("notice2")
    held = admit("held", held=True)
    human = admit("human1", human=True)
    admit("human2", human=True)
    claimed = []
    while True:
        head = hermes_state_wake.next_claimable(list_session_admissions(db, session_id="parent"))
        if head is None:
            break
        row = claim_session_input(db, epoch=epoch, session_id="parent", head=head["admission_id"])
        assert row and row["admission_id"] == head["admission_id"]
        claimed.append(row["payload"]["text"])
        if row["admission_id"] == human["admission_id"]:
            assert [r["admission_id"] for r in row["held"]] == [held["admission_id"]]
            ack_held_inputs(db, epoch=epoch, generation=row["generation"],
                            admission_ids=[held["admission_id"]], session_id="parent")
        else:
            assert not row.get("held")
        settle_session_input(db, epoch=epoch, admission_id=row["admission_id"],
                             generation=row["generation"], outcome="completed")
    assert claimed == ["human1", "human2", "notice1", "notice2"]


def test_human_arrival_invalidates_background_preflight_but_not_running_claim(queue):
    db, epoch, admit = queue
    notice = admit("notice")
    old_head = list_session_admissions(db, session_id="parent")[0]
    human = admit("human", human=True)
    assert claim_session_input(db, epoch=epoch, session_id="parent", head=old_head["admission_id"]) is False
    row = claim_session_input(db, epoch=epoch, session_id="parent", head=human["admission_id"])
    assert row["admission_id"] == human["admission_id"]
    admit("human2", human=True)
    assert claim_session_input(db, epoch=epoch, session_id="parent") is None
    pending = list_session_admissions(db, session_id="parent")
    assert next(r for r in pending if r["admission_id"] == notice["admission_id"])["status"] == "queued"
