"""A pinned cron job may declare its own ``fallback_providers`` (plan §4.3, Phase 0 item 2).

``cron.scheduler._job_fallback_chain`` passes the job's declared chain through the shared
``scoped_fallback_chain(declared=...)`` rule: a pinned job with a declared chain fails over onto
THAT chain (at credential resolution and mid-run), never the profile's global chain; ``[]`` still
disables fallback; a pinned job without one keeps failing loudly (test_cron_pinned_job_fallback.py).
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import classify_api_error
from cron import scheduler
from cron.scheduler import _CronJobConfig, _job_fallback_chain, _resolve_job_runtime
from hermes_cli.auth import AuthError
from tests.cron.test_cron_pinned_job_fallback import _CHAIN, _job, _run, _runtime

DECLARED = [{"provider": "anthropic", "model": "claude-sonnet-5"}]
PINNED = {"provider": "openai-codex", "model": "gpt-6.1-sol"}


def test_declared_chain_beats_the_pin_rule_and_never_borrows_the_global_chain():
    cfg = {"fallback_providers": list(_CHAIN)}
    assert _job_fallback_chain(_job(**PINNED, fallback_providers=DECLARED), cfg) == DECLARED
    assert _job_fallback_chain(_job(**PINNED), cfg) is None
    assert _job_fallback_chain(_job(**PINNED, fallback_providers=[]), cfg) is None
    assert _job_fallback_chain(_job(fallback_providers=DECLARED), cfg) == DECLARED  # unpinned: own chain
    assert _job_fallback_chain(_job(fallback_providers=[]), cfg) is None
    assert _job_fallback_chain(_job(), cfg) == _CHAIN


def test_pinned_job_with_declared_chain_resolves_onto_it_when_the_primary_fails():
    jc = _CronJobConfig(cfg={"fallback_providers": list(_CHAIN)}, model="gpt-6.1-sol", model_cfg={},
                        cron_default_provider="")
    requested = []

    def resolve(**kwargs):
        requested.append(kwargs.get("requested"))
        if kwargs.get("requested") == "anthropic":
            return _runtime("anthropic")
        raise AuthError("No Codex credentials stored")
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolve):
        runtime, model = _resolve_job_runtime(_job(**PINNED, fallback_providers=DECLARED), "pin-job", jc)
    assert (runtime["provider"], model) == ("anthropic", "claude-sonnet-5")
    assert "openrouter" not in requested


def test_pinned_job_with_declared_chain_fails_over_mid_run_on_a_fallback_worthy_error(tmp_path):
    """The chain run_job hands AIAgent is what the provider ladder walks: a content-policy block on
    the pinned primary (a "fall back now" class) moves the real agent onto the declared entry."""
    success, error, requested, agent_kwargs = _run(tmp_path, _job(**PINNED, fallback_providers=DECLARED))
    assert (success, error) == (True, None)
    assert agent_kwargs["fallback_model"] == DECLARED and "openrouter" not in requested
    from tests.agent.test_provider_fallback import _make_agent, _mock_client
    agent = _make_agent(fallback_model=agent_kwargs["fallback_model"])
    agent.provider, agent.model = PINNED["provider"], PINNED["model"]
    blocked = classify_api_error(RuntimeError("This content was flagged for possible cybersecurity risk"),
                                 provider=agent.provider, model=agent.model)
    assert blocked.should_fallback and not blocked.retryable
    with patch("agent.auxiliary_client.resolve_provider_client",
               return_value=(_mock_client(base_url="https://api.anthropic.example/v1"), "claude-sonnet-5")):
        assert agent._try_activate_fallback(blocked.reason) is True
    assert agent.model == "claude-sonnet-5" and agent.provider == "anthropic"


def test_pinned_job_with_declared_chain_failure_notice_mentions_backups(monkeypatch):
    monkeypatch.setattr(scheduler, "load_config", lambda: {"fallback_providers": list(_CHAIN)})
    assert scheduler._fallback_chain_phrase(_job(**PINNED, fallback_providers=DECLARED)) == (
        "No backup provider succeeded either.")


@pytest.mark.parametrize(("job", "route"), [
    (_job(**PINNED, fallback_providers=DECLARED), {**PINNED, "fallback_providers": DECLARED}),
    (_job(**PINNED), PINNED),
    (_job(), None),
])
def test_fire_session_records_the_credential_free_route(job, route):
    from cron.scheduler_routes import cron_job_route, stamp_cron_session_route
    assert cron_job_route(dict(job, fallback_providers=[{**e, "api_key": "sk-secret"} for e in DECLARED])
                          if "fallback_providers" in job else job) == route
    db = MagicMock()
    with patch("hermes_state_session_meta.stamp_session_meta") as stamp:
        stamp_cron_session_route(db, "cron-session", job)
    stamp.assert_called_once_with(db, "cron-session", lineage_kind="cron", route=route)


def test_fire_session_route_lands_in_the_sessions_row(tmp_path):
    from hermes_state import SessionDB
    from cron.scheduler_routes import stamp_cron_session_route
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("cron-session", source="cron")
        stamp_cron_session_route(db, "cron-session", _job(**PINNED, fallback_providers=DECLARED))
        row = db._read_one("SELECT lineage_kind, route FROM sessions WHERE id='cron-session'")
        assert row["lineage_kind"] == "cron"
        assert row["route"] == ('{"fallback_providers":[{"model":"claude-sonnet-5","provider":"anthropic"}],'
                                '"model":"gpt-6.1-sol","provider":"openai-codex"}')
        stamp_cron_session_route(None, "cron-session", _job())  # no store: nothing to do, never raises
    finally:
        db.close()
