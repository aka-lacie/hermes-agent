"""Literal reminders use canonical session delivery, never a detached cron agent."""
import json
from unittest.mock import Mock

import pytest

from cron import jobs, scheduler, session_delivery
from gateway import delivery_targets as dt


ORIGIN = {"platform": "discord", "chat_id": "42", "chat_type": "dm"}


def test_reminder_defaults_and_literal_output(monkeypatch):
    literal = "  take a break\n[SILENT]\n"
    job = jobs.create_job(literal, "in 30m", job_type="reminder", origin=ORIGIN)
    assert job["delivery_mode"] == "internal_turn"
    target = job["delivery_target"]
    assert target["kind"] == "session"
    assert target["selector"] == "discord:dm:42"
    assert target["attach"] is False
    assert target["wake_policy"] == "immediate"
    monkeypatch.setattr(scheduler, "_open_cron_session_db", Mock(side_effect=AssertionError("no agent DB")))
    monkeypatch.setattr("cron.scheduler_authority.run_canonical_job", Mock(side_effect=AssertionError("no inference")))
    success, doc, text, error = scheduler.run_job(job)
    assert success and error is None
    assert text == literal
    assert literal in doc
    assert jobs.get_job(job["id"])["prompt"] == literal


@pytest.mark.parametrize("kwargs", [
    {"prompt": ""}, {"script": "check.py"}, {"skills": ["example"]},
    {"monitor_url": "https://example.com"}, {"no_agent": True},
])
def test_reminder_invalid_payload_rejected(kwargs):
    fields = {"prompt": "remember", "schedule": "in 30m", "job_type": "reminder", "origin": ORIGIN}
    fields.update(kwargs)
    with pytest.raises(ValueError):
        jobs.create_job(**fields)


def test_update_invariants_and_legacy_script_alias():
    job = jobs.create_job("remember", "every 1h", origin=ORIGIN)
    updated = jobs.update_job(job["id"], {"job_type": "reminder", "delivery_mode": "internal_turn"})
    assert updated["job_type"] == "reminder"
    assert updated["delivery_target"]["kind"] == "session"
    with pytest.raises(ValueError):
        jobs.update_job(job["id"], {"script": "check.py"})
    with pytest.raises(ValueError):
        jobs.update_job(job["id"], {"prompt": ""})
    updated = jobs.update_job(job["id"], {"no_agent": True, "script": "check.py"})
    assert updated["job_type"] == "script" and updated["no_agent"]
    updated = jobs.update_job(job["id"], {"no_agent": False})
    assert updated["job_type"] == "agent" and not updated["no_agent"]


@pytest.mark.parametrize("deliver", ["local", "all", "origin,discord:42"])
def test_internal_rejects_non_conversation_targets(deliver):
    with pytest.raises(ValueError):
        jobs.create_job("remember", "in 30m", job_type="reminder", deliver=deliver)


def test_legacy_internal_selector_uses_origin_identity():
    target = session_delivery.legacy_session_target({
        "delivery_mode": "internal_turn", "deliver": "origin",
        "origin": {"platform": "slack", "chat_id": "G42", "scope_id": "T1", "chat_type": "group", "user_id": "U1"},
    })
    assert target["selector"] == "slack:group:T1:G42:U1"


def test_delivery_mode_update_releases_only_inferred_target():
    job = jobs.create_job("remember", "every 1h", job_type="reminder", origin=ORIGIN)
    direct = jobs.update_job(job["id"], {"delivery_mode": "direct", "deliver": "local"})
    assert direct["delivery_mode"] == "direct"
    assert direct.get("delivery_target") is None
    explicit = jobs.create_job("remember", "every 1h", job_type="reminder", origin=ORIGIN,
                               delivery_target={"kind": "session", "selector": "title:Other"})
    changed = jobs.update_job(explicit["id"], {"deliver": "discord:77"})
    assert changed["delivery_target"]["selector"] == "title:Other"


@pytest.mark.parametrize("target", [None, {"kind": "none"}, {"kind": "session", "bogus": "x"}])
def test_unresolved_internal_delivery_fails_closed(monkeypatch, target):
    monkeypatch.setattr("cron.scheduler_delivery._resolve_delivery_targets", lambda *a, **k: [])
    job = {"id": "legacy", "delivery_mode": "internal_turn", "deliver": "origin", "delivery_target": target}
    handled, error = session_delivery.route_declared(job, "remember", Mock(side_effect=AssertionError("no platform fallback")))
    assert handled and error


def test_explicit_target_and_failure_suppression(monkeypatch):
    job = jobs.create_job("remember", "in 30m", job_type="reminder",
                          delivery_target={"kind": "session", "selector": "title:Bot Chat"})
    assert job["delivery_target"]["wake_policy"] == "immediate"
    assert job["delivery_target"]["attach"] is False
    job["failure_deliver"] = "local"
    monkeypatch.setattr(session_delivery, "deliver_to_session", Mock(side_effect=AssertionError("suppressed")))
    assert scheduler._deliver_result(job, "failed", for_failure=True) is None


def test_reminder_delivers_typed_event_via_canonical_dispatch(tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "sessions.db")
    captured = []
    db.create_session("destination", source="cli")

    def capture(db, row, target, event, *, wake_policy, request_id):
        captured.append((event, target))
        return dt.DeliveryReceipt("session", True, "test")

    dt.register_sink("reminder-test", lambda db, row, target: True, capture)
    try:
        job = {"id": "reminder", "execution_id": "run-1", "job_type": "reminder"}
        target = dt.session_target("destination", attach=False, wake_policy="immediate")
        assert session_delivery.deliver_to_session(job, "literal text", target, db=db) is None
        event, actual_target = captured[0]
        assert event.text == "literal text" and event.sender_kind == "cron"
        assert event.source_event_id == "run-1"
        assert actual_target == target
    finally:
        dt.unregister_sink("reminder-test")
        db.close()


def test_registry_forwards_reminder_controls(monkeypatch):
    from tools import cronjob_tools
    monkeypatch.setattr(cronjob_tools, "_gateway_liveness_notice", lambda **kw: {})
    result = json.loads(cronjob_tools.registry.dispatch("cronjob_manage", {
        "action": "create", "prompt": "remember", "schedule": "in 30m", "job_type": "reminder",
        "delivery_mode": "internal_turn", "delivery_target": {"kind": "session", "selector": "title:Bot Chat"},
    }))
    assert result["success"], result
    assert result["job"]["job_type"] == "reminder"
    assert result["job"]["delivery_target"]["selector"] == "title:Bot Chat"
    changed = json.loads(cronjob_tools.registry.dispatch("cronjob_manage", {
        "action": "update", "job_id": result["job_id"], "delivery_target": {"kind": "session", "selector": "discord"},
    }))
    assert changed["success"], changed
    assert changed["job"]["delivery_target"]["selector"] == "discord"


def test_reminder_full_run_delivers_without_external_worker(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from cron.executions import latest_executions
    db = SessionDB(db_path=tmp_path / "sink.db")
    db.create_session("destination", source="cli")
    captured = []

    def capture(db, row, target, event, **kw):
        captured.append(event.text)
        return dt.DeliveryReceipt("session", True, "test", row["id"])

    dt.register_sink("reminder-full-test", lambda db, row, target: True, capture)
    monkeypatch.setattr("hermes_state_registry.acquire", lambda: db)
    monkeypatch.setattr("hermes_state_registry.release", lambda db: None)
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", Mock(side_effect=AssertionError("literal reminder stays local")))
    try:
        job = jobs.create_job("[SILENT]", "in 30m", job_type="reminder", deliver="local",
                              delivery_target={"kind": "session", "session_id": "destination"})
        assert scheduler.run_one_job(job)
        assert captured == ["[SILENT]"]
        assert jobs.get_job(job["id"])["last_status"] == "ok"
        assert latest_executions([job["id"]])[job["id"]]["delivery_outcome"] == "delivered"
    finally:
        dt.unregister_sink("reminder-full-test")
        db.close()


def test_script_worker_hands_internal_delivery_to_live_gateway(monkeypatch):
    job = {"id": "script", "job_type": "script", "execution_id": "own-attempt", "deliver": "local",
           "delivery_mode": "internal_turn", "delivery_target": {"kind": "session", "session_id": "destination"}}
    monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", "own-attempt")
    queued = Mock(return_value=None)
    monkeypatch.setattr("cron.delivery_queue.enqueue_and_wait", queued)
    monkeypatch.setattr("cron.delivery_queue.get_status", lambda execution: {"status": "delivered"})
    monkeypatch.setattr(session_delivery, "deliver_to_session", Mock(side_effect=AssertionError("no local worker sink")))
    assert scheduler._deliver_result(job, "script stdout") is None
    queued.assert_called_once_with("own-attempt", job, "script stdout", for_failure=False)
