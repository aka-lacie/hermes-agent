"""Concurrent approval followers require a known matching origin."""
import time
import pytest
from tools import approval_context

class TestConcurrentApprovalCoalescing:
    @pytest.fixture(autouse=True)
    def known_origin(self, monkeypatch):
        # Coalescing now requires a known matching actor; unknown actors are tested separately.
        from types import SimpleNamespace
        from agent.approval_provenance import capture_origin
        from agent.tool_invocation import CallContext
        context = CallContext(SimpleNamespace(session_id="same-actor", _current_turn_id="turn"), "task", "call")
        captured = capture_origin(context, "terminal")
        monkeypatch.setattr("agent.approval_provenance.current_origin", lambda: captured)

    SESSION_KEY = "test-coalesce-session"

    def setup_method(self):
        from tools import approval as mod
        mod._gateway_queues.clear()
        mod._gateway_notify_cbs.clear()
        mod._session_approved.clear()
        mod._permanent_approved.clear()

    teardown_method = setup_method

    def _data(self, command="rm -rf .git"):
        return {
            "command": command,
            "description": "desc",
            "pattern_key": "dangerous",
            "pattern_keys": ["dangerous"],
        }

    def _spawn_waits(self, mod, notified, n=2, command="rm -rf .git"):
        import threading
        results = [None] * n
        threads = []
        for i in range(n):
            def _run(idx=i):
                results[idx] = mod._await_gateway_decision(
                    self.SESSION_KEY, notified.append, self._data(command)
                )
            t = threading.Thread(target=_run)
            t.start()
            threads.append(t)
            if i == 0:
                # Wait for the leader to enqueue AND for its notify_cb to
                # fire (the pre-approval hook dispatch runs between the
                # queue append and the notify, and can be slow on first
                # call) so follower threads deterministically find it and
                # coalesce against a fully-presented prompt.
                for _ in range(400):
                    if mod._gateway_queues.get(self.SESSION_KEY) and notified:
                        break
                    time.sleep(0.005)
            else:
                # Followers never enqueue; give the thread a beat to reach
                # the leader wait.
                time.sleep(0.05)
        return results, threads

    def test_identical_concurrent_approvals_send_one_prompt(self, monkeypatch):
        from tools import approval as mod
        monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 30)

        notified = []
        results, threads = self._spawn_waits(mod, notified, n=3)

        # Only the leader is in the queue; only one notify fired.
        assert len(mod._gateway_queues.get(self.SESSION_KEY, [])) == 1
        assert len(notified) == 1

        # One answer resolves everyone.
        assert mod.resolve_gateway_approval(self.SESSION_KEY, "session") == 1
        for t in threads:
            t.join(timeout=5)
        for r in results:
            assert r is not None and r["resolved"] and r["choice"] == "session"
        # Followers are marked as coalesced adoptions.
        assert sum(1 for r in results if r.get("coalesced")) == 2

    def test_deny_propagates_to_followers(self, monkeypatch):
        from tools import approval as mod
        monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 30)

        notified = []
        results, threads = self._spawn_waits(mod, notified, n=2)
        assert len(notified) == 1

        mod.resolve_gateway_approval(self.SESSION_KEY, "deny", reason="nope")
        for t in threads:
            t.join(timeout=5)
        for r in results:
            assert r is not None and r["choice"] == "deny"
        follower = next(r for r in results if r.get("coalesced"))
        assert follower["reason"] == "nope"

    def test_once_makes_follower_reprompt(self, monkeypatch):
        from tools import approval as mod
        monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 30)

        notified = []
        results, threads = self._spawn_waits(mod, notified, n=2)
        assert len(notified) == 1

        # "once" covers only the leader — the follower must re-prompt.
        mod.resolve_gateway_approval(self.SESSION_KEY, "once")
        for _ in range(400):
            if len(notified) == 2:
                break
            time.sleep(0.01)
        assert len(notified) == 2, "follower did not issue a fresh prompt after 'once'"

        mod.resolve_gateway_approval(self.SESSION_KEY, "once")
        for t in threads:
            t.join(timeout=5)
        assert all(r is not None and r["choice"] == "once" for r in results)

    def test_different_commands_are_not_coalesced(self, monkeypatch):
        from tools import approval as mod
        monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 30)
        import threading

        notified = []
        results = [None, None]

        def _run(idx, cmd):
            results[idx] = mod._await_gateway_decision(
                self.SESSION_KEY, notified.append, self._data(cmd)
            )

        t1 = threading.Thread(target=_run, args=(0, "rm -rf .git"))
        t1.start()
        for _ in range(200):
            if mod._gateway_queues.get(self.SESSION_KEY):
                break
            time.sleep(0.005)
        t2 = threading.Thread(target=_run, args=(1, "rm -rf /tmp/x"))
        t2.start()
        for _ in range(200):
            if len(mod._gateway_queues.get(self.SESSION_KEY, [])) == 2:
                break
            time.sleep(0.005)
        # notify_cb fires after the queue append (hook dispatch runs in
        # between and can be slow on first call) — wait for both prompts.
        for _ in range(1000):
            if len(notified) == 2:
                break
            time.sleep(0.005)

        # Two distinct prompts, two queue entries, two resolutions needed.
        assert len(notified) == 2
        assert len(mod._gateway_queues.get(self.SESSION_KEY, [])) == 2
        mod.resolve_gateway_approval(self.SESSION_KEY, "session", resolve_all=True)
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert all(r is not None and r["choice"] == "session" for r in results)

