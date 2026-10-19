"""Automatic webhook identities are concrete before durable admission, never auth fallbacks."""
import hashlib
import hmac
import json
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import WebhookRuntimeOverride
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_runtime import retained_runtime, snapshot_runtime
from gateway.session_envelope import restore_native
from tests.gateway.fixtures.webhook_route_authority import mount_authority


@pytest.mark.asyncio
async def test_automatic_provider_is_frozen_in_signed_admission(tmp_path, monkeypatch):
    import gateway.run as gateway_run
    from hermes_cli import auth

    config = {"model": {"default": "global-before", "provider": "auto"}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    # Exercise auth.resolve_provider's real automatic-selection branch, without
    # consulting any host OAuth/AWS state or resolving credentials at admission.
    monkeypatch.setattr(auth, "_config_model_provider", lambda: (config["model"],
        config["model"]["provider"] if config["model"]["provider"] != "auto" else None))
    monkeypatch.setattr(auth, "_openrouter_auto_detected", lambda reader: True)
    adapter_config = PlatformConfig(enabled=True, extra={"secret": "owned-secret", "routes": {
        "fixture": {"model": "worker-model", "prompt": "{text}", "deliver": "log"}}})
    adapter = WebhookAdapter(adapter_config)
    app = web.Application()
    mount_authority(app, adapter)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        body = json.dumps({"text": "owned event"}).encode()
        headers = {"X-GitHub-Delivery": "auto-identity", "X-Hub-Signature-256":
                   "sha256=" + hmac.new(b"owned-secret", body, hashlib.sha256).hexdigest()}
        assert (await client.post("/webhooks/fixture", data=body, headers=headers)).status == 202
        authority = adapter._message_handler.__self__.session_authority
        row = authority.db._read_one("SELECT payload_json FROM session_admissions WHERE request_id=?",
                                     ("auto-identity",))
        payload = json.loads(row["payload_json"])
        expected = {"model": "worker-model", "provider": "openrouter"}
        assert payload["native_text_v1"]["webhook_runtime"] == expected
        source = restore_native(payload, authority.runner).source
        config["model"] = {"default": "global-after", "provider": "anthropic"}
        retry = await client.post("/webhooks/fixture", data=body, headers=headers)
        assert retry.status == 200, await retry.text()
        replacement = WebhookAdapter(adapter_config)
        runner = authority.runner
        runner.adapters[Platform.WEBHOOK] = replacement
        runner._wire_adapter_handlers(replacement)
        replacement.gateway_runner = runner
        assert retained_runtime(runner, source) == expected
        resolve = Mock(return_value={"provider": "openrouter"})
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs_for_provider", resolve)
        model, runtime = runner._resolve_session_agent_runtime(source=source)
        assert model == "worker-model" and runtime["_webhook_runtime_pinned"]
        resolve.assert_called_once_with("openrouter", target_model="worker-model")


def test_auto_local_endpoint_freezes_custom_without_auth_resolution(monkeypatch):
    import gateway.run as gateway_run
    from hermes_cli import auth
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
        "model": {"default": "global-model", "provider": "auto", "base_url": "http://127.0.0.1:12345/v1"}})
    resolve = Mock(side_effect=AssertionError("local identity must not inspect cloud authentication"))
    monkeypatch.setattr(auth, "resolve_provider", resolve)
    assert snapshot_runtime(WebhookRuntimeOverride(model="worker-model")) == {
        "model": "worker-model", "provider": "custom"}
    resolve.assert_not_called()


def test_auto_oauth_identity_does_not_resolve_or_fall_back_during_snapshot(monkeypatch):
    import gateway.run as gateway_run
    from hermes_cli import auth, runtime_provider as rp

    config = {"model": {"default": "global-model", "provider": "auto"}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    monkeypatch.setattr(auth, "_config_model_provider", lambda: (config["model"], None))
    monkeypatch.setattr(auth, "_openrouter_auto_detected", lambda reader: False)
    monkeypatch.setattr(auth, "_env_key_auto_detected", lambda reader, active: None)
    monkeypatch.setattr(auth, "_logged_in_oauth_active_provider", lambda **kwargs: "openai-codex")
    oauth = Mock(side_effect=auth.AuthError("owned revoked OAuth", provider="openai-codex", code="missing_api_key"))
    monkeypatch.setattr(rp, "_resolve_oauth_runtime", oauth)
    selected = snapshot_runtime(WebhookRuntimeOverride(model="worker-model"))
    assert selected == {"model": "worker-model", "provider": "openai-codex"}
    oauth.assert_not_called()

    # Once execution resolves the concrete identity, a revoked OAuth credential
    # must raise. Only an automatic request is permitted to swallow this error.
    monkeypatch.setattr(rp, "_get_model_config", lambda: config["model"])
    monkeypatch.setattr(rp, "_resolve_from_pool", lambda *args: None)
    fallback = Mock(side_effect=AssertionError("a concrete OAuth identity must not fall through"))
    monkeypatch.setattr(rp, "_openrouter_fallback", fallback)
    with pytest.raises(RuntimeError, match="owned revoked OAuth"):
        gateway_run._resolve_runtime_agent_kwargs_for_provider(selected["provider"], target_model=selected["model"])
    oauth.assert_called_once()
    fallback.assert_not_called()
