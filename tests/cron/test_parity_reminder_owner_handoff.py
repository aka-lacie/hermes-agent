"""A reminder fired outside the gateway is committed into its chat's own FIFO for the gateway owner.

Regression for the dev2 -> dev3 parity review (P2): dashboard Run-now, ``hermes cron run``, a CLI
session's cron tool and a headless tick deliver a literal reminder in their own process, where no
gateway owner sink is bound, so the fire was consumed with ``no_owner`` and never reached the chat.
It is now admitted into the chat's ``session_admissions`` as the typed automation row the owner builds
itself, which the owner claims as a cron system event (gateway/session_automation_handoff.py).
"""
import json
import time
from datetime import datetime
from unittest.mock import Mock

import pytest

from cron import jobs, scheduler

KEY = "agent:main:discord:dm:42"


@pytest.fixture
def human_chat(monkeypatch, tmp_path):
    """A real store holding a Discord DM whose route has one committed (settled) human turn."""
    from gateway.config import Platform
    from gateway.session import SessionSource
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_input
    from tools.delegate_session_store import runtime_epoch
    home = tmp_path / "home"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")
    db.create_session("human-chat", source="discord", session_key=KEY, chat_id="42", chat_type="dm", user_id="u1")
    source = SessionSource(platform=Platform.DISCORD, chat_id="42", chat_type="dm", user_id="u1").to_dict()
    source.update(is_bot=False, message_id="m1")
    human = {"text": "hello", "native_text_v1": {
        "source": source, "route": KEY, "event": {"message_id": "m1"},
        "timestamp": datetime.now().astimezone().isoformat(), "provenance": {"transport_profile": None}}}
    epoch = runtime_epoch(db)
    admit_session_input(db, epoch=epoch, principal_id="u1", session_id="human-chat", request_id="m1", payload=human)
    turn = claim_session_input(db, epoch=epoch, session_id="human-chat")
    settle_session_input(db, epoch=epoch, admission_id=turn["admission_id"], generation=turn["generation"],
                         outcome="completed", result={"result": {"final_response": "hi"}, "usage": {}})
    monkeypatch.setattr("hermes_state_registry.acquire", lambda: db)
    monkeypatch.setattr("hermes_state_registry.release", lambda db: None)
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker",
                        Mock(side_effect=AssertionError("a literal reminder starts no worker")))
    yield db, home
    db.close()


def _queued(db):
    from hermes_state_runtime import list_session_admissions
    return [row for row in list_session_admissions(db, session_id="human-chat") if row["status"] == "queued"]


def test_ownerless_reminder_is_queued_then_claimed_as_a_typed_cron_event(human_chat):
    from gateway.session_envelope import restore_native
    from gateway.typed_input import turn_typed_input
    from hermes_state_runtime import claim_session_input
    from hermes_state_wake import is_user_turn
    from tools.delegate_session_store import runtime_epoch
    db, _home = human_chat
    job = jobs.create_job("stand up", "in 30m", job_type="reminder",
                          delivery_target={"kind": "session", "session_id": "human-chat"})

    assert scheduler.run_one_job(job)  # no adapters, no owner sink in this process

    assert jobs.get_job(job["id"])["last_status"] == "ok"
    [row] = _queued(db)
    assert row["principal_id"] == "automation:" + KEY
    assert not is_user_turn(row["principal_id"], row["payload"])
    automation = row["payload"]["native_text_v1"]["automation"]
    assert automation["owner"] == "human-chat"
    assert automation["sender"]["sender_kind"] == "cron"
    assert automation["sender"]["sender_id"] == f"cron:{job['id']}"

    claimed = claim_session_input(db, epoch=runtime_epoch(db), session_id="human-chat")  # the owner's drain
    assert claimed["admission_id"] == row["admission_id"]
    event = restore_native(claimed["payload"])
    assert event.internal and event.text == "stand up"
    typed = turn_typed_input(event, event.source, event.text, None, None)
    assert typed["sender_kind"] == "cron" and typed["sender_id"] == f"cron:{job['id']}"
    assert [part["type"] for part in typed["parts"]] == ["system_event"]


def test_owner_pickup_schedules_the_signalled_conversation(human_chat):
    from gateway.session_automation_handoff import _collect, pick_up
    _db, home = human_chat
    job = jobs.create_job("stand up", "in 30m", job_type="reminder",
                          delivery_target={"kind": "session", "session_id": "human-chat"})
    assert scheduler.run_one_job(job)

    scheduled = []
    authority = Mock(sessions={"human-chat": object()}, profile_id="profile", runner=None,
                     _schedule=lambda ref: scheduled.append(ref.session_id))
    assert pick_up(authority, _collect(home)) == ["human-chat"]
    assert scheduled == ["human-chat"]
    assert _collect(home) == []  # one wake per signal


def test_conversation_the_route_moved_past_is_not_handed_off(human_chat):
    db, _home = human_chat
    db.create_session("newer-chat", source="discord", session_key=KEY, chat_id="42", chat_type="dm", user_id="u1")
    # The retained routing now names the newer conversation (a /new on the chat).
    db._write_sql("INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at) VALUES('', ?, ?, ?)",
                  (KEY, json.dumps({"session_id": "newer-chat"}), time.time()))
    job = {"id": "reminder", "job_type": "reminder", "execution_id": "fire-1", "delivery_mode": "internal_turn",
           "delivery_target": {"kind": "session", "session_id": "human-chat"}}
    error = scheduler._deliver_result(job, "stand up")
    assert error and "moved past" in error
    assert _queued(db) == []


def test_repeated_ownerless_fire_is_one_durable_admission(human_chat):
    db, _home = human_chat
    job = {"id": "reminder", "job_type": "reminder", "execution_id": "same-fire",
           "delivery_target": {"kind": "session", "session_id": "human-chat"}}
    assert scheduler._deliver_result(job, "stand up") is None
    [first] = _queued(db)
    assert scheduler._deliver_result(job, "stand up") is None
    [second] = _queued(db)
    assert first["admission_id"] == second["admission_id"]
    error = scheduler._deliver_result(job, "changed text")
    assert error and "admission_conflict" in error
    assert _queued(db)[0]["payload"]["text"] == "stand up"
