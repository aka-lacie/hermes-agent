"""Materialized delegate-list recency (re-review R8): ``sessions.conversation_activity_at`` is an upper bound of
the list's exact order key on every write path, the compression family a continuation joins is its chain's,
and the offline unified migration folds both in idempotently."""

import hashlib
import json
import sqlite3
import time

from hermes_state import SessionDB
from hermes_state_unified_migration import Options, run
from tests.hermes_state.test_unified_migration import NOW, legacy_db  # noqa: F401  (pytest fixture)


def _violations(db) -> list:
    """Rows whose bound is below the exact list key the directory computes (must be none)."""
    from agent import session_directory as directory
    with db._read_ctx() as conn:
        rows = conn.execute("SELECT * FROM sessions").fetchall()
        exact = {info["session_id"]: info["last_activity_at"] for info in directory.hydrate(conn, rows)}
    return [(row["id"], row["conversation_activity_at"], exact[row["id"]]) for row in rows
            if row["conversation_activity_at"] < exact[row["id"]]]


def test_api_writes_keep_the_bound_and_the_family(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        t0 = time.time() + 3600  # after every started_at below: each write must raise the bound itself
        db.create_session("main", "cli")
        brief = {"_delegate_from": "main"}
        for child in ("a", "b"):
            db.create_session(child, "subagent", parent_session_id="main", model_config=brief)
            db._write_sql("UPDATE sessions SET lineage_kind = 'delegate', status = 'idle' WHERE id = ?", (child,))
        db.append_message("a", "user", "first", timestamp=t0)
        db.append_messages_batch("b", [{"role": "user", "content": "batch", "timestamp": t0 + 5}])
        db.end_session("a", "compression")  # the split: a continuation arrives after
        db.create_session("a2", "subagent", parent_session_id="a", model_config=brief)
        db.append_message("a2", "user", "continued", timestamp=t0 + 10)
        db.touch_session_activity("b", ts=t0 + 20)
        rows = {r["id"]: r for r in db._conn.execute("SELECT * FROM sessions")}
        assert rows["a2"]["conversation_root_id"] == rows["a"]["conversation_root_id"] == "a"
        assert rows["a"]["conversation_activity_at"] == t0 + 10  # the tip's message raised its root
        assert rows["b"]["conversation_activity_at"] == t0 + 20  # mid-turn activity
        assert rows["main"]["conversation_activity_at"] >= rows["main"]["started_at"]
        assert _violations(db) == []
        db.end_session("b", "done")
        db._write_sql("UPDATE sessions SET status = 'closed' WHERE id = 'b'")
        assert db._conn.execute("SELECT list_closed FROM sessions WHERE id = 'b'").fetchone()[0] == 1
        from tools.delegate_session_store import DelegateSessionStore
        assert [r["session_id"] for r in DelegateSessionStore(db).list("main", limit=5)] == ["a"]
    finally:
        db.close()


def _logical(path) -> str:
    conn = sqlite3.connect(path)
    try:
        digest = hashlib.sha256()
        for table, key in (("sessions", "id"), ("messages", "id")):
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {key}"):
                digest.update(repr(row).encode())
        return digest.hexdigest()
    finally:
        conn.close()


def test_the_offline_migration_folds_recency_in_and_repeats_as_a_noop(legacy_db):  # noqa: F811
    receipt = run(Options(db=legacy_db, apply=True, scope="all", now=NOW))
    assert receipt["mode"] == "apply"
    db = SessionDB(db_path=legacy_db)
    try:
        assert _violations(db) == []
        roots = dict(db._conn.execute("SELECT id, conversation_root_id FROM sessions"))
        assert roots["child2"] == roots["child"] == "child"  # the migrated retained child's compression chain
        assert roots["dm2"] == roots["dm"] and roots["grand"] == "grand"  # a delegate spawn is its own family
    finally:
        db.close()
    before = _logical(legacy_db)
    again = run(Options(db=legacy_db, apply=True, scope="all", now=NOW))
    assert again["message_changes_total"] == 0 and again["sessions"]["sessions_changed"] == 0
    assert _logical(legacy_db) == before


def test_bound_columns_are_declared_and_their_triggers_installed(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        columns = {r[1] for r in db._conn.execute("PRAGMA table_info(sessions)")}
        assert {"conversation_root_id", "conversation_activity_at", "list_closed"} <= columns
        names = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%recency%'")}
        assert {"idx_sessions_parent_recency", "idx_sessions_root_recency", "messages_list_recency_insert",
                "sessions_list_recency_insert", "sessions_list_recency_split", "sessions_list_recency_join",
                "sessions_list_recency_closed", "sessions_list_recency_floor"} <= names
        # The admission hot path carries no list trigger (running overrides are read back by index).
        on_admissions = [r[0] for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'session_admissions'")]
        assert on_admissions == [], on_admissions
        assert json.loads(json.dumps(sorted(names)))
    finally:
        db.close()
