"""Unified-session delegation (Phase 3): children are sessions rows driven through session_admissions.

Real temporary SessionDBs exercise the store, the FIFO primitives, delivery targets and the list rule;
the end-to-end cases build real AIAgent children with only ``run_conversation`` replaced.
"""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import (
    admit_session_input, claim_session_input, list_session_admissions, settle_session_input,
)
from tools.delegate_session_store import DelegateSessionError, DelegateSessionStore, runtime_epoch


@pytest.fixture
def state(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    yield db, DelegateSessionStore(db)
    db.close()


def child(db, store, sid="child", parent="parent", **kwargs):
    db.create_session(sid, "subagent", parent_session_id=parent, model_config={"_delegate_from": parent})
    kwargs.setdefault("route", {"v": 1, "kind": "named", "provider": "chosen-provider", "model": "chosen-model",
                                "fallback_providers": [{"provider": "backup", "model": "backup-model"}]})
    return store.register(parent, sid, model="chosen-model", provider="chosen-provider",
                          assignment=kwargs.pop("assignment", "Review the implementation"), **kwargs)


def session_row(db, sid):
    return dict(db._read_one("SELECT * FROM sessions WHERE id = ?", (sid,)))


# ── the child row and its FIFO ───────────────────────────────────────────────

def test_spawned_child_is_an_ordinary_session_row_with_its_contract_columns(state):
    db, store = state
    record = child(db, store, title="Review", workspace="/review/project", forked_context=True, group_id="deleg-1")
    row = session_row(db, "child")
    assert row["lineage_kind"] == "delegate" and row["parent_session_id"] == "parent"
    assert row["root_session_id"] == "parent" and row["depth"] == 1
    assert json.loads(row["delivery_target"]) == {"kind": "session", "session_id": "parent", "attach": True}
    route = json.loads(row["route"])
    assert route["fallback_providers"] == [{"provider": "backup", "model": "backup-model"}]
    assert not {"api_key", "base_url"} & set(route)
    assert json.loads(row["fork_origin"])["session_id"] == "parent"
    assert (row["role"], row["workspace"], row["status"], row["group_id"]) == ("leaf", "/review/project", "idle", "deleg-1")
    assert record["title"] == "Review" and record["lineage_kind"] == "delegate"
    # The store never creates or writes the retired parallel table.
    assert not db._read_one("SELECT 1 FROM sqlite_master WHERE name = 'delegate_sessions'")


def test_assignments_are_admissions_on_the_child_fifo_and_release_settles_exactly_once(state):
    db, store = state
    child(db, store)
    record, token = store.acquire("parent", "child", "First assignment")
    assert record["status"] == "running"
    rows = list_session_admissions(db, session_id="child", pending_only=False)
    assert [(r["status"], r["principal_id"], r["payload"]["sender_kind"]) for r in rows] == [
        ("started", "parent:parent", "parent")]
    db.append_message("child", "user", "First assignment")
    db.append_message("child", "assistant", "Result one", origin="model")
    assert store.release("child", token)
    assert not store.release("child", token)  # late cleanup cannot settle twice
    assert store.get("parent", "child")["status"] == "idle"
    assert [r["outcome"] for r in list_session_admissions(db, session_id="child", pending_only=False)] == ["completed"]
    _, second = store.acquire("parent", "child", "Follow up", model="alternate-model")
    with pytest.raises(DelegateSessionError) as busy:
        store.close("parent", "child")
    assert busy.value.code == "busy"
    assert store.get("parent", "child")["assignment"] == "Follow up"
    assert store.release("child", second)
    store.close("parent", "child")
    with pytest.raises(DelegateSessionError) as closed:
        store.acquire("parent", "child", "Do more")
    assert closed.value.code == "closed"
    assert store.list("parent") == []
    assert store.list("parent", query="Review")[0]["status"] == "closed"
    assert store.observe("parent", "child")["messages"][-1]["content"] == "Result one"


def test_a_resume_while_running_waits_in_the_fifo_instead_of_being_refused(state):
    db, store = state
    child(db, store)
    _, token = store.acquire("parent", "child", "First")
    queued = store.admit("parent", "child", "Second, queued behind the run")
    assert queued["busy"] and queued["status"] == "queued" and queued["position"] == 0
    assert store.claim("child") is None  # the running claim holds the FIFO
    store.release("child", token)
    claim = store.claim("child")
    assert claim["admission_id"] == queued["admission_id"] and claim["payload"]["text"].startswith("Second")
    store.release("child", claim["token"])


def test_a_resume_and_a_webhook_style_delivery_serialize_on_one_session(state):
    """One FIFO, no second lock: whichever arrives first runs first; the other waits its turn."""
    from gateway.delivery_targets import DeliveryEvent, admit_event, event_from_payload
    db, store = state
    child(db, store)
    _, running = store.acquire("parent", "child", "Running assignment")
    resumed = store.admit("parent", "child", "Resume while busy")
    delivered = admit_event(db, "child", DeliveryEvent("webhook", "PR #90 updated", "webhook", "webhook:github-pr"))
    assert delivered.delivered and delivered.sink == "session_fifo"
    queued = [r for r in list_session_admissions(db, session_id="child") if r["status"] == "queued"]
    assert [r["admission_id"] for r in queued] == [resumed["admission_id"], delivered.admission_id]
    store.release("child", running)
    first = store.claim("child")
    assert first["admission_id"] == resumed["admission_id"]
    assert store.claim("child") is None
    store.release("child", first["token"])
    second = store.claim("child")
    assert event_from_payload(second["payload"])["sender_kind"] == "webhook"
    store.release("child", second["token"])


def test_ownership_follows_compression_but_never_a_fork(state):
    db, store = state
    child(db, store)
    db.end_session("parent", "compression")
    db.create_session("parent-tip", "cli", parent_session_id="parent")
    assert store.get("parent-tip", "child")["session_id"] == "child"
    for sid, source, marker in [("branch", "cli", "_branched_from"), ("sibling", "subagent", "_delegate_from")]:
        db.create_session(sid, source, parent_session_id="parent", model_config={marker: "parent"})
        with pytest.raises(DelegateSessionError) as foreign:
            store.get(sid, "child")
        assert foreign.value.code == "unavailable"
    db.end_session("child", "compression")
    db.create_session("child-tip", "subagent", parent_session_id="child", model_config={"_delegate_from": "parent"})
    record, token = store.acquire("parent-tip", "child", "Continue")
    assert (record["session_id"], record["current_session_id"]) == ("child", "child-tip")
    assert store.release("child", token, current_session_id="child-tip")


def test_deleted_compression_tip_is_unavailable_and_never_falls_back(state):
    db, store = state
    child(db, store, "rotated")
    db.end_session("rotated", "compression")
    db.create_session("tip", "subagent", parent_session_id="rotated")
    assert store.get("parent", "rotated")["current_session_id"] == "tip"
    db.delete_session("tip")
    with pytest.raises(DelegateSessionError):
        store.acquire("parent", "rotated", "Do not fall back to stale history")


def test_dead_runner_settles_interrupted_and_is_never_replayed(state):
    db, store = state
    child(db, store)
    _, token = store.acquire("parent", "child", "Abandoned assignment")
    admission = token.split(":")[0]
    db._write_sql("UPDATE state_meta SET value = ? WHERE key = ?",
                  (json.dumps({"pid": 2 ** 22 + 12345, "pidns": None, "start_ticks": 1}), "delegate.runner.v1." + admission))
    recovered = store.get("parent", "child")
    assert recovered["status"] == "idle"
    assert [r["outcome"] for r in list_session_admissions(db, session_id="child", pending_only=False)] == ["interrupted"]
    assert db.get_messages("child") == []
    assert not store.release("child", token)


def test_route_cannot_persist_a_credential_bundle(state):
    db, store = state
    db.create_session("child", "subagent", parent_session_id="parent", model_config={"_delegate_from": "parent"})
    with pytest.raises(DelegateSessionError) as rejected:
        store.register("parent", "child", model="m", provider="p", assignment="x",
                       route={"v": 1, "kind": "named", "provider": "p", "api_key": "must-not-be-stored"})
    assert rejected.value.code == "invalid_route"


# ── steer: atomic transfer and lossless handoff ──────────────────────────────

def test_steer_transfers_to_the_running_claim_and_an_undelivered_one_runs_next(state):
    from hermes_state_runtime import revert_steer_input, steer_session_input
    db, store = state
    child(db, store)
    _, token = store.acquire("parent", "child", "Long task")
    running = store.running_claim("child")
    delivered = store.admit("parent", "child", "Narrow the scope", intent="steer")
    late = store.admit("parent", "child", "Also check the docs", intent="steer")
    queued = store.admit("parent", "child", "Unrelated follow-up")
    epoch = runtime_epoch(db)
    for item in (delivered, late):
        steer_session_input(db, epoch=epoch, admission_id=item["admission_id"], generation=running["generation"])
    # The closing turn never reached the second one's boundary: it goes back to its own FIFO place.
    revert_steer_input(db, epoch=epoch, admission_id=late["admission_id"], generation=running["generation"])
    assert store.release("child", token, transfers={delivered["admission_id"]: True})
    outcomes = {r["admission_id"]: (r["status"], r["outcome"])
                for r in list_session_admissions(db, session_id="child", pending_only=False)}
    assert outcomes[delivered["admission_id"]] == ("terminal", "steered")
    assert outcomes[late["admission_id"]] == ("queued", None)
    nxt = store.claim("child")
    assert nxt["admission_id"] == late["admission_id"]  # priority: the steer keeps its earlier FIFO seat
    store.release("child", nxt["token"])
    assert store.claim("child")["admission_id"] == queued["admission_id"]


def test_steer_accepted_but_missed_is_requeued_not_lost():
    from types import SimpleNamespace
    from tools.delegate_sessions import _turn_outcome, note_steer_accepted
    agent = SimpleNamespace(_delegate_steer_transfers={}, _pending_steer=None,
                            _delegate_turn_entry={"status": "completed", "missed_steer": "x"})
    note_steer_accepted(agent, "a1")
    agent._pending_steer = "first"  # a boundary has not taken it yet
    note_steer_accepted(agent, "a2")
    outcome, transfers = _turn_outcome(agent)
    assert outcome == "completed" and transfers == {"a1": False, "a2": False}
    agent._pending_steer = None  # a boundary took everything so far
    note_steer_accepted(agent, "a3")
    agent._delegate_turn_entry = {"status": "completed"}
    assert _turn_outcome(agent)[1] == {"a1": True, "a2": True, "a3": True}


# ── wake policy ──────────────────────────────────────────────────────────────

def test_batch_with_next_user_turn_holds_until_the_next_user_turn(state):
    from gateway.delivery_targets import DeliveryEvent, admit_event, write_held_events
    db, store = state
    child(db, store)
    held = admit_event(db, "child", DeliveryEvent("cron", "nightly digest ready", "cron", "cron:digest"),
                       wake_policy="batch_with_next_user_turn")
    assert held.delivered
    assert store.claim("child") is None  # a held notice never runs a turn by itself
    user = store.admit("parent", "child", "Next real assignment")
    claim = store.claim("child")
    assert claim["admission_id"] == user["admission_id"]
    assert [h["admission_id"] for h in claim["held"]] == [held.admission_id]
    write_held_events(db, "child", claim)
    store.release("child", claim["token"])
    rows = db.get_messages("child")
    assert [(r["role"], r["sender_kind"], r["sender_id"], r["part_kind"]) for r in rows] == [
        ("user", "cron", "cron:digest", "cron")]
    outcome = {r["admission_id"]: r["outcome"] for r in list_session_admissions(db, session_id="child", pending_only=False)}
    assert outcome[held.admission_id] == "batched"


def test_a_held_notice_its_claim_never_wrote_goes_back_to_wait(state):
    from gateway.delivery_targets import DeliveryEvent, admit_event
    db, store = state
    child(db, store)
    held = admit_event(db, "child", DeliveryEvent("cron", "later", "cron", "cron:x"), wake_policy="batch_with_next_user_turn")
    store.admit("parent", "child", "Assignment")
    claim = store.claim("child")
    assert claim["held"]
    store.release("child", claim["token"], outcome="failed")  # crashed before writing it
    again = {r["admission_id"]: (r["status"], r["wake_policy"]) for r in list_session_admissions(db, session_id="child")}
    assert again[held.admission_id] == ("queued", "batch_with_next_user_turn")


def test_immediate_is_today_behaviour_and_unknown_policies_refuse(state):
    from hermes_state_runtime import RuntimeStoreError
    db, _ = state
    epoch = runtime_epoch(db)
    row = admit_session_input(db, epoch=epoch, principal_id="automation:x", session_id="parent", request_id="r1",
                              payload={"text": "now"}, wake_policy="immediate")
    assert row["wake_policy"] is None
    assert claim_session_input(db, epoch=epoch, session_id="parent")["admission_id"] == row["admission_id"]
    with pytest.raises(RuntimeStoreError):
        admit_session_input(db, epoch=epoch, principal_id="automation:x", session_id="parent", request_id="r2",
                            payload={"text": "?"}, wake_policy="eventually")


# ── delivery targets ─────────────────────────────────────────────────────────

def test_child_completion_becomes_a_typed_user_admission_on_the_parent(state):
    """A d=2 child's completion lands on its delegate parent's FIFO; the parent turn's input is one typed
    system_event row from the child (Rule 1: nothing is written as assistant)."""
    from gateway.delivery_targets import child_completion_event, dispatch, event_from_payload, event_typed_input
    from agent.message_parts import apply_typed_input
    db, store = state
    child(db, store, "orchestrator", role="orchestrator")
    child(db, store, "grandchild", parent="orchestrator")
    row = db._read_one("SELECT * FROM sessions WHERE id = ?", ("grandchild",))
    receipt = dispatch(db, row, child_completion_event(row, {"status": "completed", "summary": "Found 3 issues"}))
    assert receipt.delivered and receipt.target_session_id == "orchestrator" and receipt.sink == "session_fifo"
    admission = list_session_admissions(db, session_id="orchestrator")[0]
    event = event_from_payload(admission["payload"])
    assert (event["kind"], event["sender_kind"], event["sender_id"]) == ("subagent_complete", "child", "grandchild")
    assert admission["principal_id"] == "child:grandchild"
    msg = {"role": "user", "content": "Found 3 issues"}
    assert apply_typed_input(msg, event_typed_input([event]))
    assert msg["sender_kind"] == "child" and msg["_parts"][0]["type"] == "system_event"
    assert msg["_parts"][0]["meta"]["status"] == "completed"


def test_batch_siblings_fold_into_one_queued_parent_turn(state):
    from gateway.delivery_targets import DeliveryEvent, admit_event, event_events
    db, store = state
    child(db, store, "orchestrator", role="orchestrator")
    for i in range(3):
        admit_event(db, "orchestrator", DeliveryEvent("subagent_complete", f"result {i}", "child", f"c{i}",
                                                      meta={"status": "completed"}, fold_key="deleg-7"))
    queued = [r for r in list_session_admissions(db, session_id="orchestrator") if r["status"] == "queued"]
    assert len(queued) == 1
    assert [e["sender_id"] for e in event_events(queued[0]["payload"])] == ["c0", "c1", "c2"]
    # A sibling arriving after the folded turn was claimed queues alone.
    claim = DelegateSessionStore(db).claim("orchestrator")
    late = admit_event(db, "orchestrator", DeliveryEvent("subagent_complete", "result 3", "child", "c3", fold_key="deleg-7"))
    assert late.delivered and not late.folded
    assert claim["admission_id"] != late.admission_id


def test_platform_and_none_targets_write_nothing_into_any_session(state):
    from gateway import delivery_targets as dt
    db, _ = state
    before = db._read_one("SELECT COUNT(*) FROM session_admissions")[0], db._read_one("SELECT COUNT(*) FROM messages")[0]
    sent = []
    dt.register_platform_sender("test", lambda target, text, *, event: sent.append((target, text)) or True)
    try:
        event = dt.DeliveryEvent("cron", "ping", "cron", "cron:release-check")
        receipt = dt.dispatch(db, None, event, target={"kind": "platform", "platform": "discord", "chat_id": "123"})
        assert receipt.delivered and sent == [({"kind": "platform", "platform": "discord", "chat_id": "123",
                                                "thread_id": None}, "ping")]
        assert not dt.dispatch(db, {"delivery_target": None}, event).delivered
    finally:
        dt.unregister_platform_sender("test")
    after = db._read_one("SELECT COUNT(*) FROM session_admissions")[0], db._read_one("SELECT COUNT(*) FROM messages")[0]
    assert before == after


def test_malformed_targets_never_guess_a_destination():
    from gateway.delivery_targets import DeliveryTargetError, parse_delivery_target, resolve_delivery_target
    for bad in ({"kind": "session"}, {"kind": "session", "session_id": "x", "attach": "yes"},
                {"kind": "platform"}, {"kind": "mail"}, "[not json"):
        with pytest.raises(DeliveryTargetError):
            parse_delivery_target(bad)
    assert resolve_delivery_target({"id": "s", "delivery_target": '{"kind": "mail"}'}) == {"kind": "none"}


def test_cron_kind_session_admits_typed_cron_event_and_kind_platform_only_rewrites_deliver(state, monkeypatch):
    from cron import scheduler_delivery as sd
    from cron.session_delivery import deliver_to_session, platform_job
    from gateway.delivery_targets import event_from_payload
    db, store = state
    child(db, store, "watcher")
    job = {"id": "digest", "name": "Digest", "execution_id": "e1",
           "delivery_target": {"kind": "session", "session_id": "watcher", "attach": False}}
    assert deliver_to_session(job, "3 new items", job["delivery_target"], db=db) is None
    event = event_from_payload(list_session_admissions(db, session_id="watcher")[0]["payload"])
    assert (event["kind"], event["sender_kind"], event["sender_id"]) == ("cron", "cron", "cron:digest")
    platform = {**job, "delivery_target": {"kind": "platform", "platform": "discord", "chat_id": "42", "thread_id": "7"},
                "attach_to_session": True}
    rewritten = platform_job(platform, {"kind": "platform", "platform": "discord", "chat_id": "42", "thread_id": "7"})
    assert rewritten["deliver"] == "discord:42:7" and rewritten["attach_to_session"] is False
    assert sd._cron_mirror_delivery_enabled(platform, {"cron": {"mirror_delivery": True}}) is False
    # kind=none delivers nothing and reports success.
    assert sd._deliver_result({"id": "x", "delivery_target": {"kind": "none"}}, "quiet") is None


# ── stored routes ────────────────────────────────────────────────────────────

def test_failover_chain_and_route_effort_survive_resume(monkeypatch):
    from types import SimpleNamespace
    from agent.session_routes import capture_route, resolve_route
    from tools.delegate_tool import resumed_route
    route = capture_route(provider="cliproxy-claude", model="claude-x", base_url="https://example.test/v1",
                          routing_cfg={"fallback_providers": [{"provider": "openai-codex", "model": "gpt-x",
                                                               "api_key": "sk-never-stored"}]},
                          reasoning_effort="high", difficulty="hard", backend="per-task")
    assert route["fallback_providers"] == [{"provider": "openai-codex", "model": "gpt-x"}]
    assert "sk-never-stored" not in json.dumps(route)
    monkeypatch.setattr("tools.delegate_tool_config._load_config", lambda: {"reasoning_effort": "low"})
    block = resolve_route(route, SimpleNamespace(provider="custom"))
    assert block["fallback_providers"] == [{"provider": "openai-codex", "model": "gpt-x"}]
    assert block["provider"] == "cliproxy-claude" and block["reasoning_effort"] == "high"
    captured = {}

    def creds(cfg, parent):
        captured.update(cfg)
        return {"provider": cfg["provider"], "model": cfg["model"], "base_url": "https://example.test/v1",
                "api_key": "k", "api_mode": "chat_completions"}
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", creds)
    task_route = resumed_route(SimpleNamespace(provider="custom"), {"route": route}, model="claude-y")
    assert task_route.routing_cfg["fallback_providers"] == [{"provider": "openai-codex", "model": "gpt-x"}]
    assert task_route.reasoning_effort == "high" and captured["model"] == "claude-y"


def test_anonymous_endpoint_must_still_match_its_digest(monkeypatch):
    from types import SimpleNamespace
    from agent.session_routes import RouteError, capture_route, resolve_route
    route = capture_route(provider="custom", model="local", base_url="http://127.0.0.1:8080/v1",
                          routing_cfg={"provider": "custom"})
    assert route["endpoint_sha256"] and "base_url" not in route
    monkeypatch.setattr("tools.delegate_tool_config._load_config", dict)
    monkeypatch.setattr("tools.delegate_tool_config._resolve_delegation_credentials",
                        lambda cfg, parent: {"provider": "custom", "base_url": "http://127.0.0.1:9999/v1"})
    with pytest.raises(RouteError):
        resolve_route(route, SimpleNamespace(provider="custom"))


# ── Freya's list rule, rollups, observe, ACL ─────────────────────────────────

def _webhook(db, sid, *, target, attach, parent=None, kind="webhook"):
    db.create_session(sid, "webhook", parent_session_id=parent)
    db._write_sql("UPDATE sessions SET lineage_kind = ?, delivery_target = ?, status = 'idle' WHERE id = ?",
                  (kind, json.dumps({"kind": "session", "session_id": target, "attach": attach}) if target else None, sid))


def test_default_list_is_own_spawns_plus_explicit_opt_ins_only(state):
    db, store = state
    child(db, store, "mine", title="Mine")
    # Excluded: a compression continuation of the caller, a branch, a memory fork, a standalone cron,
    # an unattached webhook delivering here, and a delegate of another conversation.
    db.end_session("parent", "compression")
    db.create_session("parent-tip", "cli", parent_session_id="parent")
    db._write_sql("UPDATE sessions SET lineage_kind = 'continuation' WHERE id = 'parent-tip'")
    db.create_session("branch", "cli", parent_session_id="parent", model_config={"_branched_from": "parent"})
    db._write_sql("UPDATE sessions SET lineage_kind = 'branch' WHERE id = 'branch'")
    db.create_session("memory-fork", "subagent", parent_session_id="parent")
    db.create_session("standalone-cron", "cron")
    db._write_sql("UPDATE sessions SET lineage_kind = 'cron' WHERE id = 'standalone-cron'")
    _webhook(db, "pr-quiet", target="parent", attach=False)
    _webhook(db, "pr-90", target="parent", attach=True)
    db.create_session("other", "cli")
    child(db, store, "not-mine", parent="other")
    listed = {row["session_id"]: row for row in store.list("parent-tip")}
    assert set(listed) == {"mine", "pr-90"}
    assert listed["pr-90"]["badge"] == "webhook" and "badge" not in listed["mine"]


def test_grandchildren_roll_up_and_observe_lists_sub_children(state):
    db, store = state
    child(db, store, "lead", role="orchestrator")
    for i in range(3):
        child(db, store, f"worker-{i}", parent="lead")
    _, token = store.acquire("lead", "worker-0", "busy")
    store.close("lead", "worker-2")
    listed = store.list("parent")
    assert [r["session_id"] for r in listed] == ["lead"]  # d=2 never flattens
    assert listed[0]["children"]["active"] == 1 and listed[0]["children"]["total"] == 3
    observed = store.observe("parent", "lead")
    assert {c["session_id"]: c["status"] for c in observed["children"]} == {
        "worker-0": "running", "worker-1": "idle", "worker-2": "closed"}
    tree = store.list("parent", scope="tree")
    assert {r["session_id"]: r["depth_below_caller"] for r in tree} == {"lead": 1, "worker-0": 2, "worker-1": 2}
    siblings = store.list("worker-1", scope="siblings")
    assert {r["session_id"] for r in siblings} == {"worker-0"}
    store.release("worker-0", token)


def test_acl_denies_non_descendants_and_admits_delivery_targets(state):
    from agent import session_directory as directory
    db, store = state
    child(db, store, "mine")
    db.create_session("stranger", "cli")
    child(db, store, "theirs", parent="stranger")
    _webhook(db, "inbox", target="parent", attach=False)
    for sid in ("theirs",):
        with pytest.raises(DelegateSessionError):
            store.get("parent", sid)
        with pytest.raises(DelegateSessionError):
            store.admit("parent", sid, "hijack")
    assert store.get("parent", "inbox")["lineage_kind"] == "webhook"  # main may address what delivers to it
    with pytest.raises(DelegateSessionError):
        store.get("stranger", "inbox")
    # The ACL is pluggable (worker C exposes it on ctx.sessions).
    previous = directory.set_session_acl(lambda db_, conn, caller, row: row["id"] == "theirs")
    try:
        assert store.get("parent", "theirs")["session_id"] == "theirs"
        with pytest.raises(DelegateSessionError):
            store.get("parent", "mine")
    finally:
        directory.set_session_acl(previous)


def test_main_resumes_a_webhook_session_delivering_to_it_through_its_owner_sink(state):
    from gateway import delivery_targets as dt
    from tools.delegate_sessions import resume
    from types import SimpleNamespace
    db, store = state
    _webhook(db, "inbox", target="parent", attach=True)
    got = []

    def deliver(db_, row, target, event, *, wake_policy=None, request_id=None):
        got.append((row["id"], event.kind, event.sender_kind, event.sender_id, event.text))
        return dt.DeliveryReceipt("session", True, "test", row["id"], "adm-1")
    dt.register_sink("test-owner", lambda db_, row, target: row["id"] == "inbox", deliver)
    try:
        result = json.loads(resume(SimpleNamespace(session_id="parent"), store, store.get("parent", "inbox"),
                                   "Reply to the thread", None, False))
    finally:
        dt.unregister_sink("test-owner")
    assert result["status"] == "queued" and got == [
        ("inbox", "parent_message", "parent", "session:parent", "Reply to the thread")]


def test_legacy_delegate_rows_are_read_through_the_one_way_shim(state):
    db, store = state
    db.create_session("old-child", "subagent", parent_session_id="parent", model_config={"_delegate_from": "parent"})
    db._write_sql("CREATE TABLE delegate_sessions (session_id TEXT PRIMARY KEY, owner_session_id TEXT, title TEXT, "
                  "initial_goal TEXT, assignment TEXT, context TEXT, status TEXT, model TEXT, provider TEXT, role TEXT, "
                  "workspace TEXT, forked_context INTEGER, created_at REAL)")
    db._write_sql("INSERT INTO delegate_sessions VALUES ('old-child', 'parent', 'Legacy', 'Old goal', 'Old goal', '', "
                  "'idle', 'm', 'p', 'leaf', NULL, 0, 1.0)")
    listed = store.list("parent")
    assert [(r["session_id"], r["title"], r["status"]) for r in listed] == [("old-child", "Legacy", "idle")]
    assert db._read_one("SELECT lineage_kind FROM sessions WHERE id = 'old-child'")[0] is None  # never written


# ── gateway typing of a delivered child event ────────────────────────────────

def test_gateway_types_a_delivered_completion_with_the_childs_provenance():
    from types import SimpleNamespace
    from gateway.config import Platform
    from gateway.run_notifications import _watch_notification_metadata
    from gateway.session_automation import automation_sender_metadata, restore_sender_metadata
    from gateway.typed_input import turn_typed_input
    source = SimpleNamespace(platform=Platform.DISCORD, chat_id="c", user_id="u", thread_id=None)
    evt = {"type": "async_delegation", "delivery": {"target_session_id": "main", "sender_kind": "child",
                                                     "sender_id": "child-1", "kind": "subagent_complete",
                                                     "meta": {"children": ["child-1"]}}}
    metadata = _watch_notification_metadata(evt, source)
    stored = automation_sender_metadata(metadata)
    restored = restore_sender_metadata(stored)
    event = SimpleNamespace(internal=True, metadata=restored, message_id="m1")
    typed = turn_typed_input(event, source, "Found 3 issues", 1.0, "internal_notification")
    assert (typed["sender_kind"], typed["sender_id"], typed["part_kind"]) == ("child", "child-1", "subagent_complete")
    with pytest.raises(Exception):
        automation_sender_metadata({"typed_sender": {"sender_kind": "human", "sender_id": "x", "kind": "k"}})


# ── end to end: real children, only the model call replaced ──────────────────

@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    cfg = {"compression": {"enabled": False}, "terminal": {"cwd": str(tmp_path)},
           "delegation": {"fallback_providers": [{"provider": "backup", "model": "backup-model"}]}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda *a, **kw: cfg)
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    from agent.turn_context import build_turn_context
    from run_agent import AIAgent
    observed, children = [], []

    def run_conversation(agent, user_message, conversation_history=None, task_id=None, **kwargs):
        from agent import conversation_loop as loop
        ctx = build_turn_context(
            agent, user_message, None, conversation_history, task_id, None, None,
            restore_or_build_system_prompt=loop._restore_or_build_system_prompt,
            install_safe_stdio=loop._install_safe_stdio, sanitize_surrogates=loop._sanitize_surrogates,
            summarize_user_message_for_log=loop._summarize_user_message_for_log,
            set_session_context=loop.set_session_context, set_current_write_origin=loop.set_current_write_origin,
            ra=loop._ra, typed_input=kwargs.get("typed_input"))
        observed.append({"session_id": agent.session_id, "history": deepcopy(ctx.messages), "model": agent.model,
                         "fallback": deepcopy(getattr(agent, "_fallback_chain", None)), "typed": kwargs.get("typed_input")})
        children.append(agent)
        ctx.messages.append({"role": "assistant", "content": f"Done: {user_message}"[:80], "_assistant_origin": "model"})
        agent._flush_messages_to_session_db(ctx.messages, conversation_history)
        return {"final_response": f"Done: {user_message}"[:80], "completed": True, "messages": ctx.messages, "api_calls": 0}

    monkeypatch.setattr(AIAgent, "run_conversation", run_conversation)

    def credentials(route, parent):
        return {"provider": route.get("provider") or "custom", "model": route.get("model"),
                "base_url": "http://127.0.0.1:1/v1", "api_key": "test-key", "api_mode": "chat_completions"}
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", credentials)
    monkeypatch.setattr("tools.delegate_tool_dispatch._resolve_async_wake_sid", lambda *args, **kwargs: None)
    with patch("agent.process_bootstrap.OpenAI"):
        db = SessionDB(db_path=tmp_path / "state.db")

        def parent(session_id="owner"):
            agent = AIAgent(api_key="test-key", base_url="http://127.0.0.1:1/v1", model="test/model", provider="custom",
                            enabled_toolsets=[], quiet_mode=True, skip_context_files=True, skip_memory=True,
                            session_db=db, session_id=session_id)
            agent._ensure_db_session()
            return agent
        yield parent, db, observed, children
        for agent in children:
            agent.close()
        db.close()


def test_spawn_resume_and_list_run_through_admissions_end_to_end(harness):
    from agent.subagent_lifecycle import SubagentLifecycleService
    from tools.delegate_tool import delegate_task
    make_parent, db, observed, _ = harness
    parent = make_parent()
    service = SubagentLifecycleService(lambda: parent)
    first = service.launch_session(goal="Investigate the first issue", title="Review", fork_context=False)
    sid = first["results"][0]["session_id"]
    assert first["results"][0]["status"] == "completed", first
    spawn_rows = list_session_admissions(db, session_id=sid, pending_only=False)
    assert [(r["status"], r["outcome"], r["payload"]["text"]) for r in spawn_rows] == [
        ("terminal", "completed", "Investigate the first issue")]
    row = session_row(db, sid)
    assert row["lineage_kind"] == "delegate" and json.loads(row["delivery_target"])["session_id"] == parent.session_id
    assert json.loads(row["route"]).get("fallback_providers") == [{"provider": "backup", "model": "backup-model"}]
    parent.close()
    parent = make_parent()
    second = json.loads(delegate_task(action="resume", session_id=sid, message="Now check the regression",
                                      parent_agent=parent))
    assert second["results"][0]["status"] == "completed", second
    assert observed[1]["session_id"] == sid
    assert observed[1]["fallback"] == observed[0]["fallback"] and observed[1]["fallback"]  # chain survived resume
    assert [r["outcome"] for r in list_session_admissions(db, session_id=sid, pending_only=False)] == [
        "completed", "completed"]
    roster = json.loads(delegate_task(action="list", parent_agent=parent))
    assert [(r["session_id"], r["status"], r["assignment"]) for r in roster["subagents"]] == [
        (sid, "idle", "Now check the regression")]
    observed_child = json.loads(delegate_task(action="observe", session_id=sid, parent_agent=parent))
    assert observed_child["session"]["session_id"] == sid and observed_child["children"] == []
    closed = json.loads(delegate_task(action="close", session_id=sid, parent_agent=parent))
    assert closed["status"] == "closed"
    refused = json.loads(delegate_task(action="resume", session_id=sid, message="Try again", parent_agent=parent))
    assert "closed" in refused["error"].lower()
    parent.close()


def test_idle_steer_runs_as_the_next_turn_through_the_fifo(harness):
    import time
    from agent.subagent_lifecycle import SubagentLifecycleService
    from tools.delegate_tool import delegate_task
    make_parent, db, observed, _ = harness
    parent = make_parent()
    sid = SubagentLifecycleService(lambda: parent).launch_session(goal="Draft", fork_context=False)["results"][0]["session_id"]
    parent._delegate_depth = 1  # an orchestrator parent: the drain runs in-process and delivers to its FIFO
    child_row = session_row(db, parent.session_id)
    db._write_sql("UPDATE sessions SET lineage_kind = 'delegate' WHERE id = ?", (parent.session_id,))
    reply = json.loads(delegate_task(action="steer", session_id=sid, message="Use the newer API", parent_agent=parent))
    assert reply["status"] == "queued_next_turn"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and len(observed) < 2:
        time.sleep(0.05)
    assert len(observed) == 2 and "Use the newer API" in json.dumps(observed[1]["history"])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not list_session_admissions(db, session_id=parent.session_id):
        time.sleep(0.05)
    delivered = list_session_admissions(db, session_id=parent.session_id)
    assert delivered and delivered[0]["principal_id"] == f"child:{sid}"
    assert child_row["id"] == parent.session_id
    parent.close()


def test_sessions_api_admits_typed_events_within_the_acl_only(state):
    """The ctx.sessions surface (agent/sessions_api.py): typed peer/parent events, never human, ACL-gated."""
    from agent.sessions_api import SessionsAPI
    from gateway.delivery_targets import event_from_payload
    db, store = state
    child(db, store, "a")
    child(db, store, "b")
    api = SessionsAPI(db)
    receipt = api.admit("parent", "a", "Look at module X", sender_kind="parent", kind="parent_message")
    assert receipt.delivered
    event = event_from_payload(list_session_admissions(db, session_id="a")[0]["payload"])
    assert (event["sender_kind"], event["sender_id"], event["kind"]) == ("parent", "parent", "parent_message")
    with pytest.raises(LookupError):  # siblings are not reachable under the default ACL
        api.admit("a", "b", "hi")
    assert [r["session_id"] for r in api.list("parent")] and api.observe("parent", "a")["session"]["session_id"] == "a"
