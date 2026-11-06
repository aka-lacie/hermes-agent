"""Agentless reminders must not create detached cron agent-session admissions."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("job", [
    {"job_type": "reminder", "prompt": "literal"},
    {"job_type": "script", "script": "task.py", "no_agent": True},
])
def test_no_detached_session_for_agentless_job(job, monkeypatch):
    from gateway.session_cron import _create
    from hermes_state_runtime import RuntimeStoreError
    monkeypatch.setattr("cron.jobs.get_job", lambda _id: job)
    db = Mock()
    db.get_session.return_value = None
    authority = SimpleNamespace(profile_id="test", db=db)
    actor = SimpleNamespace(subject="cron")
    with pytest.raises(RuntimeStoreError, match="not_found"):
        _create(authority, actor, {"job_id": "j", "request_id": "fire1", "extra_prompt": None})
    db.create_session.assert_not_called()
