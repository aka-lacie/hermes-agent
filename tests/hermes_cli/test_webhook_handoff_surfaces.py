"""CLI/dashboard webhook delivery-mode and worker-pin write/read parity."""
import argparse

import pytest

from hermes_cli.subcommands.webhook import build_webhook_parser
from hermes_cli.webhook import _load_subscriptions, webhook_command
from tests.hermes_cli.test_dashboard_admin_endpoints import _client


def args(*extra):
    parser = argparse.ArgumentParser()
    build_webhook_parser(parser.add_subparsers(), cmd_webhook=webhook_command)
    return parser.parse_args(["webhook", "subscribe", "handoff", *extra])


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr("hermes_cli.webhook._is_webhook_enabled", lambda: True)
    monkeypatch.setattr("hermes_cli.webhook._get_webhook_base_url", lambda: "http://localhost:8644")


def test_cli_mode_and_worker_pins_create_preserve_and_clear(capsys):
    assert webhook_command(args("--deliver", "discord", "--deliver-chat-id", "7", "--delivery-mode", "internal_turn",
                                "--model", "worker-m", "--provider", "worker-p")) is None
    stored = _load_subscriptions()["handoff"]
    assert (stored["delivery_mode"], stored["model"], stored["provider"]) == ("internal_turn", "worker-m", "worker-p")
    assert "wake agent" in capsys.readouterr().out
    webhook_command(args("--deliver", "discord", "--deliver-chat-id", "7"))
    assert _load_subscriptions()["handoff"]["model"] == "worker-m"
    assert _load_subscriptions()["handoff"]["delivery_mode"] == "internal_turn"
    webhook_command(args("--deliver", "discord", "--model", "", "--provider", "", "--delivery-mode", "direct"))
    stored = _load_subscriptions()["handoff"]
    assert "model" not in stored and "provider" not in stored and stored["delivery_mode"] == "direct"


@pytest.mark.parametrize("extra", [("--deliver", "log"), ("--deliver", "github_comment"),
                                    ("--deliver", "discord", "--deliver-only")])
def test_cli_rejects_internal_turn_without_an_agent_conversation(extra):
    assert webhook_command(args("--delivery-mode", "internal_turn", *extra)) == 1
    assert _load_subscriptions() == {}


def test_dashboard_mode_pins_roundtrip_and_invalid_update_leaves_original_unchanged():
    client, _ = _client()
    response = client.post("/api/webhooks", json={"name": "handoff", "deliver": "discord", "deliver_chat_id": "7",
        "delivery_mode": "internal_turn", "model": "worker-m", "provider": "worker-p"})
    assert response.status_code == 200, response.text
    assert response.json()["delivery_mode"] == "internal_turn"
    assert response.json()["model"] == "worker-m" and response.json()["provider"] == "worker-p"
    listed = client.get("/api/webhooks").json()["subscriptions"][0]
    assert listed["delivery_mode"] == "internal_turn" and listed["model"] == "worker-m"
    before = _load_subscriptions()["handoff"]
    refused = client.post("/api/webhooks", json={"name": "handoff", "deliver": "log", "delivery_mode": "internal_turn"})
    assert refused.status_code == 400 and _load_subscriptions()["handoff"] == before
    updated = client.post("/api/webhooks", json={"name": "handoff", "deliver": "discord", "deliver_chat_id": "7"})
    assert updated.status_code == 200, updated.text
    assert _load_subscriptions()["handoff"]["model"] == "worker-m"
    assert _load_subscriptions()["handoff"]["delivery_mode"] == "internal_turn"
    cleared = client.post("/api/webhooks", json={"name": "handoff", "deliver": "discord", "model": "", "provider": None})
    assert cleared.status_code == 200, cleared.text
    assert "model" not in _load_subscriptions()["handoff"] and "provider" not in _load_subscriptions()["handoff"]
