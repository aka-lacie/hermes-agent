"""Newest-N over a compressed lineage reads a bounded window, never the whole lineage (review R9).

``get_messages(include_ancestors=True, latest=True, limit=N)`` must equal the last N of the full merged,
deduplicated read even where compaction copies straddle the window edge (protected-tail copies in the
continuation, a carried-forward tool call with stable ids, a summary carrier folding into an older uid),
and must fetch O(N) rows when nothing straddles.
"""

import json
from contextlib import contextmanager

import pytest

from hermes_state import SessionDB


@pytest.fixture
def lineage(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    c = db._conn
    c.execute("BEGIN")
    c.execute("INSERT INTO sessions(id,source,started_at) VALUES('root','cli',1)")
    rows = []
    for k in range(400):
        role = ("user", "assistant", "tool")[k % 3]
        rows.append(("root", role, f"root {k} " + "r" * 50, 100 + k, None, None, None, 0))
    # a tool-calling assistant (stable call ids) and the uid a carrier folds into, near the root's end
    rows.append(("root", "assistant", "calling", 600, None, json.dumps([{"id": "call-1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\": \"a\"}"}}]), "uid-call", 0))
    rows.append(("root", "user", "original question", 601, None, None, "uid-q", 0))
    c.executemany("INSERT INTO messages(session_id,role,content,timestamp,tool_call_id,tool_calls,message_uid,"
                  "_compressed_summary) VALUES(?,?,?,?,?,?,?,?)", rows)
    c.execute("UPDATE sessions SET ended_at=900, end_reason='compression' WHERE id='root'")
    c.execute("INSERT INTO sessions(id,source,parent_session_id,started_at) VALUES('tip','cli','root',901)")
    tip = []
    # the protected tail copied into the continuation: same role/content/timestamp as the root's last rows
    for k in range(392, 400):
        role = ("user", "assistant", "tool")[k % 3]
        tip.append(("tip", role, f"root {k} " + "r" * 50, 100 + k, None, None, None, 0))
    tip.append(("tip", "assistant", "calling (pruned args)", 600, None, json.dumps([{"id": "call-1", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"}}]), None, 0))
    tip.append(("tip", "user", "[summary of earlier turns]", 902, None, None, "uid-q", 1))  # a summary carrier
    for k in range(300):
        role = ("user", "assistant", "tool")[k % 3]
        tip.append(("tip", role, f"tip {k} " + "t" * 50, 1000 + k, None, None, None, 0))
    c.executemany("INSERT INTO messages(session_id,role,content,timestamp,tool_call_id,tool_calls,message_uid,"
                  "_compressed_summary) VALUES(?,?,?,?,?,?,?,?)", tip)
    c.commit()
    yield db
    db.close()


def _rows_fetched(db):
    counter, original = {"rows": 0, "statements": 0}, db._read_ctx

    class Cursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchall(self):
            rows = self._cursor.fetchall()
            counter["rows"] += len(rows)
            return rows

        def fetchone(self):
            row = self._cursor.fetchone()
            counter["rows"] += row is not None
            return row

        def __iter__(self):
            for row in self._cursor:
                counter["rows"] += 1
                yield row

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class Conn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args, **kwargs):
            counter["statements"] += 1
            return Cursor(self._conn.execute(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def read_ctx(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            yield Conn(conn)

    db._read_ctx = read_ctx
    return counter


def test_newest_n_equals_the_tail_of_the_full_lineage_read_across_every_window_edge(lineage):
    db = lineage
    full = db.get_messages("tip", include_ancestors=True)
    assert len(full) < 400 + 2 + 310  # the copies collapsed
    for limit in [1, 2, 5, 6, 7, 20, 50, 299, 300, 301, 302, 303, 305, 310, 320, 400, 700, 800]:
        got = db.get_messages("tip", include_ancestors=True, limit=limit, latest=True)
        assert [m["id"] for m in got] == [m["id"] for m in full[-limit:]], limit
        assert got == full[-limit:], limit
    for limit, offset in [(5, 0), (5, 1), (10, 295), (10, 300), (3, 600)]:
        got = db.get_messages("tip", include_ancestors=True, limit=limit, offset=offset, latest=True)
        expected = full[::-1][offset:][:limit][::-1]
        assert [m["id"] for m in got] == [m["id"] for m in expected], (limit, offset)
    preview = db.get_lineage_tail_preview("tip", 7)
    assert [m["id"] for m in preview] == [m["id"] for m in full[-7:]]
    assert [m["content"] for m in preview] == [m["content"] for m in full[-7:]]  # short bodies are whole


def test_newest_n_fetches_o_of_n_rows(lineage):
    db = lineage
    counter = _rows_fetched(db)
    got = db.get_messages("tip", include_ancestors=True, limit=6, latest=True)
    assert len(got) == 6
    assert counter["rows"] <= 10 * 6, counter  # the whole-lineage read fetched ~710 rows here


def test_preview_bounds_long_bodies_but_previews_them_exactly(lineage):
    from agent import session_views as views
    db = lineage
    db.append_message("tip", "tool", "head words " * 600 + "the very end")
    db.append_message("tip", "assistant", "  ".join(["lots of   spaced words"] * 400))
    full = db.get_messages("tip", include_ancestors=True)
    preview = db.get_lineage_tail_preview("tip", 3)
    assert [m["id"] for m in preview] == [m["id"] for m in full[-3:]]
    assert [views.message_view(m) for m in preview] == [views.message_view(m) for m in full[-3:]]
    assert all(len(m["content"]) <= 2 * 1200 + 1 for m in preview)
    assert preview[-1]["_content_chars"] == len(full[-1]["content"])
