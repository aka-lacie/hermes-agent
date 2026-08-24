"""First-class reminder jobs wake the main agent without a detached worker."""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import hermes_constants
    import cron.jobs
    import cron.scheduler

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    importlib.reload(cron.scheduler)
    return home


def test_create_reminder_defaults_to_internal_turn(hermes_env):
    from cron.jobs import create_job

    job = create_job(
        prompt="Check whether the customer replied",
        schedule="30m",
        job_type="reminder",
        deliver="origin",
    )

    assert job["job_type"] == "reminder"
    assert job["delivery_mode"] == "internal_turn"
    assert job["no_agent"] is False
    assert job["provider_snapshot"] is None
    assert job["model_snapshot"] is None


def test_reminder_without_explicit_target_defaults_to_origin(hermes_env):
    from cron.jobs import create_job

    job = create_job(
        prompt="Check whether the customer replied",
        schedule="30m",
        job_type="reminder",
    )

    assert job["deliver"] == "origin"
    assert job["delivery_mode"] == "internal_turn"


def test_reminder_rejects_script_and_skills(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="cannot use script or skills"):
        create_job(
            prompt="Check the account",
            schedule="30m",
            job_type="reminder",
            skills=["account-review"],
        )


@pytest.mark.parametrize("deliver", ["local", "bot-chat", "discord,bot-chat:yuri"])
def test_internal_turn_rejects_non_conversation_delivery_lanes(
    hermes_env, deliver
):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="internal_turn"):
        create_job(
            prompt="Check the account",
            schedule="30m",
            deliver=deliver,
            delivery_mode="internal_turn",
        )


def test_legacy_internal_turn_with_unresolved_target_reports_delivery_error(
    hermes_env
):
    from cron.scheduler import _deliver_result

    error = _deliver_result(
        {
            "id": "legacy-reminder",
            "delivery_mode": "internal_turn",
            "deliver": "local",
        },
        "Check the account",
    )

    assert error is not None
    assert "resolvable live gateway conversation" in error


def test_run_reminder_returns_literal_text_without_agent(hermes_env):
    from cron.scheduler import run_job

    job = {
        "id": "reminder-1",
        "name": "customer follow-up",
        "job_type": "reminder",
        "prompt": "Check whether the customer replied",
    }

    with patch("cron.scheduler._run_job_script") as script:
        success, document, response, error = run_job(job)

    assert success is True
    assert error is None
    assert response == "Check whether the customer replied"
    assert "no detached agent" in document
    script.assert_not_called()


def test_internal_delivery_enqueues_main_agent_turn(hermes_env):
    from cron.scheduler import _deliver_result

    job = {
        "id": "reminder-1",
        "name": "customer follow-up",
        "job_type": "reminder",
        "delivery_mode": "internal_turn",
        "deliver": "origin",
        "origin": {
            "platform": "discord",
            "chat_id": "dm-123",
            "user_id": "freya",
            "chat_type": "dm",
        },
    }

    with patch(
        "gateway.internal_turns.InternalTurnService.enqueue",
        return_value=True,
    ) as enqueue, patch(
        "tools.send_message_tool._send_to_platform"
    ) as direct_send, patch(
        "hermes_cli.profiles.get_active_profile_name",
        return_value="yuri",
    ):
        error = _deliver_result(job, "Check whether the customer replied")

    assert error is None
    direct_send.assert_not_called()
    enqueue.assert_called_once()
    kwargs = enqueue.call_args.kwargs
    assert kwargs["platform"] == "discord"
    assert kwargs["chat_id"] == "dm-123"
    assert kwargs["profile"] == "yuri"
    assert kwargs["kind"] == "reminder"
