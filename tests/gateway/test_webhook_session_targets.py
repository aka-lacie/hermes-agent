"""Per-route ``delivery_target`` (BRIEF3 contract) and the typed webhook input row.

``sessions.delivery_target`` is written with exactly the shared shape; ``kind=session`` output goes to
Worker A's dispatcher only through ``webhook_session_targets.dispatch_session_target`` (stubbed here),
``kind=platform`` egresses through the route's own sender.
"""
import hashlib
import hmac
import json
import sys
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_session_keys import keyed_chat_id
from gateway.platforms.webhook_session_targets import route_delivery_target
from tests.gateway.fixtures.webhook_route_authority import mount_authority

SECRET = "owned-target-secret"


def test_contract_shapes():
    assert route_delivery_target({"delivery_target": {"kind": "session", "session_id": "main"}}) == {
        "kind": "session", "session_id": "main", "attach": False}
    assert route_delivery_target({"delivery_target": {"kind": "session", "session_id": "main", "attach": True}}) == {
        "kind": "session", "session_id": "main", "attach": True}
    assert route_delivery_target({"delivery_target": {"kind": "platform", "platform": "discord", "chat_id": 7,
                                                      "thread_id": 9, "token": "never-stored"}}) == {
        "kind": "platform", "platform": "discord", "chat_id": "7", "thread_id": "9"}
    assert route_delivery_target({"delivery_target": {"kind": "none"}}) == {"kind": "none"}
    assert route_delivery_target({}) is None


async def _deliver(route, monkeypatch, *, dispatcher=None):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"secret": SECRET, "routes": {"pr": route}}))
    app = web.Application()
    mount_authority(app, adapter)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    if dispatcher is not None:
        monkeypatch.setitem(sys.modules, "gateway.delivery_targets",
                            types.SimpleNamespace(resolve_delivery_target=dispatcher))
    async with TestClient(TestServer(app)) as client:
        body = json.dumps({"text": "hello", "repo": "o/r", "number": 1}).encode()
        response = await client.post("/webhooks/pr", data=body, headers={
            "X-GitHub-Delivery": "t1",
            "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()})
        assert response.status == 202, await response.text()
        db = adapter._message_handler.__self__.session_authority.db
        sid = db._read_one("SELECT target_session_id FROM session_admissions")["target_session_id"]
        stored = db._read_one("SELECT delivery_target FROM sessions WHERE id=?", (sid,))["delivery_target"]
        calls = []

        async def cross(platform, content, delivery):
            calls.append(("platform", platform, content, delivery))
            return SendResult(success=True)

        async def comment(content, delivery):
            calls.append(("route", content, delivery))
            return SendResult(success=True)
        adapter._deliver_cross_platform, adapter._deliver_github_comment = cross, comment
        monkeypatch.setattr("gateway.platforms.webhook._is_known_platform", lambda name: True)
        adapter.gateway_runner = adapter._message_handler.__self__
        result = await adapter.send(keyed_chat_id(None, "pr", "o/r#1"), "final answer")
    return json.loads(stored) if stored else None, sid, result, calls


ROUTE = {"prompt": "{text}", "session_key": "{repo}#{number}", "deliver": "github_comment",
         "deliver_extra": {"repo": "{repo}", "pr_number": "{number}"}}


@pytest.mark.asyncio
async def test_session_target_is_stored_and_dispatched_through_the_seam(monkeypatch):
    seen = []

    def resolve_delivery_target(row):
        async def deliver(text):
            seen.append((row, text))
        return deliver
    route = {**ROUTE, "delivery_target": {"kind": "session", "session_id": "main-1", "attach": True}}
    stored, sid, result, calls = await _deliver(route, monkeypatch, dispatcher=resolve_delivery_target)
    assert stored == {"kind": "session", "session_id": "main-1", "attach": True}
    assert result.success and calls == []  # not also posted by the route's own deliver
    assert seen == [({"id": sid, "delivery_target": stored}, "final answer")]


@pytest.mark.asyncio
async def test_session_target_without_a_dispatcher_keeps_the_output_in_the_session(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.delivery_targets", None)  # import fails: not installed
    route = {**ROUTE, "delivery_target": {"kind": "session", "session_id": "main-1"}}
    stored, _sid, result, calls = await _deliver(route, monkeypatch)
    assert stored == {"kind": "session", "session_id": "main-1", "attach": False}
    assert result.success and calls == []


@pytest.mark.asyncio
async def test_platform_target_egresses_to_that_chat(monkeypatch):
    route = {**ROUTE, "delivery_target": {"kind": "platform", "platform": "discord", "chat_id": "123",
                                          "thread_id": "456"}}
    stored, _sid, result, calls = await _deliver(route, monkeypatch)
    assert stored == {"kind": "platform", "platform": "discord", "chat_id": "123", "thread_id": "456"}
    assert result.success
    assert calls == [("platform", "discord", "final answer",
                      {"deliver": "discord", "deliver_extra": {"chat_id": "123", "thread_id": "456"}, "profile": None})]


@pytest.mark.asyncio
async def test_no_target_keeps_the_route_deliver(monkeypatch):
    stored, _sid, result, calls = await _deliver(dict(ROUTE), monkeypatch)
    assert stored is None and result.success
    assert calls == [("route", "final answer", {"deliver": "github_comment",
                                                "deliver_extra": {"repo": "o/r", "pr_number": "1"}})]


def test_a_webhook_delivery_is_a_typed_webhook_event_never_human_input():
    from gateway.typed_input import turn_typed_input
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"secret": SECRET}))
    source = adapter.build_source(chat_id="webhook:v2:x", user_id="webhook:github-pr", chat_type="webhook")
    assert source.platform == Platform.WEBHOOK
    event = types.SimpleNamespace(message_id="delivery-7", metadata={}, internal=False)
    typed = turn_typed_input(event, source, "Review PR 7", 12.5, None)
    assert typed["sender_kind"] == "webhook" and typed["sender_id"] == "webhook:github-pr"
    assert typed["source_event_id"] == "delivery-7" and typed["part_kind"] == "webhook"
    assert typed["parts"] == [{"type": "system_event", "v": 1, "kind": "webhook", "text": "Review PR 7",
                               "meta": {"route": "github-pr", "delivery_id": "delivery-7"}}]
