"""Webhook worker pins survive CLI/dashboard edits and reject incompatible routes."""

import argparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import webhook as wh
from hermes_cli.subcommands.webhook import build_webhook_parser
from hermes_cli.web_routers.ops import router


@pytest.fixture(autouse=True)
def subscription_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(wh, "_is_webhook_enabled", lambda: True)
    monkeypatch.setattr(wh, "_get_webhook_base_url", lambda: "http://localhost:8644")


@pytest.fixture
def parser():
    parser = argparse.ArgumentParser()
    build_webhook_parser(parser.add_subparsers(), cmd_webhook=wh.webhook_command)
    return parser


def subscribe(parser, *flags):
    args = parser.parse_args(["webhook", "subscribe", "inbox", *flags])
    args.func(args)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def test_cli_round_trip_preserves_omitted_pins_and_clears_each_independently(parser, capsys):
    subscribe(parser, "--model", " claude-opus-5-5 ", "--provider", " cliproxy-claude ",
              "--deliver", "discord", "--delivery-mode", "internal_turn")
    route = wh._load_subscriptions()["inbox"]
    assert (route["model"], route["provider"]) == ("claude-opus-5-5", "cliproxy-claude")
    assert route["delivery_mode"] == "internal_turn"

    subscribe(parser, "--description", "updated")
    route = wh._load_subscriptions()["inbox"]
    assert (route["model"], route["provider"]) == ("claude-opus-5-5", "cliproxy-claude")
    subscribe(parser, "--model", "")
    route = wh._load_subscriptions()["inbox"]
    assert "model" not in route
    assert route["provider"] == "cliproxy-claude"
    wh._cmd_list(argparse.Namespace())
    assert "Worker provider: cliproxy-claude" in capsys.readouterr().out
    subscribe(parser, "--provider", "  ")
    route = wh._load_subscriptions()["inbox"]
    assert "model" not in route and "provider" not in route


@pytest.mark.parametrize("field", ["model", "provider"])
def test_cli_single_field_and_verbatim_provider_names(parser, field):
    subscribe(parser, f"--{field}", "  My-Exact/Identifier  ")
    route = wh._load_subscriptions()["inbox"]
    assert route[field] == "My-Exact/Identifier"
    assert {"model", "provider"}.intersection(route) == {field}


@pytest.mark.parametrize("field", ["model", "provider"])
@pytest.mark.parametrize("bad", [12, False, [], {}])
def test_cli_rejects_invalid_persisted_pin_without_overwriting(parser, field, bad, capsys):
    original = {"secret": "test-secret", field: bad}
    wh._mutate_subscriptions(lambda subs: subs.update(inbox=original))
    subscribe(parser)
    assert "Error:" in capsys.readouterr().out
    assert wh._load_subscriptions()["inbox"] == original


@pytest.mark.parametrize("field", ["model", "provider"])
@pytest.mark.parametrize("mode", ["deliver-only", "cron-job"])
def test_cli_rejects_pins_on_non_worker_routes(parser, field, mode, capsys):
    flags = ["--deliver", "discord", "--deliver-only"] if mode == "deliver-only" else ["--cron-job", "job"]
    subscribe(parser, *flags, f"--{field}", "pin")
    assert "Error:" in capsys.readouterr().out
    assert "inbox" not in wh._load_subscriptions()


def test_cli_transition_to_deliver_only_requires_clearing_old_pin(parser, capsys):
    subscribe(parser, "--model", "pinned")
    original = wh._load_subscriptions()["inbox"]
    subscribe(parser, "--deliver", "discord", "--deliver-only")
    assert "Error:" in capsys.readouterr().out
    assert wh._load_subscriptions()["inbox"] == original
    subscribe(parser, "--deliver", "discord", "--deliver-only", "--model", "")
    route = wh._load_subscriptions()["inbox"]
    assert route["deliver_only"] is True
    assert "model" not in route


def test_api_round_trip_preserves_omitted_pins_and_exposes_summary(client):
    response = client.post("/api/webhooks", json={
        "name": "inbox", "model": " claude-opus-5-5 ", "provider": " cliproxy-claude ",
        "deliver": "discord", "delivery_mode": "internal_turn",
    })
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "claude-opus-5-5"
    assert response.json()["provider"] == "cliproxy-claude"
    assert wh._load_subscriptions()["inbox"]["delivery_mode"] == "internal_turn"
    assert client.post("/api/webhooks", json={"name": "inbox"}).status_code == 200
    route = wh._load_subscriptions()["inbox"]
    assert (route["model"], route["provider"]) == ("claude-opus-5-5", "cliproxy-claude")
    summary = client.get("/api/webhooks").json()["subscriptions"][0]
    assert (summary["model"], summary["provider"]) == (route["model"], route["provider"])
    assert "secret" not in summary


@pytest.mark.parametrize("field", ["model", "provider"])
@pytest.mark.parametrize("cleared", [None, "", "   "])
def test_api_explicit_clear_is_fieldwise(client, field, cleared):
    assert client.post("/api/webhooks", json={
        "name": "inbox", "model": "pinned-model", "provider": "Pinned-Provider",
    }).status_code == 200
    response = client.post("/api/webhooks", json={"name": "inbox", field: cleared})
    assert response.status_code == 200, response.text
    route = wh._load_subscriptions()["inbox"]
    other = "provider" if field == "model" else "model"
    assert field not in route
    assert route[other] == {"model": "pinned-model", "provider": "Pinned-Provider"}[other]
    assert response.json()[field] is None


@pytest.mark.parametrize("field", ["model", "provider"])
@pytest.mark.parametrize("bad", [123, False, [], {}])
def test_api_rejects_non_string_values_before_mutation(client, field, bad):
    response = client.post("/api/webhooks", json={"name": "inbox", field: bad})
    assert response.status_code == 422, response.text
    assert "inbox" not in wh._load_subscriptions()


@pytest.mark.parametrize("field", ["model", "provider"])
def test_api_rejects_pins_on_deliver_only_routes(client, field):
    response = client.post("/api/webhooks", json={
        "name": "inbox", "deliver": "discord", field: "pin",
        "deliver_only": True,
    })
    assert response.status_code == 400, response.text
    assert "inbox" not in wh._load_subscriptions()


def test_api_old_pin_cannot_silently_survive_switch_to_deliver_only(client):
    assert client.post("/api/webhooks", json={
        "name": "inbox", "provider": "exact-provider",
    }).status_code == 200
    original = wh._load_subscriptions()["inbox"]
    response = client.post("/api/webhooks", json={
        "name": "inbox", "deliver": "discord", "deliver_only": True,
    })
    assert response.status_code == 400
    assert wh._load_subscriptions()["inbox"] == original
    response = client.post("/api/webhooks", json={
        "name": "inbox", "deliver": "discord", "deliver_only": True, "provider": None,
    })
    assert response.status_code == 200, response.text
    assert "provider" not in wh._load_subscriptions()["inbox"]


@pytest.mark.parametrize("field", ["model", "provider"])
def test_api_existing_cron_route_rejects_pin_without_erasing_job(client, field):
    original = {"cron_job": "existing-job", "secret": "test-secret"}
    wh._mutate_subscriptions(lambda subs: subs.update(inbox=original))
    response = client.post("/api/webhooks", json={"name": "inbox", field: "pin"})
    assert response.status_code == 400
    assert wh._load_subscriptions()["inbox"] == original


def test_api_legacy_route_has_no_implicit_pin(client):
    response = client.post("/api/webhooks", json={"name": "inbox"})
    assert response.status_code == 200, response.text
    route = wh._load_subscriptions()["inbox"]
    assert "model" not in route and "provider" not in route
    assert response.json()["model"] is None and response.json()["provider"] is None
