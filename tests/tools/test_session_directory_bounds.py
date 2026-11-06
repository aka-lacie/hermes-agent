"""Statement/row gates for the delegate list and observe (independent review R8/R9).

The review's fixture: 200 children + 400 grandchildren with admission history, and a compressed two-segment
child lineage holding ~12,550 messages. Before the fix ``list(limit=1|20)`` issued 1,807 statements (600
admission-history reads, 201 MAX(timestamp) reads) and ``observe(limit=6)`` fetched all 12,550 rows to
show six. The gates count every statement the SessionDB read pool executes (``set_trace_callback`` on each
connection it hands out) and every row those statements return, then double the fixture and require the
same statement counts.
"""

import json
from contextlib import contextmanager

import pytest

from hermes_state import SessionDB
from tools.delegate_session_store import DelegateSessionStore

BRIEF = json.dumps({"_delegate_brief": {"retain": True, "initial_goal": "goal", "title": "fixture"}})
ROUTE = json.dumps({"kind": "named", "provider": "probe", "model": "probe"})
TARGET = json.dumps({"kind": "session", "session_id": "parent", "attach": True})


def _grow(db, start: int, children: int = 200, history: int = 10, tip_messages: int = 12_000) -> None:
    """*children* delegate children (``c<n>``) of ``parent``, two grandchildren each, a few messages and
    *history* terminal admissions per session; the first batch compresses ``c00000`` into ``tip``; every batch
    appends *tip_messages* to ``tip``."""
    c = db._conn
    c.execute("BEGIN")
    if start == 0:
        c.execute("INSERT INTO sessions(id,source,started_at,title,root_session_id,depth,lineage_kind) "
                  "VALUES('parent','cli',1,'parent','parent',0,'root')")
    rows = []
    for i in range(start, start + children):
        child = f"c{i:05d}"
        rows.append((child, "parent", 1, 1 + i % 7))
        rows.extend((f"g{i:05d}_{j}", child, 2, 2 + j) for j in range(2))
    c.executemany(
        "INSERT INTO sessions(id,source,started_at,parent_session_id,root_session_id,depth,lineage_kind,status,"
        "model_config,route,delivery_target) VALUES(?,'subagent',?,?,'parent',?,'delegate','idle',?,?,?)",
        [(sid, started, parent, depth, BRIEF, ROUTE, TARGET) for sid, parent, depth, started in rows])
    c.executemany(
        "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
        [(sid, ("user", "assistant", "tool")[k % 3], f"{sid} message {k} " + "x" * 200, 10 + k + i % 5)
         for i, (sid, _, _, _) in enumerate(rows) for k in range(3)])
    c.executemany(
        "INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,lineage_json,"
        "payload_json,payload_digest,intent,status,outcome) VALUES(?,?,?,?,'[]',?,'d','queue','terminal','completed')",
        [(f"{sid}-{k}", str(k), f"parent:{parent}", sid, json.dumps({"text": f"assignment {sid} {k}"}))
         for sid, parent, _, _ in rows for k in range(history)])
    if start == 0:
        c.executemany("INSERT INTO messages(session_id,role,content,timestamp) VALUES('c00000',?,?,?)",
                      [(("user", "assistant", "tool")[k % 3], f"segment one {k} " + "y" * 200, 100 + k)
                       for k in range(547)])
        c.execute("UPDATE sessions SET ended_at=900, end_reason='compression' WHERE id='c00000'")
        c.execute("INSERT INTO sessions(id,source,parent_session_id,started_at,lineage_kind) "
                  "VALUES('tip','subagent','c00000',901,'continuation')")
    base = c.execute("SELECT COALESCE(MAX(timestamp), 1000) FROM messages WHERE session_id='tip'").fetchone()[0]
    c.executemany("INSERT INTO messages(session_id,role,content,timestamp) VALUES('tip',?,?,?)",
                  [(("user", "assistant", "tool")[k % 3], f"tip {start} {k} " + "z" * 256, base + 1 + k)
                   for k in range(tip_messages)])
    c.commit()


class _Counter:
    def __init__(self):
        self.statements, self.rows = [], 0

    def reset(self):
        self.statements.clear()
        self.rows = 0


class _Cursor:
    def __init__(self, cursor, counter):
        self._cursor, self._counter = cursor, counter

    def fetchone(self):
        row = self._cursor.fetchone()
        self._counter.rows += row is not None
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._counter.rows += len(rows)
        return rows

    def __iter__(self):
        for row in self._cursor:
            self._counter.rows += 1
            yield row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _Conn:
    def __init__(self, conn, counter):
        self._conn, self._counter = conn, counter

    def execute(self, *args, **kwargs):
        return _Cursor(self._conn.execute(*args, **kwargs), self._counter)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _traced(db) -> _Counter:
    """Count every statement run on (and every row returned by) each connection the read pool hands out."""
    counter, original = _Counter(), db._read_ctx

    @contextmanager
    def read_ctx(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            conn.set_trace_callback(counter.statements.append)
            try:
                yield _Conn(conn, counter)
            finally:
                conn.set_trace_callback(None)

    db._read_ctx = read_ctx
    return counter


@pytest.fixture
def big(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _grow(db, 0)
    counter = _traced(db)
    yield db, counter
    db.close()


def _measure(counter, fn):
    counter.reset()
    value = fn()
    return value, len(counter.statements), counter.rows


def _costs(db, counter, store):
    from agent import session_directory as directory
    out = {}
    for limit in (5, 20):
        rows, statements, fetched = _measure(counter, lambda: store.list("parent", limit=limit))
        assert len(rows) == limit
        out[f"list_{limit}"] = (statements, fetched)

    def observe():
        with db._read_ctx() as conn:
            return directory.observe(db, conn, "parent", "c00000", limit=6)
    observed, statements, fetched = _measure(counter, observe)
    assert len(observed["messages"]) == 6 and observed["truncated"]
    out["observe"] = (statements, fetched)
    tail, statements, fetched = _measure(counter, lambda: db.get_messages(
        "tip", include_ancestors=True, limit=6, latest=True))
    assert len(tail) == 6
    out["tail"] = (statements, fetched)
    return out


def test_list_and_observe_statement_and_row_counts_are_bounded_and_size_independent(big):
    db, counter = big
    store = DelegateSessionStore(db)
    _costs(db, counter, store)  # warm the per-db caches (message column names): steady state is measured
    small = _costs(db, counter, store)
    for limit in (5, 20):
        statements, fetched = small[f"list_{limit}"]
        assert statements <= 8, (limit, counter.statements)
        assert fetched <= 10 * limit + 10, (limit, fetched)
    statements, fetched = small["observe"]
    assert statements <= 10, counter.statements
    assert fetched <= 10 * 6, fetched
    statements, fetched = small["tail"]
    assert statements <= 5 and fetched <= 10 * 6, (statements, fetched)

    _grow(db, 200)  # double: 400 children, 800 grandchildren, ~24.5k lineage messages
    doubled = _costs(db, counter, store)
    assert {k: v[0] for k, v in doubled.items()} == {k: v[0] for k, v in small.items()}, (small, doubled)
    for key, (_, fetched) in doubled.items():
        assert fetched <= small[key][1] + 2, (key, small, doubled)


def test_bounded_list_is_the_head_of_the_full_ordering_with_roll_ups(big):
    db, _ = big
    store = DelegateSessionStore(db)
    every = store.list("parent", limit=100)
    assert [r["session_id"] for r in store.list("parent", limit=5)] == [r["session_id"] for r in every[:5]]
    assert [r["session_id"] for r in store.list("parent", limit=20)] == [r["session_id"] for r in every[:20]]
    keys = [(r["last_activity_at"], r["created_at"], r["session_id"]) for r in every]
    assert keys == sorted(keys, reverse=True)
    assert all(r["children_total"] == 2 and r["running_children"] == 0 for r in every)
    assert all(r["assignment"].startswith(f"assignment {r['session_id']} 9") for r in every)
    assert not any(r["session_id"].startswith("g") or r["session_id"] == "tip" for r in every)
    compressed = store.get("parent", "c00000")
    assert compressed["current_session_id"] == "tip"


def test_observe_tail_is_the_newest_of_the_whole_lineage_read(big):
    from agent import session_directory as directory
    from agent import session_views as views
    db, _ = big
    full = db.get_messages("tip", include_ancestors=True)  # the unbounded, whole-lineage read
    with db._read_ctx() as conn:
        observed = directory.observe(db, conn, "parent", "c00000", limit=6)
    assert observed["messages"] == [views.message_view(m) for m in full[-6:]]
    assert [m["id"] for m in db.get_messages("tip", include_ancestors=True, limit=6, latest=True)] == \
        [m["id"] for m in full[-6:]]
    with db._read_ctx() as conn:
        child = directory.observe(db, conn, "parent", "c00001", limit=6)
    assert sorted(c["session_id"] for c in child["children"]) == ["g00001_0", "g00001_1"]
    assert all(c["latest"] for c in child["children"])
