"""Cron reminder controls cross real CLI, REST and authored-definition boundaries."""
import argparse
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cron import jobs
from cron.job_definition import merge_job_definition
from hermes_cli.subcommands.cron import build_cron_parser


def parser():
    result = argparse.ArgumentParser()
    build_cron_parser(result.add_subparsers(dest="command"), cmd_cron=lambda args: None)
    return result


def test_cli_create_and_edit_roundtrip_controls(monkeypatch, capsys):
    from hermes_cli import cron
    monkeypatch.setattr(cron, "_warn_if_gateway_not_running", lambda: None)
    target = {"kind": "session", "selector": "discord:dm:42", "attach": True}
    args = parser().parse_args(["cron", "create", "every 1h", " remember ", "--job-type", "reminder",
                               "--delivery-mode", "internal_turn", "--delivery-target", json.dumps(target)])
    assert cron.cron_create(args) == 0
    job = jobs.list_jobs(True)[0]
    assert job["prompt"] == " remember "
    assert job["job_type"] == "reminder" and job["delivery_target"]["attach"] is True
    assert "internal_turn" in capsys.readouterr().out
    edited = parser().parse_args(["cron", "edit", job["id"], "--delivery-mode", "direct",
                                 "--delivery-target", '{"kind":"none"}', "--no-attach-to-session"])
    assert cron.cron_edit(edited) == 0
    stored = jobs.get_job(job["id"])
    assert stored["delivery_mode"] == "direct" and stored["delivery_target"] == {"kind": "none"}
    assert stored["attach_to_session"] is False
    assert dict(cron._job_rows(stored))["Mode"] == "reminder"


def test_cli_rejects_malformed_target_before_mutation():
    with pytest.raises(SystemExit):
        parser().parse_args(["cron", "create", "in 30m", "remember", "--delivery-target", '{"kind":"session"}'])
    assert jobs.load_jobs() == []


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    from hermes_cli import web_server_cron as service
    from hermes_cli.web_routers import cron as router
    monkeypatch.setattr(service, "_cron_profile_home", lambda profile: ("default", tmp_path))
    monkeypatch.setattr(service, "_cron_profile_dicts", lambda: [{"name": "default", "path": str(tmp_path)}])
    monkeypatch.setattr(service, "_notify_cron_provider_for_profile", lambda profile: None)
    app = FastAPI()
    app.include_router(router.router)
    with TestClient(app) as client:
        yield client


def test_dashboard_create_update_and_persist(dashboard):
    response = dashboard.post("/api/cron/jobs", json={
        "prompt": "  exact reminder\n", "schedule": "every 1h", "job_type": "reminder",
        "delivery_mode": "internal_turn", "delivery_target": {"kind": "session", "selector": "title:Bot Chat"},
        "attach_to_session": True, "timezone": "Asia/Tokyo", "catch_up": False,
    })
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["job_type"] == "reminder" and job["prompt"] == "  exact reminder\n"
    assert job["delivery_target"]["wake_policy"] == "immediate"
    assert job["timezone"] == "Asia/Tokyo" and job["catch_up"] is False
    changed = dashboard.put(f"/api/cron/jobs/{job['id']}", json={"updates": {
        "delivery_target": {"kind": "session", "selector": "discord:dm:7", "attach": True},
        "delivery_mode": "internal_turn",
    }})
    assert changed.status_code == 200, changed.text
    stored = dashboard.get(f"/api/cron/jobs/{job['id']}").json()
    assert stored["delivery_target"]["selector"] == "discord:dm:7"
    assert stored["delivery_target"]["attach"] is True


@pytest.mark.parametrize("fields", [
    {"job_type": "unknown"}, {"delivery_mode": "unknown"},
    {"job_type": "reminder", "skills": ["not-a-reminder"]},
    {"delivery_mode": "internal_turn", "delivery_target": {"kind": "none"}},
])
def test_dashboard_rejects_invalid_modes_and_targets(dashboard, fields):
    result = dashboard.post("/api/cron/jobs", json={"prompt": "remember", "schedule": "in 30m", **fields})
    assert result.status_code in {400, 422}, result.text


@pytest.mark.parametrize("attach", [None, False, True])
def test_synthesized_attachment_create_and_update(attach):
    fields = {} if attach is None else {"attach_to_session": attach}
    job = jobs.create_job("remember", "every 1h", job_type="reminder",
                          origin={"session_key": "discord:dm:42"}, **fields)
    assert job["delivery_target"]["attach"] is (attach is True)
    updated = jobs.update_job(job["id"], {"attach_to_session": True})
    assert updated["delivery_target"]["attach"] is True
    updated = jobs.update_job(job["id"], {"attach_to_session": False})
    assert updated["delivery_target"]["attach"] is False


def test_authored_import_preserves_execution_and_delivery_without_resetting_state():
    authored = {"prompt": "remember", "job_type": "reminder", "delivery_mode": "internal_turn",
                "delivery_target": {"kind": "session", "selector": "title:Bot Chat", "attach": True},
                "schedule": {"kind": "interval", "minutes": 60}, "timezone": "Asia/Tokyo"}
    local = {**authored, "id": "job", "repeat": {"times": None, "completed": 3},
             "last_status": "ok", "next_run_at": "2099-01-01T00:00:00Z"}
    merged = merge_job_definition(local, authored)
    assert merged["job_type"] == "reminder" and merged["delivery_mode"] == "internal_turn"
    assert merged["delivery_target"] == authored["delivery_target"]
    assert merged["repeat"]["completed"] == 3 and merged["last_status"] == "ok"
    assert merged["next_run_at"] == local["next_run_at"]


def test_local_tool_session_attachment_remains_editable(monkeypatch):
    from tools import cronjob_tools
    monkeypatch.setattr(cronjob_tools, "_origin_from_env", lambda schedule: None)
    created = json.loads(cronjob_tools.registry.dispatch("cronjob_manage", {
        "action": "create", "prompt": "remember", "schedule": "every 1h", "job_type": "reminder",
        "attach_to_session": True,
    }, session_id="local-session"))
    assert created["success"], created
    target = created["job"]["delivery_target"]
    assert target["selector"] == "session:local-session" and target["attach"] is True
    changed = jobs.update_job(created["job_id"], {"attach_to_session": False})
    assert changed["delivery_target"]["attach"] is False
