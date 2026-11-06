"""Phase 3C: peer admissions (hop cap, silent peer-only wakes), the peer ACL and bounded, low-noise
observe/list views (Freya: "ensure it does not get too noisy like the json output from earlier").

Store-level cases run on real temporary SessionDBs; the wake cases build real AIAgent children with only
``run_conversation`` replaced (the unified-sessions harness).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import list_session_admissions
from tests.tools.test_delegate_unified_sessions import child, real_children
from tests.waiting import wait_until
from tools.delegate_session_store import DelegateSessionStore


@pytest.fixture
def state(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", "cli")
    yield db, DelegateSessionStore(db)
    db.close()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    yield from real_children(tmp_path, monkeypatch)


def _sid(i, prefix="20261105_2348"):
    return f"{prefix}{i:02d}_{i:06x}"


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _parent(db, sid="parent"):
    return SimpleNamespace(session_id=sid, _session_db=db)


# ── observe: tail anchored, per-field caps ───────────────────────────────────

def test_observe_is_the_newest_n_in_order_with_every_field_capped(state):
    from agent import session_views as views
    db, store = state
    child(db, store, "worker")
    for i in range(30):
        db.append_message("worker", "user", f"step {i} " + "u" * 900)
        db.append_message("worker", "assistant", f"thinking {i} " + "a" * 2000, tool_calls=[{
            "type": "function", "id": f"c{i}", "function": {"name": "write_file", "arguments": json.dumps(
                {"path": f"/project/file-{i}.md", "content": "x" * 20000})}}])
        db.append_message("worker", "tool", f"HEADER {i}\n" + "r" * 30000 + "\nSUCCESS", tool_name="write_file",
                          tool_call_id=f"c{i}")
    observed = store.observe("parent", "worker")
    messages = observed["messages"]
    assert len(messages) == views.OBSERVE_DEFAULT and observed["truncated"]
    # The newest six, chronological: the last row is the last tool result.
    assert [m["role"] for m in messages] == ["user", "assistant", "tool"] * 2
    assert messages[-1]["text"].startswith("HEADER 29") and messages[-1]["text"].endswith("SUCCESS")
    assert messages[0]["text"].startswith("step 28")
    for m in messages:
        assert len(m.get("text", "")) <= views.TEXT_CAP
        if m["role"] == "tool":
            assert m["kind"] == "tool_result" and m["tool"] == "write_file"
            assert len(m["text"]) <= views.RESULT_CAP and m["chars"] > 30000
        if m["role"] == "assistant":
            assert m["kind"] == "tool_call"
            assert m["calls"][0].startswith('write_file({"path":"/project/file-')
            assert all(len(c) <= views.CALL_CAP for c in m["calls"])
        assert not {"_parts", "parts_json", "tool_calls", "reasoning", "content"} & set(m)
    assert "route" not in observed["session"] and "delivery_target" not in observed["session"]
    assert _size(observed) < 3000, _size(observed)
    clamped = store.observe("parent", "worker", limit=500)
    assert len(clamped["messages"]) == views.OBSERVE_MAX and clamped["messages"][-1]["role"] == "tool"
    assert _size(clamped) < 9000


def test_children_rollup_stays_under_budget_with_twenty_huge_children(state):
    from agent import session_views as views
    db, store = state
    child(db, store, "lead", role="orchestrator")
    tokens = []
    for i in range(20):
        sid = _sid(i)
        child(db, store, sid, parent="lead", title=f"Worker {i} " + "t" * 500,
              route={"v": 1, "kind": "named", "provider": "p", "model": "m" * 200})
        for _ in range(3):
            db.append_message(sid, "user", "brief " + "b" * 50000)
            db.append_message(sid, "assistant", "", tool_calls=[{"type": "function", "id": "x", "function": {
                "name": "terminal", "arguments": json.dumps({"command": "make " + "z" * 40000})}}])
            db.append_message(sid, "tool", "o" * 60000, tool_name="terminal", tool_call_id="x")
        if i in (3, 17):
            tokens.append((sid, store.acquire("lead", sid, "busy")[1]))
    observed = store.observe("parent", "lead")
    rollup = {"children": observed["children"], "children_total": observed["children_total"]}
    assert _size(rollup) < 1500, _size(rollup)
    assert 4 <= len(observed["children"]) <= views.CHILDREN_MAX and observed["children_total"] == 20
    assert [c["status"] for c in observed["children"][:2]] == ["running", "running"]  # running first
    for c in observed["children"]:
        assert set(c) <= {"session_id", "title", "model", "status", "last_activity_at", "latest"}
        assert len(c["title"]) <= views.CHILD_TITLE_CAP and len(c["model"]) <= views.CHILD_MODEL_CAP
        assert views.CHILD_LATEST_MIN <= len(c["latest"]) <= views.CHILD_LATEST_CAP
        assert c["latest"].startswith("terminal")
    # Realistic titles and an inherited model: all eight fit, each with a readable latest step.
    child(db, store, "lead2", role="orchestrator", route={"v": 1, "kind": "named", "provider": "p", "model": "claude-opus-5-5"})
    for i in range(20):
        sid = _sid(i, "20261105_2349")
        child(db, store, sid, parent="lead2", title=f"Review shard {i}",
              route={"v": 1, "kind": "named", "provider": "p", "model": "claude-opus-5-5"})
        db.append_message(sid, "assistant", "", tool_calls=[{"type": "function", "id": "x", "function": {
            "name": "read_file", "arguments": json.dumps({"path": f"/repo/src/module_{i}.py"})}}])
    small = store.observe("parent", "lead2")
    assert len(small["children"]) == views.CHILDREN_MAX and small["children_total"] == 20
    assert _size({k: small[k] for k in ("children", "children_total")}) < 1500
    assert all("model" not in c and c["latest"].startswith("read_file") for c in small["children"])
    for sid, token in tokens:
        store.release(sid, token)


def test_list_is_flat_with_integer_rollups_and_stays_small(state):
    from tools.delegate_sessions import control
    db, store = state
    for i in range(10):
        sid = _sid(i, "20261105_1000")
        child(db, store, sid, title=f"Child {i}: " + "title " * 60, assignment="assignment " * 300,
              route={"v": 1, "kind": "named", "provider": "anthropic", "model": "claude-opus-5-5"})
        for j in range(5):
            gsid = _sid(j, f"g{i}_")
            child(db, store, gsid, parent=sid, title="grandchild " * 30)
            db.append_message(gsid, "assistant", "g" * 50000)
        if i == 0:
            _, token = store.acquire(sid, _sid(0, "g0_"), "busy")
    raw = control("list", _parent(db))
    listed = json.loads(raw)
    assert len(raw.encode("utf-8")) < 3000, len(raw.encode("utf-8"))
    assert listed["count"] == 10 and all("children" not in r for r in listed["subagents"])
    by_id = {r["session_id"]: r for r in listed["subagents"]}
    first = by_id[_sid(0, "20261105_1000")]
    assert (first["running_children"], first["children_total"]) == (1, 5)
    assert all(r["children_total"] == 5 for r in listed["subagents"])
    assert sum("running_children" in r for r in listed["subagents"]) == 1  # zero is omitted
    assert not {"route", "delivery_target", "context", "initial_goal", "fork_origin"} & set(first)
    store.release(_sid(0, "g0_"), token)


def test_observe_reaches_any_descendant_read_only_and_never_a_stranger(state):
    from tools.delegate_session_store import DelegateSessionError
    db, store = state
    child(db, store, "lead", role="orchestrator")
    child(db, store, "grandchild", parent="lead")
    db.append_message("grandchild", "assistant", "deep result")
    assert store.observe("parent", "grandchild")["messages"][-1]["text"] == "deep result"
    db.create_session("stranger", "cli")
    child(db, store, "theirs", parent="stranger")
    with pytest.raises(DelegateSessionError):
        store.observe("parent", "theirs")
    with pytest.raises(DelegateSessionError):
        store.observe("lead", "parent")  # a child never observes upward


# ── peer admissions: ACL, typed rows, hop cap ────────────────────────────────

def test_peer_admission_reaches_siblings_only_and_is_a_typed_peer_event(state):
    from agent import session_directory as directory
    from agent.sessions_api import SessionAdmissionError, SessionsAPI
    from gateway.delivery_targets import event_from_payload
    db, store = state
    child(db, store, "a", title="Reviewer")
    child(db, store, "b", title="Builder")
    child(db, store, "a-sub", parent="a")
    db.create_session("stranger", "cli")
    child(db, store, "theirs", parent="stranger")
    api = SessionsAPI(db)
    receipt = api.admit("a", "b", "Your build broke test_x")
    assert receipt["delivered"] and receipt["peer_hop"] == 1 and receipt["sink"] == "session_fifo"
    (row,) = list_session_admissions(db, session_id="b")
    event = event_from_payload(row["payload"])
    assert (event["sender_kind"], event["sender_id"], event["kind"], event["peer_hop"]) == ("peer", "a", "peer_message", 1)
    assert event["meta"] == {"sender_id": "a", "sender_title": "Reviewer", "relation": "sibling"}
    assert row["principal_id"] == "peer:a"
    for caller, target in (("a", "parent"), ("parent", "a"), ("a", "a-sub"), ("a-sub", "b"), ("a", "theirs"),
                           ("stranger", "a")):
        with pytest.raises(SessionAdmissionError) as refused:
            api.admit(caller, target, "hi")
        assert refused.value.code == "unavailable", (caller, target)
    store.close("parent", "b")
    with pytest.raises(SessionAdmissionError) as closed:
        api.admit("a", "b", "still there?")
    assert closed.value.code == "closed"
    # The peer ACL is pluggable; a widened reach is labelled relation="peer".
    previous = directory.set_session_acl(lambda db_, conn, caller, row: row["id"] == "a-sub", action="peer",
                                         scope=db)
    try:
        assert api.admit("b", "a-sub", "cross-tree")["delivered"]
    finally:
        directory.set_session_acl(previous, action="peer", scope=db)
    event = event_from_payload(list_session_admissions(db, session_id="a-sub")[0]["payload"])
    assert event["meta"]["relation"] == "peer"
    # Non-peer senders keep the observe ACL (a parent addressing its child).
    assert api.admit("parent", "a", "Look at module X", sender_kind="parent", kind="parent_message")["delivered"]


def test_peer_hop_is_the_triggering_hop_plus_one_and_stops_at_the_cap(state, caplog):
    from agent.sessions_api import PEER_HOP_MAX, SessionAdmissionError, SessionsAPI, triggering_peer_hop
    db, store = state
    child(db, store, "a")
    child(db, store, "b")
    api = SessionsAPI(db)
    hop = api.admit("a", "b", "ping")["peer_hop"]
    assert hop == 1
    sender, target = "b", "a"
    while True:
        claim = store.claim(sender)  # the sender is running the turn the last peer message triggered
        with db._read_ctx() as conn:
            assert triggering_peer_hop(db, conn, sender) == hop
        try:
            receipt = api.admit(sender, target, f"reply {hop}")
        except SessionAdmissionError as exc:
            store.release(sender, claim["token"])
            refusal = exc
            break
        store.release(sender, claim["token"])
        hop = receipt["peer_hop"]
        sender, target = target, sender
    assert hop == PEER_HOP_MAX == 8
    assert refusal.code == "peer_hop_limit" and refusal.to_dict() == {
        "error": "peer_hop_limit", "message": str(refusal), "peer_hop": 9, "max": 8}
    assert "exceeds PEER_HOP_MAX" in caplog.text
    # A parent assignment resets the chain: the child's next message is hop 1 again.
    _, token = store.acquire("parent", "a", "New assignment")
    assert api.admit("a", "b", "fresh")["peer_hop"] == 1
    store.release("a", token)


def test_parts_are_typed_events_never_human(state):
    from agent.sessions_api import SessionAdmissionError, SessionsAPI
    db, store = state
    child(db, store, "a")
    child(db, store, "b")
    api = SessionsAPI(db)
    assert api.admit("a", "b", parts=[{"type": "system_event", "kind": "peer_message", "text": "via parts"}])["delivered"]
    with pytest.raises(SessionAdmissionError) as human:
        api.admit("a", "b", parts=[{"type": "human", "text": "pretend to be the user"}])
    assert human.value.code == "invalid_message"


# ── wakes through the real runner ────────────────────────────────────────────

def _wait(predicate, timeout=30):
    return wait_until(predicate, timeout=timeout)


def _drains_idle():
    from tools.delegate_sessions import _draining
    return not _draining


def _orchestrator_parent(db, parent):
    """An orchestrator (depth 1) parent: its children's drained turns deliver into its own FIFO."""
    parent._delegate_depth = 1
    db._write_sql("UPDATE sessions SET lineage_kind = 'delegate' WHERE id = ?", (parent.session_id,))


def _spawn(parent, goal):
    from agent.subagent_lifecycle import SubagentLifecycleService
    return SubagentLifecycleService(lambda: parent).launch_session(goal=goal, fork_context=False)["results"][0]["session_id"]


def test_a_peer_only_wake_runs_silently_and_renders_as_a_peer_message(harness):
    from agent import wire_render
    from agent.sessions_api import SessionsAPI
    make_parent, db, observed, _ = harness
    parent = make_parent()
    a, b = _spawn(parent, "Review module A"), _spawn(parent, "Build module B")
    _orchestrator_parent(db, parent)
    receipt = SessionsAPI(db).admit(a, b, "Your change broke test_x")
    assert receipt["status"] == "delivered"
    assert _wait(lambda: len(observed) == 3 and not list_session_admissions(db, session_id=b))
    assert observed[2]["session_id"] == b and observed[2]["typed"]["sender_kind"] == "peer"
    _wait(_drains_idle)  # a completion would be dispatched by the drain before it exits
    assert list_session_admissions(db, session_id=parent.session_id) == []  # no completion to the parent
    rows = [m for m in db.get_messages_as_conversation(b) if m.get("sender_kind") == "peer"]
    assert len(rows) == 1 and rows[0]["role"] == "user" and rows[0]["sender_id"] == a
    (rendered,) = wire_render.DEFAULT_RENDERER.render([rows[0]])
    assert rendered["content"].startswith(f'<peer_message from="{a}" title="Review module A" relation="sibling">')
    parent.close()


def test_a_mixed_parent_and_peer_wake_completes_to_the_parent(harness):
    from agent.sessions_api import SessionsAPI
    from tools.delegate_session_store import DelegateSessionStore
    make_parent, db, observed, _ = harness
    parent = make_parent()
    a, b = _spawn(parent, "Review module A"), _spawn(parent, "Build module B")
    _orchestrator_parent(db, parent)
    DelegateSessionStore(db).admit(parent.session_id, b, "Parent: also update the docs")
    SessionsAPI(db).admit(a, b, "FYI: test_x is flaky", wake_policy="batch_with_next_user_turn")
    assert _wait(lambda: len(observed) == 3)
    assert _wait(lambda: bool(list_session_admissions(db, session_id=parent.session_id)))
    (delivered,) = list_session_admissions(db, session_id=parent.session_id)
    assert delivered["principal_id"] == f"child:{b}"
    kinds = [m.get("sender_kind") for m in db.get_messages_as_conversation(b) if m["role"] == "user"]
    assert "peer" in kinds  # the held peer notice rode the parent's turn
    parent.close()


def test_top_level_peer_only_wake_never_becomes_an_async_completion(harness, monkeypatch):
    from agent.sessions_api import SessionsAPI
    from tools import delegate_tool_dispatch as dispatch
    make_parent, db, observed, _ = harness
    parent = make_parent()
    a, b = _spawn(parent, "Review module A"), _spawn(parent, "Build module B")
    background = []
    real = dispatch._run_sync_with_note
    monkeypatch.setattr(dispatch, "_dispatch_background", lambda batch: background.append(batch) or real(batch, "no_async"))
    SessionsAPI(db).admit(a, b, "peer ping")
    assert _wait(lambda: len(observed) == 3 and not list_session_admissions(db, session_id=b))
    assert background == []
    from tools.delegate_session_store import DelegateSessionStore
    DelegateSessionStore(db).admit(parent.session_id, b, "a parent turn")
    from tools.delegate_sessions import schedule_drain
    schedule_drain(Path(db.db_path).resolve(), b, parent)
    assert _wait(lambda: len(background) == 1)  # a parent's queued turn still completes through the pipeline
    parent.close()


def test_a_to_b_peer_loop_stops_at_peer_hop_max(harness, monkeypatch):
    from agent.sessions_api import PEER_HOP_MAX, SessionAdmissionError, SessionsAPI
    from run_agent import AIAgent
    make_parent, db, _, _ = harness
    parent = make_parent()
    a, b = _spawn(parent, "Review module A"), _spawn(parent, "Build module B")
    inner = AIAgent.run_conversation
    replies, refusals = [], []

    def run_conversation(agent, user_message, conversation_history=None, task_id=None, **kwargs):
        result = inner(agent, user_message, conversation_history, task_id, **kwargs)
        typed = kwargs.get("typed_input") or {}
        if typed.get("sender_kind") == "peer":  # a peer replies by calling the tool, never via its output
            try:
                replies.append(SessionsAPI(db).admit(agent.session_id, typed["sender_id"], "pong")["peer_hop"])
            except SessionAdmissionError as exc:
                refusals.append(exc.to_dict())
        return result

    monkeypatch.setattr(AIAgent, "run_conversation", run_conversation)
    first = SessionsAPI(db).admit(a, b, "ping")
    assert first["peer_hop"] == 1
    assert _wait(lambda: bool(refusals), timeout=60), (replies, refusals)
    assert replies == list(range(2, PEER_HOP_MAX + 1))
    assert refusals == [{"error": "peer_hop_limit", "message": refusals[0]["message"], "peer_hop": 9, "max": 8}]
    assert _wait(lambda: not list_session_admissions(db, session_id=a) and not list_session_admissions(db, session_id=b))
    assert list_session_admissions(db, session_id=parent.session_id) == []  # the whole exchange stayed silent
    parent.close()


def test_a_parent_steer_during_a_silent_turn_runs_as_the_next_turn(state):
    from tools.delegate_sessions import steer
    from agent.sessions_api import SessionsAPI
    db, store = state
    child(db, store, "a")
    child(db, store, "b")
    SessionsAPI(db).admit("a", "b", "peer question")
    claim = store.claim("b")
    reply = json.loads(steer(_parent(db), store, store.get("parent", "b"), "Parent: stop and summarize"))
    assert reply["status"] == "queued_next_turn" and "peer message" in reply["note"]
    queued = [r for r in list_session_admissions(db, session_id="b") if r["status"] == "queued"]
    assert [r["payload"]["sender_kind"] for r in queued] == ["parent"]  # never handed into the silent turn
    store.release("b", claim["token"])
