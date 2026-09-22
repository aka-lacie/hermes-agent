"""Internal-delivery cron workers select operating prose without changing cron identity."""

from unittest.mock import patch

import pytest


@pytest.mark.parametrize("delivery_mode", ["direct", "internal_turn"])
@pytest.mark.parametrize("with_workdir", [False, True])
def test_cron_construction_selects_worker_role(tmp_path, monkeypatch, delivery_mode, with_workdir):
    from agent.system_prompt import build_system_prompt
    from cron.scheduler import _construct_cron_agent, _CronAgentSetup
    from hermes_state import SessionDB
    from run_agent import AIAgent

    (tmp_path / "instructions").mkdir()
    (tmp_path / "instructions/main.md").write_text("MAIN_ONLY_POLICY")
    (tmp_path / "instructions/subagent.md").write_text("WORKER_ONLY_POLICY")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cfg = {"compression": {"enabled": False}, "terminal": {"cwd": str(tmp_path)}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda *a, **kw: cfg)
    setup = _CronAgentSetup(
        model="test/model", max_iterations=3,
        runtime={"api_key": "test-key", "base_url": "http://127.0.0.1:1/v1", "provider": "custom"},
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    with patch("agent.process_bootstrap.OpenAI"):
        agent = _construct_cron_agent(
            AIAgent, {"delivery_mode": delivery_mode, "enabled_toolsets": ["file"]}, cfg, setup,
            workdir=str(tmp_path) if with_workdir else None, session_id="cron-worker", session_db=db,
        )
        try:
            prompt = build_system_prompt(agent)
            worker = delivery_mode == "internal_turn"
            assert ("WORKER_ONLY_POLICY" in prompt) == worker
            assert ("MAIN_ONLY_POLICY" in prompt) == (with_workdir and not worker)
            assert agent.platform == "cron"
            assert agent.enabled_toolsets == ["file"]
            assert agent.skip_background_review is True
            assert not hasattr(agent, "_delegate_fork_history")
        finally:
            agent.close()
            db.close()
