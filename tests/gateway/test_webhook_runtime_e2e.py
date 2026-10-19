"""Real signed ingress → durable authority → provider wire → optional main-chat turn.

Only the provider HTTP peer and Discord vendor transport are synthetic. Each case
owns a fresh process/home; an audit hook forbids non-loopback network connections.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest


WORKER_MODEL = "claude-opus-5-5"
WORKER_PROVIDER = "custom:worker"
WORKER_KEY = "owned-worker-fake-key"
MAIN_MODEL = "destination-chat-model"
MAIN_KEY = "owned-main-fake-key"


@pytest.mark.parametrize("mode", ["route", "platform", "internal_turn", "registered"])
def test_webhook_runtime_loopback(tmp_path, mode):
    host_home = tmp_path / "host"
    host_home.mkdir()
    result = subprocess.run(
        [sys.executable, __file__, mode],
        env=dict(os.environ, HERMES_HOME=str(tmp_path), HOME=str(host_home)),
        capture_output=True, text=True, timeout=100, check=False,
    )
    (tmp_path / "child.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["worker_attempts"] == 2  # one injected 503, one successful response
    assert receipt["successful_worker_inferences"] == 1
    assert receipt["webhook_admissions"] == 1
    assert receipt["global_requests"] == (2 if mode == "internal_turn" else 0)
    print(json.dumps(receipt, sort_keys=True))


class ProviderPeer(BaseHTTPRequestHandler):
    """Serve native Anthropic worker wire and OpenAI-compatible main-chat wire."""

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        self.server.gets.append(self.path)
        self._json(200, {"data": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "messages" not in body:
            self.server.metadata.append({"path": self.path, "body": body})
            self._json(200, {})
            return
        self.server.requests.append({"path": self.path, "body": body,
                                     "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.server.worker:
            if len(self.server.requests) == 1:
                self._json(503, {"type": "error", "error": {
                    "type": "overloaded_error", "message": "owned retry fixture"}})
                return
            self.server.blocked.set()
            assert self.server.release.wait(25), "worker release timed out"
            self.server.successes += 1
            response = {"id": "msg-owned-worker", "type": "message", "role": "assistant",
                "model": body["model"], "content": [{"type": "text", "text": "WORKER_HANDOFF_COMPLETE"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5}}
            if body.get("stream"):
                self._stream_anthropic(response)
            else:
                self._json(200, response)
        else:
            self.server.successes += 1
            response = {"id": "chatcmpl-owned-main", "object": "chat.completion",
                "created": 1, "model": body["model"], "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "MAIN_CHAT_COMPLETE"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            if body.get("stream"):
                self._stream_openai(response)
            else:
                self._json(200, response)

    def _start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

    def _stream_anthropic(self, response):
        self._start_stream()
        events = [
            {"type": "message_start", "message": {**response, "content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "text_delta", "text": "WORKER_HANDOFF_COMPLETE"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        for event in events:
            self.wfile.write((f"event: {event['type']}\ndata: {json.dumps(event)}\n\n").encode())
            self.wfile.flush()

    def _stream_openai(self, response):
        self._start_stream()
        for delta, finish in [({"role": "assistant", "content": "MAIN_CHAT_COMPLETE"}, None), ({}, "stop")]:
            chunk = {**response, "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


def _peer(worker):
    peer = ThreadingHTTPServer(("127.0.0.1", 0), ProviderPeer)
    peer.worker, peer.requests, peer.gets, peer.successes = worker, [], [], 0
    peer.metadata = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    return peer


def _loopback_only(event, args):
    if event in {"socket.connect", "socket.bind"}:
        address = args[1]
        if isinstance(address, tuple):
            host = address[0]
            if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
                raise RuntimeError(f"E2E forbids non-loopback network: {address}")


def _config(home, worker, main, mode):
    # JSON is valid YAML; no dependency on production config imports before isolation.
    config = {
        "model": {"default": MAIN_MODEL, "provider": "custom:main",
                  "base_url": f"http://127.0.0.1:{main.server_port}/v1", "api_key": MAIN_KEY,
                  "context_length": 128000},
        "custom_providers": [
            {"name": "worker", "base_url": f"http://127.0.0.1:{worker.server_port}/v1",
             "api_key": WORKER_KEY, "api_mode": "anthropic_messages", "context_length": 128000},
            {"name": "main", "base_url": f"http://127.0.0.1:{main.server_port}/v1",
             "api_key": MAIN_KEY, "api_mode": "chat_completions", "context_length": 128000},
        ],
        "fallback_model": [{"provider": "custom:main", "model": "forbidden-worker-fallback"}],
        "streaming": {"enabled": False}, "compression": {"enabled": False},
        "auxiliary": {"title_generation": {"enabled": False},
                      "vision": {"provider": WORKER_PROVIDER, "model": WORKER_MODEL}},
        "terminal": {"cwd": str(home)}, "display": {"busy_input_mode": "queue"},
    }
    if mode == "registered":
        # Exercise lazy profile-plugin discovery, not a custom_providers entry or
        # a patched resolver. The name matches deployment; all code/keys are owned fixtures.
        plugin = home / "plugins" / "model-providers" / "owned-cliproxy"
        plugin.mkdir(parents=True)
        (plugin / "__init__.py").write_text(
            "from providers import register_provider\n"
            "from providers.base import ProviderProfile\n"
            "register_provider(ProviderProfile(\n"
            "    name='cliproxy-claude', api_mode='anthropic_messages',\n"
            "    env_vars=('OWNED_CLIPROXY_API_KEY',),\n"
            f"    base_url='http://127.0.0.1:{worker.server_port}/v1',\n"
            "    supports_model_listing=False,\n"
            "    fallback_models=('claude-opus-5-5',)))\n"
        )
        os.environ["OWNED_CLIPROXY_API_KEY"] = WORKER_KEY
        # This global auto/local configuration previously hijacked registry providers
        # before their own credential/transport rung. A fake OpenRouter key makes an
        # accidental fall-through observable, rather than merely missing credentials.
        os.environ["OPENROUTER_API_KEY"] = MAIN_KEY
        config["model"]["provider"] = "auto"
        config["custom_providers"] = [config["custom_providers"][1]]
    if mode != "internal_turn":
        config["providers"] = {"custom:main": {"enabled": False}}
    (home / "config.yaml").write_text(json.dumps(config))
    return config


def _discord_adapter():
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class CaptureDiscord(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token="owned-fake-token"), Platform.DISCORD)
            self.sent = []

        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            pass

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            return SendResult(success=True, message_id=str(len(self.sent)))

        async def edit_message(self, chat_id, message_id, content, *, finalize=False):
            self.sent.append(content)
            return SendResult(success=True, message_id=message_id)

        async def send_typing(self, chat_id, metadata=None):
            pass

        async def get_chat_info(self, chat_id):
            return {"id": chat_id}

    return CaptureDiscord()


async def _probe(home, worker, main, mode, config):
    import aiohttp
    import weakref
    import gateway.run as gateway_run
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.session_contract import SessionRef
    from gateway.session_authority import initialize_session_authority
    from hermes_state_runtime import list_session_admissions
    from tests.waiting import async_wait_until

    if mode != "internal_turn":
        # Establish that resolving the global route really fails before constructing a client.
        with pytest.raises(RuntimeError, match="disabled"):
            gateway_run._resolve_runtime_agent_kwargs_for_provider("custom:main", target_model=MAIN_MODEL)
    runner = gateway_run.GatewayRunner()
    runner._gateway_loop = asyncio.get_running_loop()
    gateway_run._gateway_runner_ref = weakref.ref(runner)
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="runtime-e2e")
    discord, main_entry = None, None
    if mode == "internal_turn":
        discord = _discord_adapter()
        runner.adapters[Platform.DISCORD] = discord
        runner._wire_adapter_handlers(discord)
        source = discord.build_source(chat_id="owned-chat", chat_type="dm", user_id="owned-user")
        warm = MessageEvent(text="MAIN_WARMUP", source=source, message_id="main-warmup")
        await discord.handle_message(warm)
        await async_wait_until(lambda: any("MAIN_CHAT_COMPLETE" in s for s in discord.sent),
                               timeout=30, what="real destination warmup")
        main_entry = runner.session_store.get_or_create_session(source)
        assert len(main.requests) == 1

    route = {"prompt": "Inspect owned fixture: {text}"}
    extras = {"host": "127.0.0.1", "port": 0, "secret": "owned-hook-secret", "routes": {"fixture": route}}
    pin = {"model": WORKER_MODEL, "provider": WORKER_PROVIDER}
    (extras if mode == "platform" else route).update(pin)
    if mode == "internal_turn":
        route.update(delivery_mode="internal_turn", deliver="discord", deliver_extra={
            "chat_id": "owned-chat", "user_id": "owned-user", "chat_type": "dm"})
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra=extras))
    adapter.gateway_runner = runner
    runner.adapters[Platform.WEBHOOK] = adapter
    runner._wire_adapter_handlers(adapter)
    assert await adapter.connect()
    port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/webhooks/fixture"
    # Payload fields do not own runtime selection.
    body = json.dumps({"text": "OWNED_EVENT", "model": "untrusted-model", "provider": "custom:main"}).encode()
    headers = {"X-GitHub-Delivery": "owned-stable-delivery", "X-GitHub-Event": "push",
               "X-Hub-Signature-256": "sha256=" + hmac.new(b"owned-hook-secret", body, hashlib.sha256).hexdigest()}

    def rows():
        return [r for sid in authority.sessions for r in list_session_admissions(
            authority.db, session_id=sid, pending_only=False) if r["request_id"] == "owned-stable-delivery"]

    try:
        async with aiohttp.ClientSession() as client:
            accepted = await client.post(url, data=body, headers=headers)
            assert accepted.status == 202, await accepted.text()
            assert await asyncio.to_thread(worker.blocked.wait, 30), rows()
            assert len(rows()) == 1
            frozen = rows()[0]["payload"]["native_text_v1"]["webhook_runtime"]
            assert frozen == pin
            assert WORKER_KEY not in json.dumps(rows()[0]["payload"])
            agent = authority.agent(SessionRef("default", rows()[0]["target_session_id"]))
            assert not agent._fallback_chain, "worker inherited cross-provider fallback"
            if mode == "registered":
                assert agent.provider == "cliproxy-claude"
                assert agent.api_mode == "anthropic_messages"
            duplicate = await client.post(url, data=body, headers=headers)
            assert duplicate.status in (200, 202), await duplicate.text()
            worker.release.set()
            await async_wait_until(lambda: rows()[0]["status"] == "terminal", timeout=35,
                                   what="pinned webhook worker completion")
            assert rows()[0]["outcome"] == "completed", rows()
            if discord is not None:
                await async_wait_until(lambda: len(main.requests) == 2 and len(discord.sent) >= 2,
                                       timeout=30, what="actual InternalTurnService main-chat response")
                assert "WORKER_HANDOFF_COMPLETE" in json.dumps(main.requests[-1]["body"])
                assert "MAIN_WARMUP" in json.dumps(main.requests[-1]["body"])
                assert runner.session_store.get_or_create_session(source).session_id == main_entry.session_id
            # A config edit must not turn a retry into a conflicting new runtime selection.
            config["model"]["default"] = "changed-global-after-admission"
            (home / "config.yaml").write_text(json.dumps(config))
            if mode == "platform":
                adapter.config.extra.update(model="changed-platform-after-admission", provider="custom:main")
            # Drop the local dedupe optimization; the signed admission still owns identity and pin.
            adapter._seen_deliveries.clear()
            duplicate = await client.post(url, data=body, headers=headers)
            assert duplicate.status in (200, 202), await duplicate.text()
            await async_wait_until(lambda: not adapter._background_tasks, timeout=10,
                                   what="webhook duplicate dispatch settlement")
            assert len(rows()) == 1 and len(worker.requests) == 2 and worker.successes == 1
            assert rows()[0]["payload"]["native_text_v1"]["webhook_runtime"] == pin
            for request in worker.requests:
                assert request["path"].split("?")[0] == "/v1/messages", request
                assert request["body"]["model"] == WORKER_MODEL
                assert request["headers"].get("x-api-key") == WORKER_KEY, request["headers"]
                assert MAIN_KEY not in json.dumps(request)
            for request in main.requests:
                assert request["path"] == "/v1/chat/completions"
                assert request["body"]["model"] == MAIN_MODEL
                assert request["headers"].get("authorization") == f"Bearer {MAIN_KEY}"
                assert WORKER_KEY not in json.dumps(request)
            assert len(main.requests) == (2 if discord else 0)
            if discord is None:
                assert not main.gets and not main.metadata, (main.gets, main.metadata)
            (home / "receipt.json").write_text(json.dumps({"mode": mode, "frozen_runtime": frozen,
                "worker_attempts": len(worker.requests), "successful_worker_inferences": worker.successes,
                "webhook_admissions": len(rows()), "global_requests": len(main.requests),
                "worker_wire": [r["path"] for r in worker.requests],
                "main_models": [r["body"]["model"] for r in main.requests]}, indent=2))
    finally:
        worker.release.set()
        await adapter.disconnect()


def main(mode):
    global WORKER_PROVIDER
    if mode == "registered":
        WORKER_PROVIDER = "cliproxy-claude"
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.addaudithook(_loopback_only)
    os.environ["DISCORD_ALLOWED_USERS"] = "owned-user"
    home = Path(os.environ["HERMES_HOME"])
    worker, destination = _peer(True), _peer(False)
    config = _config(home, worker, destination, mode)
    try:
        asyncio.run(_probe(home, worker, destination, mode, config))
    finally:
        (home / "wire.json").write_text(json.dumps({"worker": worker.requests,
            "main": destination.requests, "main_metadata": destination.metadata,
            "main_gets": destination.gets}, indent=2))
        for peer in (worker, destination):
            peer.release.set()
            peer.shutdown()
            peer.server_close()


if __name__ == "__main__":
    import traceback
    status = 0
    try:
        main(sys.argv[1])
    except BaseException:
        traceback.print_exc()
        status = 1
    sys.stdout.flush()
    sys.stderr.flush()
    # This disposable child owns runtime heartbeat/reaper threads; don't retain its executor.
    os._exit(status)
