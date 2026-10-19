"""A real daemon restart drains a signed, pending worker using its admitted runtime."""
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import urllib.request

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon
from tests.gateway.test_webhook_runtime_e2e import (
    MAIN_KEY, WORKER_KEY, WORKER_MODEL, WORKER_PROVIDER, _config, _loopback_only, _peer,
)
from tests.waiting import wait_until


def test_pending_webhook_executes_frozen_pair_after_process_restart(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / "state", tmp_path / "user"
    home.mkdir(mode=0o700)
    user.mkdir()
    worker, forbidden = _peer(True), _peer(False)
    worker.release.set()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = _config(home, worker, forbidden, "platform")
    config.pop("providers", None)
    config["gateway"] = {"multiplex_profiles": False}
    route = {"prompt": "{text}", "deliver": "log"}
    extra = {"host": "127.0.0.1", "port": port, "secret": "owned-restart-secret",
             "model": WORKER_MODEL, "provider": WORKER_PROVIDER, "routes": {"fixture": route}}
    config["platforms"] = {"webhook": {"enabled": True, "extra": extra}}
    config_path = home / "config.yaml"
    config_path.write_text(json.dumps(config))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
               HERMES_RUNTIME_DIR=str(home / "runtime"), PYTHONPATH=str(root),
               PYTHONUNBUFFERED="1", OWNED_RESTART_HOLD="1")

    def row():
        with closing(sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM session_admissions WHERE request_id='restart-pinned'").fetchall()
            assert len(rows) == 1
            result = dict(rows[0])
            result["payload"] = json.loads(result["payload_json"])
            return result

    evidence = []
    try:
        # Both boots run gateway.run normally; only boot one holds _schedule before claim.
        with daemon(root, home, env, barrier=True, fixture="../test_webhook_runtime_restart.py") as (proc, desc):
            body = json.dumps({"text": "SAFE_QUEUE"}).encode()
            signature = hmac.new(b"owned-restart-secret", body, hashlib.sha256).hexdigest()
            request = urllib.request.Request(f"http://127.0.0.1:{port}/webhooks/fixture", data=body,
                headers={"Content-Type": "application/json", "X-GitHub-Delivery": "restart-pinned",
                         "X-Hub-Signature-256": "sha256=" + signature}, method="POST")
            with urllib.request.urlopen(request, timeout=15) as response:
                assert response.status == 202
            first = row()
            expected = {"model": WORKER_MODEL, "provider": WORKER_PROVIDER}
            assert first["status"] == "queued"
            assert first["payload"]["native_text_v1"]["webhook_runtime"] == expected
            assert WORKER_KEY not in first["payload_json"] and MAIN_KEY not in first["payload_json"]
            assert not worker.requests and not forbidden.requests
            evidence.append({"pid": proc.pid, "epoch": desc["authority_epoch"], "status": first["status"]})
            proc.kill()
            proc.wait(timeout=10)
        (home / "boot1.log").write_text((home / "first.log").read_text())

        # Only defaults change: retain route bytes, signer and original provider definition.
        config["custom_providers"].append({"name": "changed",
            "base_url": f"http://127.0.0.1:{forbidden.server_port}/v1", "api_key": MAIN_KEY})
        config["model"].update(default="changed-global-model", provider="custom:changed")
        extra.update(model="changed-platform-model", provider="custom:changed")
        config_path.write_text(json.dumps(config))
        env["OWNED_RESTART_HOLD"] = "0"
        with daemon(root, home, env, barrier=True, fixture="../test_webhook_runtime_restart.py") as (proc, desc):
            wait_until(lambda: row()["status"] == "terminal", timeout=45,
                       what="restarted gateway drains the queued webhook")
            settled = row()
            assert settled["outcome"] == "completed", settled
            assert settled["admission_id"] == first["admission_id"]
            assert settled["payload"] == first["payload"]
            assert settled["payload"]["native_text_v1"]["webhook_runtime"] == expected
            evidence.append({"pid": proc.pid, "epoch": desc["authority_epoch"], "status": settled["status"]})
            assert proc.pid != evidence[0]["pid"]
            assert desc["authority_epoch"] > evidence[0]["epoch"]
            assert worker.successes == 1 and len(worker.requests) == 2
            assert not forbidden.requests and not forbidden.metadata and not forbidden.gets
            for request in worker.requests:
                assert request["path"].split("?")[0] == "/v1/messages"
                assert request["body"]["model"] == WORKER_MODEL
                assert request["headers"].get("x-api-key") == WORKER_KEY
                assert MAIN_KEY not in json.dumps(request)
                assert "SAFE_QUEUE" in json.dumps(request["body"])
        (home / "boot2.log").write_text((home / "first.log").read_text())
        print(json.dumps({"boots": evidence, "frozen_runtime": expected,
                          "worker_attempts": len(worker.requests), "worker_successes": worker.successes}))
    finally:
        for peer in (worker, forbidden):
            peer.release.set()
            peer.shutdown()
            peer.server_close()


if __name__ == "__main__":
    import runpy
    sys.addaudithook(_loopback_only)
    if os.environ["OWNED_RESTART_HOLD"] == "1":
        from gateway.session_authority import SessionAuthority
        from hermes_state_runtime import list_session_admissions
        schedule = SessionAuthority._schedule

        def hold_pending_worker(self, ref):
            pending = list_session_admissions(self.db, session_id=ref.session_id)
            if any(row["request_id"] == "restart-pinned" for row in pending):
                return
            return schedule(self, ref)

        SessionAuthority._schedule = hold_pending_worker
    runpy.run_module("gateway.run", run_name="__main__")
