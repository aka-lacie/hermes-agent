"""Keyed webhook sessions on the real runner and wire: one session, one FIFO, one failover chain.

Two production-runtime phases in subprocesses sharing one HERMES_HOME (the second is a gateway
restart). Only the provider is synthetic: a loopback peer that rejects the route's pinned primary
model with a content-filter block (a classifier "fall back now" error) and answers its declared
fallback model.

Phase 1: two deliveries for one key arrive while the first is still running; they commit to the
same session, run one at a time, and the second sees the first in its history. A delivery for another
key gets its own session, and an unkeyed one is finalized as before.
Phase 2 (restart): the next delivery for the key resumes that session with the stored route, so the
fresh agent again starts on the pinned primary and fails over onto the declared chain.
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys

SECRET = "owned-keyed-secret"
PRIMARY, FALLBACK = "primary-stub", "fallback-stub"


def _run_phase(tmp_path, phase, port):
    # One provider endpoint across the restart: a stored route keeps its chain's endpoint.
    env = dict(os.environ, HERMES_HOME=str(tmp_path), KEYED_PEER_PORT=str(port))
    result = subprocess.run([sys.executable, __file__, phase], env=env,
                            capture_output=True, text=True, timeout=150, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((tmp_path / f"keyed-{phase}.json").read_text())


def test_keyed_webhook_session_serializes_shares_history_and_keeps_its_failover_chain(tmp_path):
    import socket
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    first = _run_phase(tmp_path, "first", port)
    assert first["same_session"] and first["other_key_session"] != first["session"]
    assert first["second_queued_while_first_ran"]
    assert first["max_in_flight"] == 1
    assert first["second_saw_first"]
    assert first["keyed_open"] and first["unkeyed_closed"]
    assert first["rows"] == [["user", "webhook", "webhook:pr", "webhook", "d1"],
                             ["user", "webhook", "webhook:pr", "webhook", "d2"]]
    # Pinned primary first, then the declared chain, on each of the two keyed deliveries.
    assert first["models"][:2] == [PRIMARY, FALLBACK]
    assert first["models"].count(FALLBACK) >= 3 and set(first["models"]) == {PRIMARY, FALLBACK}
    # The one stored-route shape (agent/session_routes.py): the chain entry's endpoint is kept only as its
    # digest and rehydrated from the route config at every turn (phase 2 proves it survives a restart).
    from agent.session_routes import endpoint_fingerprint
    assert json.loads(first["stored_route"]) == {
        "v": 1, "kind": "declared", "provider": "custom", "model": PRIMARY,
        "fallback_providers": [{"provider": "custom", "model": FALLBACK, "key_env": "OPENAI_API_KEY",
                                "endpoint_sha256": endpoint_fingerprint(first["peer_url"])}]}
    assert first["peer_url"] not in first["stored_route"]
    again = _run_phase(tmp_path, "restart", port)
    assert again["session"] == first["session"]
    assert again["models"][:2] == [PRIMARY, FALLBACK]  # the stored route reached the fresh agent
    assert again["saw_history"]


def _signed(body):
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def probe(peer, url, phase):
    import aiohttp
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.platforms.webhook_session_keys import keyed_chat_id
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from tests.waiting import async_wait_until

    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id='default', instance_id=f'keyed-{phase}')
    route = {'prompt': '{text}', 'deliver': 'log', 'session_key': '{repo}#{number}',
             'provider': 'custom', 'model': PRIMARY,
             'fallback_providers': [{'provider': 'custom', 'model': FALLBACK, 'base_url': url,
                                     'key_env': 'OPENAI_API_KEY'}]}
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        'host': '127.0.0.1', 'port': 0, 'secret': SECRET, 'routes': {'pr': route}}))
    adapter.gateway_runner = runner
    runner.adapters[Platform.WEBHOOK] = adapter
    runner._wire_adapter_handlers(adapter)
    assert await adapter.connect()
    port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
    db = authority.db

    def rows():
        return {r['request_id']: dict(r) for r in db._read_all(
            'SELECT request_id, target_session_id, status FROM session_admissions')}

    async def post(client, payload, delivery):
        body = json.dumps(payload).encode()
        response = await client.post(f'http://127.0.0.1:{port}/webhooks/pr', data=body, headers={
            'X-GitHub-Delivery': delivery, 'X-GitHub-Event': 'push', 'X-Hub-Signature-256': _signed(body)})
        assert response.status == 202, (response.status, await response.text())

    def settled(*ids):
        current = rows()
        return all(current.get(i, {}).get('status') == 'terminal' for i in ids)

    out = {'peer_url': url}
    try:
        async with aiohttp.ClientSession() as client:
            if phase == 'first':
                await post(client, {'text': 'BLOCK_FIFO first look at PR one', 'repo': 'o/r', 'number': 1}, 'd1')
                assert await asyncio.to_thread(peer.blocked.wait, 30)
                await post(client, {'text': 'second look at PR one', 'repo': 'o/r', 'number': 1}, 'd2')
                current = rows()
                out['same_session'] = current['d1']['target_session_id'] == current['d2']['target_session_id']
                out['second_queued_while_first_ran'] = (current['d1']['status'], current['d2']['status']) == (
                    'started', 'queued')
                peer.release.set()
                await async_wait_until(lambda: settled('d1', 'd2'), timeout=60, what='both keyed deliveries')
                # Serialization is a property of ONE keyed session: measure it before d3 (another key) and
                # d4 (unkeyed) are posted, since those are independent sessions and may legitimately overlap.
                out['max_in_flight'] = peer.max_in_flight
                await post(client, {'text': 'PR two', 'repo': 'o/r', 'number': 2}, 'd3')
                await post(client, {'text': 'unkeyed event'}, 'd4')
                await async_wait_until(lambda: settled('d3', 'd4'), timeout=60, what='the other deliveries')
                await async_wait_until(lambda: not adapter._background_tasks, timeout=10, what='finalizers')
                current = rows()
                sid = current['d1']['target_session_id']
                out['session'], out['other_key_session'] = sid, current['d3']['target_session_id']
                out['keyed_open'] = db.get_session(sid)['ended_at'] is None
                out['unkeyed_closed'] = db.get_session(current['d4']['target_session_id'])['ended_at'] is not None
                out['rows'] = [[r['role'], r['sender_kind'], r['sender_id'], r['part_kind'], r['source_event_id']]
                               for r in db._read_all(
                                   "SELECT role, sender_kind, sender_id, part_kind, source_event_id FROM messages "
                                   "WHERE session_id=? AND role='user' ORDER BY id", (sid,))]
                out['stored_route'] = db._read_one('SELECT route FROM sessions WHERE id=?', (sid,))['route']
                second = [r for r in peer.requests if 'second look' in json.dumps(r['messages'][-1])]
                out['second_saw_first'] = bool(second) and 'BLOCK_FIFO first look' in json.dumps(second[-1])
            else:
                await post(client, {'text': 'third look after restart', 'repo': 'o/r', 'number': 1}, 'd5')
                await async_wait_until(lambda: settled('d5'), timeout=60, what='the resumed delivery')
                out['session'] = rows()['d5']['target_session_id']
                last = [r for r in peer.requests if r.get('model') == FALLBACK][-1]
                out['saw_history'] = 'second look at PR one' in json.dumps(last)
            out['models'] = [r.get('model') for r in peer.all_requests if 'messages' in r]
            out.setdefault('max_in_flight', peer.max_in_flight)
            out['keyed_chat'] = keyed_chat_id(None, 'pr', 'o/r#1')
    finally:
        peer.release.set()
        await adapter.disconnect()
    Path(os.environ['HERMES_HOME'], f'keyed-{phase}.json').write_text(json.dumps(out))


def main(phase):
    from http.server import ThreadingHTTPServer
    import threading
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path.insert(1, str(Path(__file__).parent / 'fixtures'))
    from shared_authority_peer import ModelPeer

    class FailoverPeer(ModelPeer):
        def do_POST(self):
            length = int(self.headers['Content-Length'])
            raw = self.rfile.read(length)
            request = json.loads(raw)
            server = self.server
            with server.lock:
                server.all_requests.append(request)
                server.in_flight += 1
                server.max_in_flight = max(server.max_in_flight, server.in_flight)
            try:
                if request.get('model') == PRIMARY:
                    body = json.dumps({'error': {'message': 'Your request was flagged by our content_filter.',
                                                 'type': 'invalid_request_error', 'code': 'content_filter'}}).encode()
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.rfile = __import__('io').BytesIO(raw)
                super().do_POST()
            finally:
                with server.lock:
                    server.in_flight -= 1

    ThreadingHTTPServer.allow_reuse_address = True
    peer = ThreadingHTTPServer(('127.0.0.1', int(os.environ['KEYED_PEER_PORT'])), FailoverPeer)
    peer.requests, peer.metadata_requests, peer.all_requests = [], [], []
    peer.blocked, peer.release, peer.lock = threading.Event(), threading.Event(), threading.Lock()
    peer.in_flight = peer.max_in_flight = 0
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    os.environ.update(OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)
    Path(os.environ['HERMES_HOME'], 'config.yaml').write_text(
        f'model:\n  default: local-wire-stub\n  provider: custom\n  base_url: {url}\n'
        'streaming:\n  enabled: false\nauxiliary:\n  title_generation:\n    enabled: false\n')
    try:
        asyncio.run(probe(peer, url, phase))
    finally:
        peer.shutdown()
        peer.server_close()


if __name__ == '__main__':
    import traceback
    status = 0
    try:
        main(sys.argv[1])
    except BaseException:
        traceback.print_exc()
        status = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)
