"""Webhook delivery snapshots select worker prose; receiving conversations stay main."""

from dataclasses import replace
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_project", [False, True])
async def test_webhook_snapshot_to_real_prompt_and_main_receiver(tmp_path, monkeypatch, skip_project):
    from agent.secret_scope import set_multiplex_active
    from agent.system_prompt import build_system_prompt
    from gateway.authz_mixin import GatewayAuthorizationMixin
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.run import _profile_runtime_scope
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from hermes_state import SessionDB
    from run_agent import AIAgent

    homes = [tmp_path / "a", tmp_path / "b"]
    for index, home in enumerate(homes):
        (home / "instructions").mkdir(parents=True)
        (home / "instructions/main.md").write_text(f"MAIN_POLICY_{index}")
        (home / "instructions/subagent.md").write_text(f"WORKER_POLICY_{index}")
        (home / "config.yaml").write_text(f"compression:\n  enabled: false\nterminal:\n  cwd: {home}\n")
    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    db = SessionDB(db_path=homes[0] / "shared.db")
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    runner = GatewayAuthorizationMixin()
    runner.adapters = {Platform.WEBHOOK: adapter}
    runner._profile_adapters = {}
    runner._primary_profile_name = "a"
    runner.config = GatewayConfig()
    runner._prefill_messages = []
    runner._service_tier = None
    runner._session_db = db
    runner._refresh_fallback_model = lambda: None
    adapter.gateway_runner = runner
    route = {"model": "test/model", "runtime": {
        "api_key": "test-key", "base_url": "http://127.0.0.1:1/v1", "provider": "custom"}}

    set_multiplex_active(True)
    try:
        with patch("agent.process_bootstrap.OpenAI"):
            for index in (0, 1, 0):
                for mode in ("internal_turn", "direct"):
                    seen = []

                    async def consume(event):
                        # Changing the route AFTER dispatch must not change this worker's role.
                        config["delivery_mode"] = "direct" if mode == "internal_turn" else "internal_turn"
                        ctx = TurnContext(
                            source=event.source, AIAgent=AIAgent, user_config={},
                            enabled_toolsets=["file"], disabled_toolsets=[],
                            session_id=f"worker-{index}-{mode}", session_key=event.source.chat_id,
                        )
                        turn = TurnRunner(runner, ctx)
                        worker = turn._build_fresh_agent(route, "webhook", None, 3, None, {}, skip_project)
                        try:
                            prompt = build_system_prompt(worker)
                            internal = mode == "internal_turn"
                            assert (f"WORKER_POLICY_{index}" in prompt) == internal
                            assert (f"MAIN_POLICY_{index}" in prompt) == (not internal and not skip_project)
                            assert f"WORKER_POLICY_{1-index}" not in prompt and f"MAIN_POLICY_{1-index}" not in prompt
                            assert worker.platform == "webhook"
                            assert not hasattr(worker, "_delegate_fork_history")
                            seen.append(worker.instruction_role)

                            # Even a notification receiver sharing the same chat id must not
                            # inherit worker posture from the webhook delivery snapshot.
                            ctx.source = replace(event.source, platform=Platform.DISCORD)
                            ctx.internal_notification = {"kind": "webhook_completion", "source": "route"}
                            ctx.session_id += "-receiver"
                            receiver = turn._build_fresh_agent(route, "discord", None, 3, None, {}, False)
                            try:
                                receiver_prompt = build_system_prompt(receiver)
                                assert f"MAIN_POLICY_{index}" in receiver_prompt
                                assert "WORKER_POLICY_" not in receiver_prompt
                                assert receiver.platform == "discord"
                            finally:
                                receiver.close()
                        finally:
                            worker.close()

                    adapter.handle_message = consume
                    config = {"delivery_mode": mode, "deliver": "discord"}
                    with _profile_runtime_scope(homes[index], prepared_secret_scope={}):
                        # The incoming payload deliberately requests the opposite posture.
                        await adapter._spawn_agent_run(
                            {"delivery_mode": "direct" if mode == "internal_turn" else "internal_turn"},
                            "Inspect event", f"delivery-{index}-{mode}", 100.0,
                            route_config=config, route_name="route", profile=homes[index].name, event_type="test",
                        )
                    assert seen == ["subagent" if mode == "internal_turn" else "main"]
    finally:
        set_multiplex_active(False)
        db.close()
