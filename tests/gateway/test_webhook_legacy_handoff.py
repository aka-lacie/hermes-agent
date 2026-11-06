"""Legacy webhook handoffs must never egress raw worker text, even on failed metadata/dispatch."""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import delivery_targets as dt
from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_session_keys import keyed_chat_id
from gateway.platforms.webhook_session_targets import route_delivery_target
from tests.gateway.fixtures.webhook_route_authority import mount_authority

SECRET = "handoff-owned-secret"
ROUTE = {"prompt": "{text}", "deliver": "discord", "delivery_mode": "internal_turn",
         "session_key": "{key}", "deliver_extra": {"chat_id": "{chat}"}}
FINAL = {"_webhook_final_response": True}


def test_legacy_translation_is_immediate_unattached_and_explicit_attach_wins():
    route = {"delivery_mode": "internal_turn", "deliver": "discord", "deliver_extra": {"chat_id": "7"}}
    assert route_delivery_target(route) == {"kind": "session", "selector": "discord:dm:7",
                                            "attach": False, "wake_policy": "immediate"}
    route["delivery_target"] = {"kind": "session", "selector": "bot-chat", "attach": True}
    assert route_delivery_target(route) == {"kind": "session", "selector": "bot-chat",
                                            "attach": True, "wake_policy": "immediate"}
    assert route_delivery_target({"delivery_mode": "internal_turn", "deliver": "telegram"})["selector"] == "home:telegram"
    assert route_delivery_target({"delivery_mode": "internal_turn", "deliver": "telegram",
                                  "deliver_extra": {"chat_id": "7", "message_thread_id": 9}})["selector"] == "telegram:dm:7:9"


@pytest.mark.parametrize("extra", [{"deliver": "log"}, {"deliver": "github_comment"},
    {"deliver_extra": {"chat_id": "{missing}"}}, {"deliver_extra": {"chat_id": []}},
    {"deliver_only": True}, {"cron_job": "id"}, {"delivery_target": {"kind": "garbage"}},
    {"delivery_target": {"kind": "platform", "platform": "discord", "chat_id": "7"}}])
def test_bad_internal_routes_raise_never_become_direct(extra):
    with pytest.raises(ValueError):
        route_delivery_target({"delivery_mode": "internal_turn", "deliver": "discord", **extra})


def app_for(route=None):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"secret": SECRET, "routes": {"r": route or dict(ROUTE)}}))
    app = web.Application()
    mount_authority(app, adapter)
    async def enable_egress(app):
        adapter.gateway_runner = adapter._message_handler.__self__
    app.on_startup.append(enable_egress)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return adapter, app


async def post(client, delivery="one", chat="7"):
    body = json.dumps({"text": "work", "chat": chat, "key": "same"}).encode()
    response = await client.post("/webhooks/r", data=body, headers={"X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()})
    assert response.status == 202, await response.text()


def chat_session(db, chat):
    sid = f"main-{chat}"
    db.create_session(sid, source="discord")
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET session_key=? WHERE id=?",
                                               (f"agent:main:discord:dm:{chat}", sid)))
    return sid


@pytest.mark.asyncio
async def test_actual_adapter_send_suppresses_notices_and_delivers_only_final(monkeypatch):
    adapter, app = app_for()
    async with TestClient(TestServer(app)) as client:
        await post(client)
        db = adapter._message_handler.__self__.session_authority.db
        sid = chat_session(db, "7")
        raw = AsyncMock(return_value=SendResult(success=True))
        adapter._deliver_cross_platform = raw
        seen = []
        def sink(db, row, target, event, **kw):
            seen.append((row["id"], target, event, kw))
            return dt.DeliveryReceipt("session", True, "capture", row["id"])
        dt.register_sink("legacy_capture", lambda *a: True, sink)
        try:
            chat = keyed_chat_id(None, "r", "same")
            assert (await adapter.send(chat, "progress notice")).success
            assert (await adapter.send(chat, "interim", metadata={"_interim_send": True})).success
            assert seen == [] and raw.await_count == 0
            assert (await adapter.send(chat, "final handoff", metadata=FINAL)).success
            assert len(seen) == 1 and seen[0][0] == sid
            assert seen[0][1]["attach"] is False and seen[0][3]["wake_policy"] == "immediate"
            assert seen[0][2].kind == "webhook" and seen[0][2].text == "final handoff"
            worker = db._read_one("SELECT id,attached_session_id FROM sessions WHERE source='webhook'")
            assert worker["attached_session_id"] is None and seen[0][2].meta["session_id"] == worker["id"]
            assert raw.await_count == 0
            native = json.loads(db._read_one("SELECT payload_json FROM session_admissions")["payload_json"])["native_text_v1"]
            assert native["event"]["reply_expected"] is False
            from gateway.session_envelope import restore_native
            from gateway.session_execution import GatewaySessionAgentMixin
            from types import SimpleNamespace
            runner = adapter._message_handler.__self__
            runner._prefill_messages, runner._service_tier = None, None
            turn = GatewaySessionAgentMixin.__new__(GatewaySessionAgentMixin)
            turn._runner = runner
            turn._ctx = SimpleNamespace(source=restore_native({"text": "work", "native_text_v1": native}, runner).source,
                user_config={}, enabled_toolsets=[], disabled_toolsets=None, session_id=worker["id"],
                session_key="key", AIAgent=lambda **kw: kw)
            built = turn._build_fresh_agent({"model": "m", "runtime": {}}, "webhook", None, 1, None, {}, False)
            assert built["instruction_role"] == "subagent"
            assert (await adapter.send(chat, "Nothing to report\n[SILENT]", metadata=FINAL)).success
            assert len(seen) == 1
        finally:
            dt.unregister_sink("legacy_capture")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_stamp", "malformed_stamp", "read_error", "dispatch_error", "refused", "unresolved"])
async def test_actual_send_fails_closed_without_raw_platform_fallthrough(monkeypatch, failure):
    adapter, app = app_for()
    async with TestClient(TestServer(app)) as client:
        await post(client)
        db = adapter._message_handler.__self__.session_authority.db
        if failure != "unresolved":
            chat_session(db, "7")
        raw = AsyncMock(return_value=SendResult(success=True))
        adapter._deliver_cross_platform = raw
        if failure in {"missing_stamp", "malformed_stamp"}:
            value = None if failure == "missing_stamp" else "not-json"
            db._execute_write(lambda conn: conn.execute("UPDATE sessions SET delivery_target=? WHERE source='webhook'", (value,)))
        if failure == "read_error":
            monkeypatch.setattr("gateway.platforms.webhook_delivery.retained_session_target", lambda *a: (_ for _ in ()).throw(RuntimeError("read failed")))
        if failure in {"dispatch_error", "refused"}:
            def dispatch(*a, **kw):
                if failure == "dispatch_error":
                    raise RuntimeError("dispatch failed")
                return dt.DeliveryReceipt("session", False, "denied")
            monkeypatch.setattr("gateway.platforms.webhook_session_targets.deliver_to_session_target", dispatch)
        result = await adapter.send(keyed_chat_id(None, "r", "same"), "PRIVATE WORKER OUTPUT", metadata=FINAL)
        assert not result.success and raw.await_count == 0


@pytest.mark.asyncio
async def test_keyed_delivery_freezes_each_rendered_target_not_last_or_first_destination(monkeypatch):
    adapter, app = app_for()
    async with TestClient(TestServer(app)) as client:
        await post(client, "one", "7")
        await post(client, "two", "8")
        db = adapter._message_handler.__self__.session_authority.db
        rows = db._read_all("SELECT admission_id,payload_json FROM session_admissions ORDER BY seq")
        assert len(rows) == 2
        for row, expected in zip(rows, ("discord:dm:7", "discord:dm:8")):
            target = json.loads(row["payload_json"])["native_text_v1"]["webhook_delivery"]["delivery_target"]
            assert target["selector"] == expected
        from gateway.platforms.webhook_delivery import retained_session_target, reply_context
        token = reply_context.set({"_webhook_delivery_id": "one"})
        try:
            assert retained_session_target(adapter, keyed_chat_id(None, "r", "same"))["delivery_target"]["selector"] == "discord:dm:7"
        finally:
            reply_context.reset(token)
        chat_session(db, "7")
        chat_session(db, "8")
        seen = []
        def sink(db, row, target, event, **kw):
            seen.append((row["id"], kw["request_id"]))
            return dt.DeliveryReceipt("session", True, "capture", row["id"])
        dt.register_sink("keyed_recovery_capture", lambda *a: True, sink)
        raw = AsyncMock(return_value=SendResult(success=True))
        adapter._deliver_cross_platform = raw
        try:
            chat = keyed_chat_id(None, "r", "same")
            assert (await adapter.send(chat, "final one", metadata={**FINAL, "_webhook_delivery_id": "one"})).success
            native = json.loads(rows[0]["payload_json"])["native_text_v1"]
            from gateway.delivery_ledger import compute_obligation_id
            obligation = compute_obligation_id(native["route"], "one", "final one")
            runner = adapter._message_handler.__self__
            monkeypatch.setattr(runner, "_obligation_adapter", AsyncMock(return_value=adapter))
            monkeypatch.setattr(runner, "_arm_flood_timers_for_waiting_rows", AsyncMock())
            marked = []
            monkeypatch.setattr("gateway.delivery_ledger.mark_delivered", marked.append)
            assert await runner._redeliver_claimed_obligations([{
                "platform": "webhook", "content": "final one", "chat_id": chat, "thread_id": None,
                "obligation_id": obligation, "needs_marker": True, "attempts": 1}]) == 1
            assert marked == [obligation]
            assert len(seen) == 2 and seen[0] == seen[1] and seen[0][0] == "main-7"
            assert not (await adapter.send(chat, "different", metadata={**FINAL, "_webhook_obligation_id": obligation})).success
            assert raw.await_count == 0
        finally:
            dt.unregister_sink("keyed_recovery_capture")


@pytest.mark.asyncio
async def test_admission_freezes_worker_route_before_a_failed_post_commit_stamp(monkeypatch):
    adapter, app = app_for({**ROUTE, "provider": "owner-provider", "model": "frozen-model",
                           "fallback_providers": [{"provider": "backup", "model": "backup-model"}]})
    monkeypatch.setattr("gateway.platforms.webhook_session_keys.stamp_admitted_session", lambda *a, **kw: None)
    async with TestClient(TestServer(app)) as client:
        await post(client)
        runner = adapter._message_handler.__self__
        row = runner.session_authority.db._read_one("SELECT target_session_id,payload_json FROM session_admissions")
        native = json.loads(row["payload_json"])["native_text_v1"]
        frozen = native["webhook_delivery"]["worker_route"]
        assert frozen["provider"] == "owner-provider" and frozen["model"] == "frozen-model"
        assert frozen["fallback_providers"] == [{"provider": "backup", "model": "backup-model"}]
        adapter._routes["r"]["model"] = "edited-model"
        from gateway.platforms.webhook_route_runtime import stored_route
        from gateway.session_envelope import restore_native
        source = restore_native(json.loads(row["payload_json"]), runner).source
        assert stored_route(runner, source, row["target_session_id"])[0] == frozen


@pytest.mark.asyncio
async def test_exact_legacy_internal_retry_preserves_receipt_and_original_runtime():
    adapter, app = app_for()
    async with TestClient(TestServer(app)) as client:
        await post(client)
        runner = adapter._message_handler.__self__
        db = runner.session_authority.db
        row = db._read_one("SELECT admission_id,target_session_id,payload_json FROM session_admissions")
        payload = json.loads(row["payload_json"])
        native = payload["native_text_v1"]
        native["webhook_delivery"].pop("delivery_target")
        native["event"].pop("reply_expected")
        native["webhook_runtime"] = {"provider": "original-provider", "model": "original-model"}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        db._execute_write(lambda conn: conn.execute("UPDATE session_admissions SET payload_json=?,payload_digest=? WHERE admission_id=?",
            (encoded, hashlib.sha256(encoded.encode()).hexdigest(), row["admission_id"])))
        response = await client.post("/webhooks/r", data=json.dumps({"text": "work", "chat": "7", "key": "same"}).encode(),
            headers={"X-GitHub-Delivery": "one", "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(),
                json.dumps({"text": "work", "chat": "7", "key": "same"}).encode(), hashlib.sha256).hexdigest()})
        assert response.status == 200 and (await response.json())["status"] == "duplicate"
        assert db._read_one("SELECT payload_json FROM session_admissions")["payload_json"] == encoded
        from gateway.platforms.webhook_route_runtime import stored_route
        from gateway.session_envelope import restore_native
        restored = restore_native(payload, runner)
        assert restored.reply_expected is False
        frozen, _config = stored_route(runner, restored.source, row["target_session_id"])
        assert frozen["provider"] == "original-provider" and frozen["model"] == "original-model"
        native["event"]["reply_expected"] = True
        assert restore_native(payload, runner).reply_expected is True


@pytest.mark.asyncio
async def test_final_ledger_seam_stamps_the_handoff_metadata(monkeypatch):
    adapter, _app = app_for()
    capture = AsyncMock(return_value=(SendResult(success=True), adapter))
    monkeypatch.setattr(BasePlatformAdapter, "send_final_ledgered", capture)
    await adapter.send_final_ledgered(object(), "key", "final", {"thread_id": "9"}, reply_to=None)
    assert capture.await_args.args[3] == {"thread_id": "9", "_webhook_final_response": True,
                                           "_webhook_delivery_id": None}
