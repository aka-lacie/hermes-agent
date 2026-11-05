"""Phase 2a additive schema: the unified session-model columns, their indexes, and the delegation-tree
topology every new sessions row is stamped with (``sessions_topology_*`` triggers).

Legacy rows are never backfilled here (the one-shot lineage migration owns that): a store reconciled
from an older shape keeps NULL topology on its existing rows, while rows created afterwards are stamped.
"""
import sqlite3

from hermes_state import SessionDB

_SESSION_COLUMNS = ("lineage_kind", "depth", "delivery_target", "route", "status", "workspace", "fork_origin",
                    "role", "root_session_id", "group_id")
_MESSAGE_COLUMNS = ("sender_kind", "sender_id", "sent_at", "source_event_id", "intent", "part_kind", "parts_json",
                    "turn_outcome")


def _columns(conn, table):
    return {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})")}


def _topology(db, sid):
    row = db._conn.execute("SELECT root_session_id, depth FROM sessions WHERE id = ?", (sid,)).fetchone()
    return tuple(row)


def test_fresh_store_declares_every_column_and_index(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        sessions, messages = _columns(db._conn, "sessions"), _columns(db._conn, "messages")
        assert all(col in sessions for col in _SESSION_COLUMNS)
        assert sessions["depth"] == "INTEGER" and sessions["route"] == "TEXT"
        assert all(col in messages for col in _MESSAGE_COLUMNS)
        assert messages["sent_at"] == "REAL"
        assert "wake_policy" in _columns(db._conn, "session_admissions")
        indexes = {row[0] for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"idx_sessions_root_session", "idx_sessions_group", "idx_sessions_parent"} <= indexes
    finally:
        db.close()


def test_topology_is_stamped_at_creation(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="cli")
        db.create_session("child", source="subagent", parent_session_id="root",
                          model_config={"_delegate_from": "root"})
        db.create_session("grandchild", source="subagent", parent_session_id="child",
                          model_config={"_delegate_from": "child"})
        db.create_session("branch", source="cli", parent_session_id="root", model_config={"_branched_from": "root"})
        # A compression continuation keeps its segment's delegate marker (it names the ORIGINAL parent,
        # not this one), so it is the same depth as the segment it continues.
        db.create_session("child-tip", source="subagent", parent_session_id="child",
                          model_config={"_delegate_from": "root"})
        assert _topology(db, "root") == ("root", 0)
        assert _topology(db, "child") == ("root", 1)
        assert _topology(db, "grandchild") == ("root", 2)
        assert _topology(db, "branch") == ("root", 0)
        assert _topology(db, "child-tip") == ("root", 1)
    finally:
        db.close()


def test_bare_row_learns_its_parent_from_the_later_upsert(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="cli")
        db.ensure_session("late", source="subagent")  # bare row first: a root of its own
        assert _topology(db, "late") == ("late", 0)
        db.create_session("late", source="subagent", parent_session_id="root", model_config={"_delegate_from": "root"})
        assert _topology(db, "late") == ("root", 1)
    finally:
        db.close()


def test_explicit_topology_is_kept(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db._conn.execute("INSERT INTO sessions (id, source, started_at, root_session_id, depth, group_id) "
                         "VALUES ('given', 'webhook', 0, 'tree', 1, 'batch-1')")
        assert _topology(db, "given") == ("tree", 1)
    finally:
        db.close()


def test_reconcile_adds_columns_to_an_old_store_without_backfilling(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("old-root", source="cli")
    db.create_session("old-child", source="subagent", parent_session_id="old-root",
                      model_config={"_delegate_from": "old-root"})
    db.append_message("old-root", role="user", content="kept")
    db.close()
    with sqlite3.connect(path) as legacy:  # the shape a pre-2a release left behind
        for name in ("sessions_topology_insert", "sessions_topology_reparent"):
            legacy.execute(f"DROP TRIGGER {name}")
        for name in ("idx_sessions_root_session", "idx_sessions_group"):
            legacy.execute(f"DROP INDEX {name}")
        for column in _SESSION_COLUMNS:
            legacy.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
        for column in _MESSAGE_COLUMNS:
            legacy.execute(f"ALTER TABLE messages DROP COLUMN {column}")
        legacy.execute("ALTER TABLE session_admissions DROP COLUMN wake_policy")
    db = SessionDB(db_path=path)
    try:
        assert all(col in _columns(db._conn, "sessions") for col in _SESSION_COLUMNS)
        assert all(col in _columns(db._conn, "messages") for col in _MESSAGE_COLUMNS)
        assert "wake_policy" in _columns(db._conn, "session_admissions")
        assert [m["content"] for m in db.get_messages_as_conversation("old-root")] == ["kept"]
        # Existing rows are left for the lineage migration...
        assert _topology(db, "old-root") == (None, None)
        assert _topology(db, "old-child") == (None, None)
        # ...and a child of an unmigrated parent cannot know its root yet either.
        db.create_session("new-child", source="subagent", parent_session_id="old-root",
                          model_config={"_delegate_from": "old-root"})
        assert _topology(db, "new-child") == (None, None)
        db.create_session("new-root", source="cli")
        assert _topology(db, "new-root") == ("new-root", 0)
    finally:
        db.close()
