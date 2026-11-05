"""Keyed webhook sessions (plan §4.5 / §6 Q6): deliveries a route keys alike resume ONE session.

Real durable admission (``mount_authority``); only execution is held, so every assertion reads the
committed FIFO and the sessions rows. The live-wire serialization and history sharing are covered in
``test_webhook_session_keys_e2e.py``.
"""
import base64
import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _WebhookDeliveryIdentity
from gateway.platforms.webhook_session_keys import (
    NO_KEY, SCRIPT_FIELD, conversation_key, is_keyed_chat_id, keyed_chat_id, resolve_delivery_key,
    route_template_key, split_script_key, strip_reserved, validate_route_session_options)
from tests.gateway.fixtures.webhook_route_authority import mount_authority

SECRET = "owned-key-secret"


def _route(**extra):
    return {"prompt": "{text}", "deliver": "log", "session_key": "{repo}#{number}", **extra}


def _app(routes):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"secret": SECRET, "routes": routes}))
    app = web.Application()
    mount_authority(app, adapter)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    finalized = []

    async def spy(event, authority, receipt):
        finalized.append(event.source.chat_id)
    adapter._finalize_delivery = spy
    return adapter, app, finalized


async def _post(client, route, payload, delivery):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Delivery": delivery,
               "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}
    response = await client.post(f"/webhooks/{route}", data=body, headers=headers)
    assert response.status == 202, await response.text()


def _targets(adapter):
    db = adapter._message_handler.__self__.session_authority.db
    return {row["request_id"]: row["target_session_id"]
            for row in db._read_all("SELECT request_id, target_session_id FROM session_admissions")}


def _session(adapter, sid):
    db = adapter._message_handler.__self__.session_authority.db
    return dict(db._read_one("SELECT lineage_kind, conversation_key, route, delivery_target, ended_at "
                             "FROM sessions WHERE id=?", (sid,)))


# ── keys ─────────────────────────────────────────────────────────────────────


def test_keyed_chat_id_is_stable_route_readable_and_distinct_from_per_delivery_ids():
    chat = keyed_chat_id("default", "pr", "owned/repo#7")
    assert chat == keyed_chat_id(None, "pr", "owned/repo#7")
    assert chat != keyed_chat_id("default", "pr", "owned/repo#8") != keyed_chat_id("default", "other", "owned/repo#7")
    assert is_keyed_chat_id(chat) and _WebhookDeliveryIdentity.route_from_chat_id(chat) == "pr"
    per_delivery = _WebhookDeliveryIdentity.from_parts(None, "pr", "d1").session_chat_id
    assert not is_keyed_chat_id(per_delivery) and not is_keyed_chat_id("webhook:pr:d1")
    assert "owned/repo" not in chat  # provider identifiers stay out of routing keys
    # dev2 (feat/webhook-session-keys) spelling, so a migrated keyed routing entry keeps resolving.
    token = chat[len("webhook:v2:"):]
    decoded = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert decoded == ["default", "pr", hashlib.sha256(b"owned/repo#7").hexdigest()[:32], "session"]


def test_script_field_is_reserved_for_the_gate():
    assert strip_reserved({"a": 1, SCRIPT_FIELD: "forged"}) == {"a": 1}
    assert split_script_key({"a": 1}) == ({"a": 1}, NO_KEY)
    assert split_script_key({"a": 1, SCRIPT_FIELD: None}) == ({"a": 1}, None)


def test_route_template_needs_every_placeholder():
    route = {"session_key": ["{repository.full_name}#{pull_request.number}", "{repository.full_name}#{issue.number}"]}
    assert route_template_key(route, {"repository": {"full_name": "o/r"}, "issue": {"number": 4}}) == "o/r#4"
    assert route_template_key(route, {"repository": {"full_name": "o/r"}}) is None
    assert route_template_key(route, {"repository": {"full_name": "o/r"}, "pull_request": {"number": {}}}) is None


@pytest.mark.asyncio
async def test_resolution_order_script_then_template_then_plugin(monkeypatch):
    calls = []

    async def hook(name, **kwargs):
        calls.append(kwargs)
        return [None, {"bad": 1}, {"key": "from-plugin"}]
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: True)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    common = dict(route_name="pr", event_type="push", delivery_id="d", profile=None, headers={"X-A": "1"})
    route = _route()
    assert await resolve_delivery_key(route_config=route, payload={"repo": "o/r", "number": 1},
                                      script_answer="gate-key", **common) == "gate-key"
    # An explicit null from the gate is a per-delivery session even when the template would key it.
    assert await resolve_delivery_key(route_config=route, payload={"repo": "o/r", "number": 1},
                                      script_answer=None, **common) is None
    assert await resolve_delivery_key(route_config=route, payload={"repo": "o/r", "number": 1}, **common) == "o/r#1"
    assert calls == []
    assert await resolve_delivery_key(route_config=route, payload={"repo": "o/r"}, **common) == "from-plugin"
    assert calls[0]["headers"] == {"X-A": "1"} and calls[0]["route_name"] == "pr"


@pytest.mark.asyncio
async def test_raising_plugin_never_gates_acceptance(monkeypatch):
    async def boom(name, **kwargs):
        raise RuntimeError("plugin down")
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: True)
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", boom)
    assert await resolve_delivery_key(route_name="pr", route_config={}, payload={}, event_type="e",
                                      delivery_id="d", profile=None) is None


@pytest.mark.asyncio
async def test_register_webhook_session_key_is_route_scoped():
    from hermes_cli.plugins_webhooks import WebhookContextMethods
    registered = {}

    class Ctx(WebhookContextMethods):
        def register_hook(self, name, callback):
            registered[name] = callback
            return "handle"

    seen = []

    async def resolver(payload, *, headers):
        seen.append((payload, headers))
        return f"pr-{payload['n']}"
    assert Ctx().register_webhook_session_key("github-pr", resolver) == "handle"
    hook = registered["resolve_session_key"]
    assert await hook(route_name="other", payload={"n": 1}, headers={}) is None
    assert await hook(route_name="github-pr", payload={"n": 1}, headers={"h": "v"}, profile=None,
                      event_type="e", delivery_id="d", route={}) == "pr-1"
    assert seen == [({"n": 1}, {"h": "v"})]


@pytest.mark.parametrize("route", [
    {"session_key": "no-placeholder"},
    {"session_key": []},
    {"session_key": "{a}", "deliver_only": True, "deliver": "telegram"},
    {"fallback_providers": [{"provider": "x"}]},
    {"fallback_providers": "openrouter"},
    {"model": ""},
    {"delivery_target": {"kind": "session"}},
    {"delivery_target": {"kind": "platform", "platform": "discord"}},
    {"delivery_target": {"kind": "session", "session_id": "s", "attach": "yes"}},
    {"delivery_target": {"kind": "email"}},
])
def test_invalid_route_session_options_fail_fast(route):
    with pytest.raises(ValueError):
        validate_route_session_options("r", route)


def test_valid_route_session_options():
    validate_route_session_options("r", _route(model="m", provider="p", fallback_providers=[],
                                               delivery_target={"kind": "session", "session_id": "s", "attach": True}))
    validate_route_session_options("r", {"fallback_providers": [{"provider": "p", "model": "m"}]})


# ── admission ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_key_resumes_one_session_other_keys_and_unkeyed_do_not():
    adapter, app, finalized = _app({"pr": _route()})
    async with TestClient(TestServer(app)) as client:
        await _post(client, "pr", {"text": "first", "repo": "owned/repo", "number": 1}, "d1")
        await _post(client, "pr", {"text": "second", "repo": "owned/repo", "number": 1}, "d2")
        await _post(client, "pr", {"text": "other pr", "repo": "owned/repo", "number": 2}, "d3")
        await _post(client, "pr", {"text": "no key"}, "d4")
        await _post(client, "pr", {"text": "no key again"}, "d5")
        targets = _targets(adapter)
        assert targets["d1"] == targets["d2"]
        assert len({targets["d1"], targets["d3"], targets["d4"], targets["d5"]}) == 4
        keyed = _session(adapter, targets["d1"])
        assert keyed["conversation_key"] == conversation_key("pr", "owned/repo#1") == "webhook:pr:owned/repo#1"
        assert keyed["lineage_kind"] == "webhook" and keyed["ended_at"] is None
        assert _session(adapter, targets["d3"])["conversation_key"] == "webhook:pr:owned/repo#2"
        unkeyed = _session(adapter, targets["d4"])
        assert unkeyed["conversation_key"] is None and unkeyed["lineage_kind"] == "webhook"
        # Only per-delivery sessions are handed to the one-shot finalizer.
        assert not any(is_keyed_chat_id(chat) for chat in finalized) and len(finalized) == 2


@pytest.mark.asyncio
async def test_keyed_session_survives_a_restart(tmp_path):
    adapter, app, _ = _app({"pr": _route()})
    async with TestClient(TestServer(app)) as client:
        await _post(client, "pr", {"text": "before", "repo": "owned/repo", "number": 9}, "before")
        first = _targets(adapter)["before"]
    # A fresh adapter, runner, SessionStore and authority over the same state.db (gateway restart).
    replacement, app2, _ = _app({"pr": _route()})
    async with TestClient(TestServer(app2)) as client:
        await _post(client, "pr", {"text": "after", "repo": "owned/repo", "number": 9}, "after")
        assert _targets(replacement)["after"] == first
        await _post(client, "pr", {"text": "fresh", "repo": "owned/repo", "number": 10}, "fresh")
        assert _targets(replacement)["fresh"] != first


@pytest.mark.asyncio
async def test_gate_script_names_the_key_and_a_forged_inbound_field_is_ignored(monkeypatch):
    adapter, app, _ = _app({"gate": {"prompt": "{text}", "deliver": "log", "script": "gate.py"}})
    seen = []

    def gate(script, payload):
        seen.append(dict(payload))
        return True, {**payload, SCRIPT_FIELD: f"thread-{payload['thread']}" if payload.get("thread") else None}
    monkeypatch.setattr(adapter._route_processor, "run_route_script", gate)
    async with TestClient(TestServer(app)) as client:
        await _post(client, "gate", {"text": "a", "thread": "t1", SCRIPT_FIELD: "forged"}, "g1")
        await _post(client, "gate", {"text": "b", "thread": "t1"}, "g2")
        await _post(client, "gate", {"text": "c"}, "g3")
        targets = _targets(adapter)
    assert SCRIPT_FIELD not in seen[0]  # the gate never sees a caller-supplied key
    assert targets["g1"] == targets["g2"] != targets["g3"]
    assert _session(adapter, targets["g1"])["conversation_key"] == "webhook:gate:thread-t1"
    db = adapter._message_handler.__self__.session_authority.db
    payloads = [json.loads(r["payload_json"]) for r in db._read_all("SELECT payload_json FROM session_admissions")]
    assert all(SCRIPT_FIELD not in json.dumps(p) for p in payloads)


@pytest.mark.asyncio
async def test_keyed_reply_leg_follows_the_executing_delivery(monkeypatch):
    route = _route(deliver="github_comment", deliver_extra={"repo": "{repo}", "pr_number": "{number}"})
    adapter, app, _ = _app({"pr": route})
    async with TestClient(TestServer(app)) as client:
        await _post(client, "pr", {"text": "one", "repo": "owned/repo", "number": 3}, "r1")
        await _post(client, "pr", {"text": "two", "repo": "owned/repo", "number": 3}, "r2")
        calls = []

        async def sink(content, delivery):
            from gateway.platforms.base import SendResult
            calls.append(delivery)
            return SendResult(success=True)
        adapter._deliver_github_comment = sink
        chat = keyed_chat_id(None, "pr", "owned/repo#3")
        assert (await adapter.send(chat, "reply")).success
        assert calls == [{"deliver": "github_comment", "deliver_extra": {"repo": "owned/repo", "pr_number": "3"}}]


@pytest.mark.asyncio
async def test_keyed_retry_is_a_duplicate_and_never_finalizes():
    adapter, app, finalized = _app({"pr": _route()})
    async with TestClient(TestServer(app)) as client:
        await _post(client, "pr", {"text": "once", "repo": "owned/repo", "number": 5}, "same")
        adapter._seen_deliveries.clear()
        body = json.dumps({"text": "once", "repo": "owned/repo", "number": 5}).encode()
        headers = {"X-GitHub-Delivery": "same",
                   "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}
        retry = await client.post("/webhooks/pr", data=body, headers=headers)
        assert retry.status == 200 and (await retry.json())["status"] == "duplicate"
        sid = _targets(adapter)["same"]
        assert len(_targets(adapter)) == 1 and _session(adapter, sid)["ended_at"] is None
        assert finalized == []


# ── reference resolvers (github-pr, ibn-inbox-events) ────────────────────────


def test_github_pr_key_matches_the_live_gate_for_gate_and_raw_payloads():
    from gateway.platforms.webhook_session_key_builtins import github_pr_session_key
    gate = {"event_type": "github.pull_request", "repository": "Aka-Lacie/IBN-ERP", "number": 90}
    raw = {"repository": {"full_name": "Aka-Lacie/IBN-ERP"}, "pull_request": {"number": 90}}
    comment = {"repository": {"full_name": "aka-lacie/ibn-erp"}, "issue": {"number": 90, "pull_request": {}}}
    assert github_pr_session_key(gate) == github_pr_session_key(raw) == github_pr_session_key(comment) == (
        "github-pr|v1|aka-lacie/ibn-erp#90")
    assert github_pr_session_key({"repository": {"full_name": "o/r"}, "issue": {"number": 3}}) is None
    assert github_pr_session_key({"repository": "not a repo", "number": 1}) is None
    assert github_pr_session_key({"repository": "o/r", "number": 0}) is None


def test_outlook_key_joins_mailbox_copies_of_one_graph_conversation():
    from gateway.platforms.webhook_session_key_builtins import normalized_conversation_id, outlook_session_key
    token = bytes(range(16))

    def graph_id(wrapper):
        return base64.urlsafe_b64encode(wrapper + bytes((0, 16, 0)) + token).decode().rstrip("=")
    first, second = graph_id(b"mailbox-one-guid"), graph_id(b"other-mailbox-guid!!")
    assert first != second and normalized_conversation_id(first) == normalized_conversation_id(second) == (
        "graph-token:" + token.hex())
    payload = {"inbox": {"mailbox": " Sales@IBN.example "}, "outlook": {"conversation_id": first}}
    assert outlook_session_key(payload) == f"outlook|v1|sales@ibn.example|graph-token:{token.hex()}"
    assert outlook_session_key({**payload, "outlook": {"conversation_id": second}}) == outlook_session_key(payload)
    other_mailbox = {"inbox": {"mailbox": "ops@ibn.example"}, "outlook": {"conversation_id": first}}
    assert outlook_session_key(other_mailbox) != outlook_session_key(payload)
    assert normalized_conversation_id("AAQkADI= odd") == "exact:aaqkadi= odd"
    assert outlook_session_key({"inbox": {"mailbox": "a@b"}, "outlook": {}}) is None
