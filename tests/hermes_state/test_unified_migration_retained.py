"""Review R1: a migrated retained delegate child round-trips through the unified runner.

The Phase 2f migrator and the Phase 3 reader/runner must share one post-migration contract: after
``run(apply=True)`` on a legacy store (``delegate_sessions`` rows), the unified ``DelegateSessionStore`` reads
the child's brief/retain/target from the sessions row alone, admits work on its canonical id (a compressed
child stays idle, not ``closed``), resumes it on a valid stored route, settles the run and delivers the child
completion to the parent's FIFO. Both a compressed and an uncompressed legacy child are covered, and a second
apply still changes nothing.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import list_session_admissions
from hermes_state_unified_migration import run
from tests.hermes_state.test_unified_migration import _opts, _rows, legacy_db  # noqa: F401  (fixture)
from tools.delegate_session_store import DelegateSessionStore

ROUTE = {"v": 1, "kind": "named", "provider": "cliproxy-claude", "model": "claude-opus-5-5"}


def _uncompress(path):
    """The fixture's child as a never-compressed retained child: its own row is the tip."""
    conn = sqlite3.connect(path)
    conn.execute("UPDATE delegate_sessions SET current_session_id = 'child', status = 'idle' WHERE session_id = 'child'")
    conn.execute("UPDATE sessions SET end_reason = NULL, ended_at = NULL WHERE id = 'child'")
    conn.commit()
    conn.close()


@pytest.fixture(params=["compressed", "uncompressed"])
def migrated(request, legacy_db):  # noqa: F811
    if request.param == "uncompressed":
        _uncompress(legacy_db)
    receipt = run(_opts(legacy_db, apply=True))
    db = SessionDB(db_path=legacy_db)
    yield request.param, legacy_db, db, receipt
    db.close()


def test_migrated_retained_child_keeps_its_unified_record(migrated):
    shape, path, db, _ = migrated
    record = DelegateSessionStore(db).get("dm2", "child")
    assert record["current_session_id"] == ("child2" if shape == "compressed" else "child")
    assert (record["title"], record["initial_goal"], record["context"]) == ("t", "g", "c")
    assert record["retained"] is True and record["lineage_kind"] == "delegate"
    assert record["status"] == "idle"  # the canonical id of a compressed child is not closed
    assert record["delivery_target"] == {"kind": "session", "session_id": "dm2", "attach": True}
    assert record["route"] == ROUTE
    assert (record["workspace"], record["role"], record["forked_context"]) == ("/ws", "leaf", True)
    [row] = _rows(path, "SELECT model_config, status FROM sessions WHERE id = 'child'")
    brief = json.loads(row["model_config"])["_delegate_brief"]
    assert brief == {"title": "t", "initial_goal": "g", "assignment": "a", "context": "c", "retain": True}
    assert json.loads(row["model_config"])["_delegate_from"] == "dm2"  # the marker is kept
    assert row["status"] == "idle"
    listed = DelegateSessionStore(db).list("dm2")
    assert [(r["session_id"], r["status"], r["retained"]) for r in listed] == [("child", "idle", True)]


def test_migrated_retained_child_resumes_settles_and_completes_to_its_parent(migrated, monkeypatch):
    from tools.delegate_tool import resumed_route
    _, _, db, _ = migrated
    store = DelegateSessionStore(db)
    admission = store.admit("dm2", "child", "continue the review")
    assert admission["status"] == "queued" and not admission["busy"]
    claim = store.claim("child")
    assert claim["admission_id"] == admission["admission_id"]
    record = store.get("dm2", "child")
    assert record["status"] == "running"

    monkeypatch.setattr("tools.delegate_tool_config._load_config", dict)
    resolved = {}

    def credentials(block, parent):
        resolved.update(block)
        return {"provider": block["provider"], "model": block["model"], "base_url": "http://127.0.0.1:1/v1",
                "api_key": "test-key", "api_mode": "chat_completions"}
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", credentials)
    route = resumed_route(SimpleNamespace(provider="cliproxy-gemini"), record)
    assert (resolved["provider"], resolved["model"]) == ("cliproxy-claude", "claude-opus-5-5")
    assert route.routing_cfg["provider"] == "cliproxy-claude"

    assert store.release("child", claim["token"])
    assert [r["outcome"] for r in list_session_admissions(db, session_id="child", pending_only=False)] == ["completed"]
    assert store.get("dm2", "child")["status"] == "idle"
    # The parent is a top-level human conversation: the child's completion travels its delivery_target to the
    # process completion consumer, addressed to the parent and typed as the child's (never the dispatch origin).
    from gateway.delivery_targets import child_completion_event, dispatch
    from tools.process_registry import process_registry
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    row = db._read_one("SELECT * FROM sessions WHERE id = ?", ("child",))
    receipt = dispatch(db, row, child_completion_event(
        row, {"status": "completed", "summary": "REVIEW DONE", "session_id": "child"},
        legacy_event={"type": "async_delegation", "summary": "REVIEW DONE"}))
    assert receipt.delivered and receipt.target_session_id == "dm2" and receipt.sink == "completion_queue"
    evt = process_registry.completion_queue.get_nowait()
    assert evt["parent_session_id"] == "dm2" and evt["summary"] == "REVIEW DONE"
    assert (evt["delivery"]["target_session_id"], evt["delivery"]["sender_kind"], evt["delivery"]["sender_id"],
            evt["delivery"]["kind"]) == ("dm2", "child", "child", "subagent_complete")


def test_migrated_child_of_a_delegate_parent_completes_into_the_parent_fifo(legacy_db):  # noqa: F811
    """An orchestrator parent (itself a delegate session) gets the completion as a typed FIFO admission."""
    from tools.delegate_sessions import _deliver_results
    conn = sqlite3.connect(legacy_db)  # the retained child's parent becomes a delegate conversation (d=1)
    conn.execute("UPDATE sessions SET model_config = ? WHERE id = 'dm2'", (json.dumps({"_delegate_from": "dm"}),))
    conn.execute("UPDATE sessions SET end_reason = 'cli_close' WHERE id = 'dm'")
    conn.commit()
    conn.close()
    run(_opts(legacy_db, apply=True))
    db = SessionDB(db_path=legacy_db)
    try:
        assert _rows(legacy_db, "SELECT lineage_kind FROM sessions WHERE id = 'dm2'")[0]["lineage_kind"] == "delegate"
        store = DelegateSessionStore(db)
        assert store.claim("child") is None  # nothing queued yet
        store.admit("dm2", "child", "continue")
        claim = store.claim("child")
        assert store.release("child", claim["token"])
        _deliver_results(db, "child", json.dumps({"results": [{"status": "completed", "summary": "REVIEW DONE"}]}))
        [delivered] = list_session_admissions(db, session_id="dm2")
        assert delivered["principal_id"] == "child:child" and delivered["status"] == "queued"
        assert "REVIEW DONE" in json.dumps(delivered["payload"])
    finally:
        db.close()


def test_second_apply_after_the_retained_fields_changes_nothing(migrated):
    _, path, _, receipt = migrated
    assert receipt["sessions"]["set_delegate_brief"] == 1
    assert receipt["sessions"]["set_delivery_target"] == 1
    before = _rows(path, "SELECT * FROM sessions ORDER BY id")
    again = run(_opts(path, apply=True))
    assert again["message_changes_total"] == 0 and again["sessions"]["sessions_changed"] == 0
    assert "set_delegate_brief" not in again["sessions"] and _rows(path, "SELECT * FROM sessions ORDER BY id") == before


def test_routes_go_only_to_delegate_conversations_and_always_validate(legacy_db):  # noqa: F811
    from agent.session_routes import validate_route
    run(_opts(legacy_db, apply=True))
    routes = {row["id"]: row["route"] for row in _rows(legacy_db, "SELECT id, route FROM sessions")}
    # grand is a delegate spawn too, but names no provider anywhere: no route (resume refuses, as before).
    assert {sid for sid, route in routes.items() if route} == {"child", "child2"}
    for sid in ("child", "child2"):
        validate_route(json.loads(routes[sid]))


def test_legacy_route_keeps_descriptor_identity_and_refuses_unnamed():
    from agent.session_routes import legacy_route
    digest = "ab" * 32
    assert legacy_route({"kind": "default", "endpoint_sha256": digest}, provider="custom", model="m") == {
        "v": 1, "kind": "default", "provider": "custom", "model": "m", "endpoint_sha256": digest}
    assert legacy_route({"kind": "parent", "endpoint_sha256": digest}, provider="openrouter", model="m")["kind"] == "parent"
    assert legacy_route(None, provider="Cliproxy-Claude", model="m") == {
        "v": 1, "kind": "named", "provider": "cliproxy-claude", "model": "m"}
    assert legacy_route(None, provider="custom", model="m") is None  # no saved identity: resume still refuses
    assert legacy_route({"kind": "named", "provider": "custom"}, model="m") is None
    assert legacy_route({"kind": "bogus"}, provider="x", model="m") is None
    assert legacy_route({"kind": "default", "endpoint_sha256": "not-a-digest"}, provider="custom", model="m") is None
    assert legacy_route(None, provider="p", model="m", reasoning_effort={"enabled": True, "effort": "high"})[
        "reasoning_effort"] == "high"
