"""Backfill of the materialized delegate-list recency columns (review R8).

``sessions.conversation_root_id`` / ``conversation_activity_at`` / ``list_closed`` (hermes_state_common
SCHEMA_SQL) are maintained incrementally by the ``*_list_recency_*`` triggers (DEFERRED_INDEX_SQL) on every
write, whoever wrote it. Rows that predate those triggers are computed here once per store, AFTER the
triggers exist (so a row written concurrently is covered by one or the other), in one write transaction,
and recorded by a state_meta marker so a settled store never takes the write lock again.

The computation is a monotone merge (families only union, the bound only rises), so re-running it is a
logical no-op: the offline unified migration (hermes_state_unified_migration) calls it again after its
apply to fold its own rewrites in without depending on trigger coverage.
"""

from __future__ import annotations

import sqlite3

from hermes_state_common import LIST_CLOSED_SQL, _non_continuation_child_sql

LIST_RECENCY_BACKFILL_KEY = "list_recency_backfill"

_EDGES_SQL = ("SELECT c.id, p.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id "
              "WHERE p.end_reason = 'compression' " + _non_continuation_child_sql("c.", "p.id").replace("\n", " "))


def _legacy_created_sql(conn) -> str:
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'delegate_sessions'").fetchone() is not None
    if not present:
        return "0"
    # agent.session_directory._created_sql: the legacy created_at only stands in for a zero started_at.
    return ("CASE WHEN COALESCE(started_at, 0) = 0 AND COALESCE(lineage_kind, '') = '' THEN COALESCE((SELECT "
            "d.created_at FROM delegate_sessions d WHERE d.session_id = sessions.id), 0) ELSE 0 END")


def families(conn) -> dict[str, str]:
    """``{session id: family label}`` for every row in a multi-row family: union-find over the eligible
    continuation edges AND the families the triggers already merged (families never split)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(child: str, into: str) -> None:
        parent.setdefault(child, child)
        parent.setdefault(into, into)
        a, b = find(child), find(into)
        if a != b:
            parent[a] = b  # the child's family joins the parent's: the head of the chain labels it

    for child, par in conn.execute(_EDGES_SQL).fetchall():
        union(child, par)
    for sid, label in conn.execute("SELECT id, conversation_root_id FROM sessions WHERE conversation_root_id "
                                   "IS NOT NULL AND conversation_root_id != id").fetchall():
        union(sid, label)
    return {sid: find(sid) for sid in list(parent)}


def compute(conn) -> None:
    """Recompute every row's family, bound and closed flag (monotone: never lowers a bound or splits)."""
    conn.execute("UPDATE sessions SET conversation_root_id = id WHERE conversation_root_id IS NULL")
    conn.executemany("UPDATE sessions SET conversation_root_id = ? WHERE id = ? AND conversation_root_id IS NOT ?",
                     [(root, sid, root) for sid, root in families(conn).items()])
    bound = ("MAX(COALESCE(started_at, 0), COALESCE(last_activity_at, 0), " + _legacy_created_sql(conn) + ", "
             "COALESCE((SELECT MAX((SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = f.id)) "
             "FROM sessions f WHERE f.conversation_root_id = sessions.conversation_root_id), 0))")
    conn.execute(f"UPDATE sessions SET conversation_activity_at = {bound} WHERE conversation_activity_at < {bound}")
    conn.execute(f"UPDATE sessions SET list_closed = {LIST_CLOSED_SQL} WHERE list_closed IS NOT {LIST_CLOSED_SQL}")


def backfill(cursor: sqlite3.Cursor) -> bool:
    """Once per store (state_meta marker), inside one savepoint. Returns True when it ran."""
    if cursor.execute("SELECT 1 FROM state_meta WHERE key = ? LIMIT 1", (LIST_RECENCY_BACKFILL_KEY,)).fetchone():
        return False
    cursor.execute("SAVEPOINT list_recency_backfill")
    try:
        compute(cursor)
        cursor.execute("INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, '1')", (LIST_RECENCY_BACKFILL_KEY,))
        cursor.execute("RELEASE SAVEPOINT list_recency_backfill")
    except BaseException:
        cursor.execute("ROLLBACK TO SAVEPOINT list_recency_backfill")
        cursor.execute("RELEASE SAVEPOINT list_recency_backfill")
        raise
    return True
