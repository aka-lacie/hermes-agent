"""A wake's follow-up final on a webhook session reaches the signed delivery it was bound to.

Regression for the dev2 -> dev3 parity review (P1): the final of a process/watch/delegate wake or
route wake is ledgered under its automation identity, not under a signed delivery id. Resolving
that identity against the signed rows alone found nothing, so every follow-up reply on a webhook
session failed (and its ledger retry could never repair it). The wake now carries the signed
delivery it borrowed its route from, and its final resolves through that exact delivery: on a
keyed chat that is the delivery the wake was admitted behind, never the newest or executing one.
"""
import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.webhook import WebhookAdapter, _WebhookDeliveryIdentity
from gateway.platforms.webhook_session_keys import keyed_chat_id
from tests.gateway.fixtures.webhook_route_authority import mount_authority

SECRET = "parity-followup-secret"
KEYED = {"prompt": "{text}", "deliver": "github_comment", "session_key": "{key}",
         "deliver_extra": {"repo": "{repo}", "pr_number": "{number}"}}
PER_DELIVERY = {"prompt": "{text}", "deliver": "github_comment",
                "deliver_extra": {"repo": "{repo}", "pr_number": "{number}"}}


def app_for(route):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"secret": SECRET, "routes": {"r": dict(route)}}))
    app = web.Application()
    mount_authority(app, adapter)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    comments = []

    async def comment(content, delivery):
        comments.append((content, delivery["deliver_extra"]["pr_number"]))
        return SendResult(success=True)
    adapter._deliver_github_comment = comment
    return adapter, app, comments


async def post(client, delivery, number):
    body = json.dumps({"text": "review", "key": "pr-thread", "repo": "owned/repo", "number": number}).encode()
    response = await client.post("/webhooks/r", data=body, headers={"X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()})
    assert response.status == 202, await response.text()


def _payload(authority, request_id):
    return json.loads(authority.db._read_one(
        "SELECT payload_json FROM session_admissions WHERE request_id=?", (request_id,))["payload_json"])


async def admit_wake(adapter, delivery, identity):
    """The production completion admission: an internal event on the route of signed *delivery*."""
    from gateway.platforms.event import MessageEvent
    from gateway.session_envelope import restore_native
    runner = adapter._message_handler.__self__
    authority = runner.session_authority
    origin = restore_native(_payload(authority, delivery), runner)
    entry = runner.session_store.lookup_by_session_key(runner.session_store._generate_session_key(origin.source))
    completion = MessageEvent(text="[process finished]", source=origin.source, internal=True,
                              metadata={"gateway_session_key": entry.session_key,
                                        "gateway_session_id": entry.session_id})
    await authority.admit_automation(adapter, completion, identity)
    return origin.source.chat_id


async def final_metadata(adapter, monkeypatch, identity):
    """What the final-response seam stamps for the wake's executing turn (the drain's restored event)."""
    from gateway.session_envelope import restore_native
    runner = adapter._message_handler.__self__
    event = restore_native(_payload(runner.session_authority, identity), runner)
    assert event.internal and event.message_id == identity
    captured = {}

    async def ledgered(self, event, session_key, text_content, metadata, *, reply_to, is_ephemeral_response=False):
        captured.update(metadata)
    with monkeypatch.context() as patched:
        patched.setattr(BasePlatformAdapter, "send_final_ledgered", ledgered)
        await adapter.send_final_ledgered(event, "key", "unused", None, reply_to=None)
    return captured


@pytest.mark.asyncio
async def test_keyed_wake_final_reaches_the_delivery_it_was_admitted_behind(monkeypatch):
    adapter, app, comments = app_for(KEYED)
    async with TestClient(TestServer(app)) as client:
        await post(client, "one", 7)
        chat = await admit_wake(adapter, "one", "process:first:1")
        await post(client, "two", 8)
        assert await admit_wake(adapter, "two", "process:second:1") == chat == keyed_chat_id(None, "r", "pr-thread")

        first = await final_metadata(adapter, monkeypatch, "process:first:1")
        second = await final_metadata(adapter, monkeypatch, "process:second:1")
        # Neither the newest delivery (8) nor the identity-less fallback answers the first wake.
        assert (await adapter.send(chat, "first follow-up", metadata=first)).success
        assert (await adapter.send(chat, "second follow-up", metadata=second)).success
        assert comments == [("first follow-up", "7"), ("second follow-up", "8")]


@pytest.mark.asyncio
async def test_keyed_wake_obligation_redelivery_resolves_the_same_delivery():
    adapter, app, comments = app_for(KEYED)
    async with TestClient(TestServer(app)) as client:
        await post(client, "one", 7)
        chat = await admit_wake(adapter, "one", "process:first:1")
        await post(client, "two", 8)
        from gateway.delivery_ledger import compute_obligation_id
        authority = adapter._message_handler.__self__.session_authority
        route = _payload(authority, "process:first:1")["native_text_v1"]["route"]
        # Boot redelivery of the ledgered final carries only its obligation id (run_startup).
        obligation = compute_obligation_id(route, "process:first:1", "first follow-up")
        metadata = {"_webhook_final_response": True, "_webhook_obligation_id": obligation}
        assert (await adapter.send(chat, "first follow-up", metadata=metadata)).success
        assert not (await adapter.send(chat, "changed text", metadata=metadata)).success
        assert comments == [("first follow-up", "7")]


@pytest.mark.asyncio
async def test_per_delivery_wake_final_reaches_its_delivery(monkeypatch):
    adapter, app, comments = app_for(PER_DELIVERY)
    async with TestClient(TestServer(app)) as client:
        await post(client, "solo", 7)
        chat = await admit_wake(adapter, "solo", "process:solo:1")
        assert chat == _WebhookDeliveryIdentity.from_parts(None, "r", "solo").session_chat_id
        metadata = await final_metadata(adapter, monkeypatch, "process:solo:1")
        assert (await adapter.send(chat, "follow-up", metadata=metadata)).success
        assert comments == [("follow-up", "7")]


@pytest.mark.asyncio
async def test_unknown_or_unbound_keyed_wake_final_never_picks_another_delivery():
    adapter, app, comments = app_for(KEYED)
    async with TestClient(TestServer(app)) as client:
        await post(client, "one", 7)
        chat = await admit_wake(adapter, "one", "process:first:1")
        await post(client, "two", 8)
        final = {"_webhook_final_response": True}
        assert not (await adapter.send(chat, "x", metadata={**final, "_webhook_delivery_id": "process:none:1"})).success
        # A wake admitted before the binding existed: a keyed chat has no unambiguous delivery.
        authority = adapter._message_handler.__self__.session_authority
        payload = _payload(authority, "process:first:1")
        assert payload["native_text_v1"]["automation"].pop("webhook_delivery_id") == "one"
        authority.db._execute_write(lambda conn: conn.execute(
            "UPDATE session_admissions SET payload_json=? WHERE request_id=?",
            (json.dumps(payload), "process:first:1")))
        assert not (await adapter.send(chat, "x", metadata={**final, "_webhook_delivery_id": "process:first:1"})).success
        assert comments == []


@pytest.mark.asyncio
async def test_local_wake_of_a_detached_webhook_conversation_resolves_its_bound_delivery():
    """A conversation its keyed chat moved past admits wakes as ``local_automation_v1``; the bound
    delivery is recorded beside the chat it answers on (``local_delivery_binding``)."""
    from gateway.platforms.webhook_delivery import local_delivery_binding
    from hermes_state_runtime import admit_session_input, list_session_admissions
    adapter, app, comments = app_for(KEYED)
    async with TestClient(TestServer(app)) as client:
        await post(client, "one", 7)
        await post(client, "two", 8)
        authority = adapter._message_handler.__self__.session_authority
        chat = keyed_chat_id(None, "r", "pr-thread")
        signed = authority.db._read_one("SELECT target_session_id FROM session_admissions WHERE request_id='one'")
        owner = signed["target_session_id"]
        prior = [row["payload"] for row in list_session_admissions(authority.db, session_id=owner, pending_only=False)]
        binding = local_delivery_binding(prior[:1], chat)
        assert binding == {"webhook_chat_id": chat, "webhook_delivery_id": "one"}
        native = _payload(authority, "one")["native_text_v1"]
        descriptor = {"identity": "watch:detached:1", "owner": owner, "route": native["route"],
                      "target": owner, **binding}
        admit_session_input(authority.db, epoch=authority.epoch, principal_id="automation:" + native["route"],
                            session_id=owner, request_id="watch:detached:1",
                            payload={"text": "[watch]", "local_automation_v1": descriptor})
        metadata = {"_webhook_final_response": True, "_webhook_delivery_id": "watch:detached:1"}
        assert (await adapter.send(chat, "detached follow-up", metadata=metadata)).success
        assert comments == [("detached follow-up", "7")]
