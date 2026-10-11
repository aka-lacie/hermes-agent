"""The model-facing cron bridge preserves reminder delivery into the durable job."""

import json


def test_registry_creates_reminder_with_internal_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import cronjob_tools
    from tools.registry import registry
    from cron.jobs import get_job

    result = json.loads(registry.dispatch("cronjob_manage", {
        "action": "create", "prompt": "Check whether the customer replied",
        "schedule": "30m", "job_type": "reminder", "deliver": "discord:123",
        "delivery_mode": "internal_turn",
    }))
    assert result.get("success"), result
    job = get_job(result["job"]["job_id"])
    assert job["job_type"] == "reminder"
    assert job["delivery_mode"] == "internal_turn"
    assert job["deliver"] == "discord:123"
    assert job["prompt"] == "Check whether the customer replied"
