"""Statement gates for the model-facing ``delegate_task`` list/observe/tree (integration of review R3/R8/R9).

``tests/tools/test_session_directory_bounds.py`` gates the directory primitives. What the model actually calls
goes through ``delegate_task`` -> ``tools.delegate_sessions.control`` -> ``DelegateSessionStore`` (caller
check, selector resolution, ACL, R3's reconcile, the compression-tip rule) and, before this fix, observe ran
the whole ``get`` twice plus the directory's own row/ACL read (35 statements), and ``scope="tree"`` walked the
default ACL row by row (one conversation lookup per distinct parent: O(rows)). The gates count every statement
on every read-pool connection AND on the writer, then grow the fixture and require the same counts.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.tools.test_session_directory_bounds import _grow
from tools.delegate_session_store import DelegateSessionError, DelegateSessionStore

DEAD = json.dumps({"pid": 2 ** 22 + 4321, "pidns": None, "start_ticks": 1})


def _own_targets(db):
    """Real grandchildren deliver to their own parent, not to the caller: the tree ACL must walk them."""
    db._conn.execute("UPDATE sessions SET delivery_target = json_object('kind', 'session', 'session_id', "
                     "parent_session_id, 'attach', json('true')) WHERE depth = 2")
    db._conn.commit()


def _trace(db) -> list:
    """Every statement on each connection the read pool hands out and on the writer inside writes."""
    statements, read_ctx, execute_write = [], db._read_ctx, db._execute_write

    @contextmanager
    def traced_read(*args, **kwargs):
        with read_ctx(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    def traced_write(fn, *args, **kwargs):
        def body(conn, *a, **k):
            conn.set_trace_callback(statements.append)
            try:
                return fn(conn, *a, **k)
            finally:
                conn.set_trace_callback(None)
        return execute_write(body, *args, **kwargs)

    db._read_ctx, db._execute_write = traced_read, traced_write
    return statements


@pytest.fixture
def rig(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _grow(db, 0)
    _own_targets(db)
    statements = _trace(db)
    yield db, statements, SimpleNamespace(session_id="parent", _session_db=db, _delegate_depth=0)
    db.close()


def _call(statements, **kwargs):
    from tools.delegate_tool import delegate_task
    statements.clear()
    out = json.loads(delegate_task(**kwargs))
    assert not out.get("error"), out
    return out, len(statements)


def _costs(db, statements, parent) -> dict:
    out = {}
    for _ in range(2):  # the second round is the steady state (per-db caches warm)
        listed, out["list5"] = _call(statements, action="list", limit=5, parent_agent=parent)
        assert listed["count"] == 5
        observed, out["observe6"] = _call(statements, action="observe", session_id="c00000", limit=6,
                                          parent_agent=parent)
        assert len(observed["messages"]) == 6 and observed["truncated"]
        tree, out["tree20"] = _call(statements, action="list", scope="tree", limit=20, parent_agent=parent)
        assert tree["count"] == 20
        searched, out["tree_query"] = _call(statements, action="list", scope="tree", limit=20, query="no-such-text",
                                            parent_agent=parent)
        assert searched["count"] == 0
    return out


def test_delegate_task_list_observe_and_tree_statements_are_constant(rig):
    db, statements, parent = rig
    small = _costs(db, statements, parent)
    assert small["list5"] <= 8, small
    assert small["observe6"] <= 12, small
    assert small["tree20"] <= 16 and small["tree_query"] <= 17, small
    _grow(db, 200)  # double: 400 children, 800 grandchildren, ~24.5k lineage messages
    _own_targets(db)
    assert _costs(db, statements, parent) == small


def test_a_running_page_reconciles_in_one_statement_and_rereads_only_after_a_recovery(rig):
    from hermes_state_runtime import get_session_admission
    db, statements, parent = rig
    store = DelegateSessionStore(db)
    _call(statements, action="list", limit=5, parent_agent=parent)
    page, idle = _call(statements, action="list", limit=5, parent_agent=parent)
    picked = [r["session_id"] for r in page["subagents"] if r["session_id"] != "c00000"][:3]
    live = []
    for sid in picked:  # three live (this process) claims on rows of the page
        store.admit("parent", sid, f"work {sid}")
        live.append((sid, store.claim(sid)))
    listed, running = _call(statements, action="list", limit=5, parent_agent=parent)
    assert sum(r["status"] == "running" for r in listed["subagents"]) == 3, listed
    assert running == idle + 1 <= 8, (idle, running)  # one reconcile statement for the page, no re-read
    # A provably dead runner on one of them: recovered by the list's single reconcile, the page re-read once.
    dead_sid, dead = live[0]
    db._write_sql("UPDATE state_meta SET value = ? WHERE key = ?", (DEAD, "delegate.runner.v1." + dead["admission_id"]))
    listed, recovering = _call(statements, action="list", limit=5, parent_agent=parent)
    assert get_session_admission(db, admission_id=dead["admission_id"])["outcome"] == "interrupted"
    status = {r["session_id"]: r["status"] for r in listed["subagents"]}
    assert status.get(dead_sid, "idle") == "idle" and [status.get(sid) for sid, _ in live[1:]] == ["running"] * 2
    assert recovering > running  # the recovery's writes and the re-read
    assert not store.release(dead_sid, dead["token"])
    for sid, claim in live[1:]:
        assert store.release(sid, claim["token"])


def test_observe_reconciles_a_dead_claim_and_refuses_a_gone_tip(rig):
    db, statements, parent = rig
    store = DelegateSessionStore(db)
    store.admit("parent", "c00005", "abandoned")
    claim = store.claim("c00005")
    db._write_sql("UPDATE state_meta SET value = ? WHERE key = ?", (DEAD, "delegate.runner.v1." + claim["admission_id"]))
    observed, _ = _call(statements, action="observe", session_id="c00005", parent_agent=parent)
    assert observed["session"]["status"] == "idle"
    assert not store.release("c00005", claim["token"])
    # A compressed child whose continuation is gone: unavailable through get, observe and delegate_task.
    db._conn.execute("INSERT INTO sessions(id,source,started_at,parent_session_id,root_session_id,depth,lineage_kind,"
                     "status,model_config,delivery_target) SELECT 'solo',source,started_at,parent_session_id,"
                     "root_session_id,depth,lineage_kind,status,model_config,delivery_target FROM sessions WHERE id = 'c00006'")
    db._conn.commit()
    assert store.get("parent", "solo")["current_session_id"] == "solo"
    db.end_session("solo", "compression")
    with pytest.raises(DelegateSessionError):
        store.get("parent", "solo")
    with pytest.raises(DelegateSessionError):
        store.observe("parent", "solo")
    from tools.delegate_tool import delegate_task
    refused = json.loads(delegate_task(action="observe", session_id="solo", parent_agent=parent))
    assert "unavailable" in refused["error"].lower()
    # Not reachable from a stranger (no disclosure), through either path.
    db.create_session("stranger", "cli")
    with pytest.raises(DelegateSessionError):
        store.observe("stranger", "c00000")


def test_batched_tree_reach_is_the_per_row_default_acl(rig):
    """Exactness of the batched walk: compare with ``default_session_acl`` row by row on a tree with
    compression-rotated parents, a non-delegate (memory fork) grandchild and a foreign-targeted one."""
    from agent import session_directory as directory
    db, _, _ = rig
    c = db._conn
    c.execute("UPDATE sessions SET ended_at = 950, end_reason = 'compression' WHERE id = 'c00007'")
    c.execute("INSERT INTO sessions(id,source,parent_session_id,started_at,lineage_kind) "
              "VALUES('c7tip','subagent','c00007',951,'continuation')")
    c.execute("INSERT INTO sessions(id,source,parent_session_id,root_session_id,depth,lineage_kind,started_at,"
              "delivery_target) VALUES('g7tip','subagent','c7tip','parent',2,'delegate',952,?)",
              (json.dumps({"kind": "session", "session_id": "c7tip", "attach": True}),))
    c.execute("INSERT INTO sessions(id,source,parent_session_id,root_session_id,depth,started_at) "
              "VALUES('memfork','subagent','c00008','parent',2,953)")
    c.execute("INSERT INTO sessions(id,source,parent_session_id,root_session_id,depth,lineage_kind,started_at,"
              "delivery_target) VALUES('foreign','webhook','c00009','parent',2,'webhook',954,?)",
              (json.dumps({"kind": "session", "session_id": "elsewhere", "attach": True}),))
    c.commit()
    with db._read_ctx() as conn:
        mine = directory.lineage(db, conn, "parent")
        picked = directory._subtree(db, conn, mine)
        batched = [row["id"] for row, _ in directory._default_tree_reach(db, conn, mine, picked)]
        per_row = [row["id"] for row, _ in picked if directory.default_session_acl(db, conn, "parent", row)]
        roots = directory._logical_roots(db, conn, ["c7tip", "c00007", "g7tip", "missing", "parent"])
        assert roots == {sid: directory.logical_root(db, conn, sid)
                         for sid in ["c7tip", "c00007", "g7tip", "missing", "parent"]}
    assert batched == per_row
    assert "g7tip" in batched


def test_the_pending_admission_read_stays_on_the_pending_range(tmp_path):
    """Composition of R2/R3 (the drain, claim, admit and running-claim read the pending FIFO on every pass)
    with R8's ``idx_session_admissions_target_seq``: next to that index the planner walked the target's whole
    admission history in seq order instead of the pending range (bounded by the queue cap)."""
    from hermes_state_runtime import list_session_admissions
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", "cli")
        db._conn.executemany(
            "INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,lineage_json,"
            "payload_json,payload_digest,intent,status,outcome) VALUES(?,?,'parent:p','s','[]','{}','d','queue',?,?)",
            [(f"a{i}", str(i), "terminal" if i < 500 else "queued", "completed" if i < 500 else None)
             for i in range(501)])
        db._conn.commit()
        indexes = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'session_admissions'")}
        assert "idx_session_admissions_target_seq" in indexes  # the composition this guards
        statements = _trace(db)
        rows = list_session_admissions(db, session_id="s")
        assert [r["admission_id"] for r in rows] == ["a500"]
        [sql] = [s for s in statements if "session_admissions" in s]
        plan = " | ".join(r[3] for r in db._conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "session_admissions_pending (target_session_id=? AND status=?)" in plan, plan
        history = list_session_admissions(db, session_id="s", pending_only=False)
        assert [r["admission_id"] for r in history] == [f"a{i}" for i in range(501)]
    finally:
        db.close()


# ── engine work (re-review R8 residual): O(limit) SQLite VM work and Python rows, not O(candidates) ─────────

class _Work:
    """Statements, fetched rows and approximate SQLite VM opcodes (a progress handler ticks every 100)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.statements = self.rows = self.vm = 0

    def tick(self):
        self.vm += 100
        return 0

    def stmt(self, _sql):
        self.statements += 1


class _RowCursor:
    def __init__(self, cursor, work):
        self._cursor, self._work = cursor, work

    def fetchone(self):
        row = self._cursor.fetchone()
        self._work.rows += row is not None
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._work.rows += len(rows)
        return rows

    def __iter__(self):
        for row in self._cursor:
            self._work.rows += 1
            yield row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _RowConn:
    def __init__(self, conn, work):
        self._conn, self._work = conn, work

    def execute(self, *args, **kwargs):
        return _RowCursor(self._conn.execute(*args, **kwargs), self._work)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _metered(db) -> _Work:
    """Observe (never alter) every read-pool connection and the writer inside writes: the same counters the
    re-review's sql_audit.py used (progress handler every 100 VM ops, trace callback, fetched rows)."""
    work, read_ctx, execute_write = _Work(), db._read_ctx, db._execute_write

    def start(conn):
        conn.set_trace_callback(work.stmt)
        conn.set_progress_handler(work.tick, 100)

    def stop(conn):
        conn.set_trace_callback(None)
        conn.set_progress_handler(None, 0)

    @contextmanager
    def metered_read(*args, **kwargs):
        with read_ctx(*args, **kwargs) as conn:
            start(conn)
            try:
                yield _RowConn(conn, work)
            finally:
                stop(conn)

    def metered_write(fn, *args, **kwargs):
        def body(conn, *a, **k):
            start(conn)
            try:
                return fn(conn, *a, **k)
            finally:
                stop(conn)
        return execute_write(body, *args, **kwargs)

    db._read_ctx, db._execute_write = metered_read, metered_write
    return work


_CALLS = (("list1", {"action": "list", "limit": 1}), ("list20", {"action": "list", "limit": 20}),
          ("tree20", {"action": "list", "scope": "tree", "limit": 20}))


def test_list_and_tree_engine_work_and_rows_do_not_grow_with_the_candidates(tmp_path):
    """The re-review's table: at 40 -> 400 direct children (+2 grandchildren each) list(1) VM work grew
    20,100 -> 182,400 (activity/compression/aggregates computed for every candidate before ORDER BY ...
    LIMIT) and tree(20) hydrated and sorted 325 -> 3,205 rows in Python (1.01M VM ops). Each must now stay
    within a small constant factor, the tree's Python rows ~ its limit, and the pages stay the exact heads."""
    from tools.delegate_tool import delegate_task
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        parent = SimpleNamespace(session_id="parent", _session_db=db, _delegate_depth=0)
        work, costs, start = None, {}, 0
        for n in (40, 160, 400):
            _grow(db, start, children=n - start, history=3, tip_messages=300)
            _own_targets(db)
            start = n
            work = work or _metered(db)
            for _ in range(2):  # the second round is the steady state (per-db caches warm)
                costs[n] = {}
                for name, kwargs in _CALLS:
                    work.reset()
                    out = json.loads(delegate_task(parent_agent=parent, **kwargs))
                    assert not out.get("error"), out
                    costs[n][name] = (work.statements, work.rows, work.vm, [r["session_id"] for r in out["subagents"]])
        for name, _ in _CALLS:
            small, big = costs[40][name], costs[400][name]
            assert big[0] == small[0], (name, costs)  # statements: constant
            assert big[1] <= small[1] + 2, (name, costs)  # Python rows: constant
            assert big[2] <= 1.5 * small[2], (name, {n: c[name][:3] for n, c in costs.items()})  # VM work
        assert costs[400]["tree20"][1] <= 3 * 20 + 20, costs[400]["tree20"][:3]
        assert costs[400]["list20"][1] <= 2 * 20 + 10, costs[400]["list20"][:3]
        # Still the exact heads of the full orderings.
        store = DelegateSessionStore(db)
        assert costs[400]["list20"][3] == [r["session_id"] for r in _reference_list(db, "parent")][:20]
        assert costs[400]["list1"][3] == costs[400]["list20"][3][:1]
        assert costs[400]["tree20"][3] == _reference_tree(db, "parent")[:20]
        assert [r["session_id"] for r in store.list("parent", limit=5)] == costs[400]["list20"][3][:5]
    finally:
        db.close()


def _reference_list(db, caller: str, include_closed: bool = False) -> list[dict]:
    """Freya's rule and the list order computed the unbounded way, row by row in Python (every sessions row
    is a candidate): the oracle the bounded scan must match."""
    from agent import session_directory as directory
    with db._read_ctx() as conn:
        mine = set(directory.lineage(db, conn, caller))
        selectors = directory._Selectors(db)
        rows = [row for row in conn.execute("SELECT * FROM sessions").fetchall()
                if row["id"] not in mine and directory._is_default_child(row, mine, conn, selectors)]
        infos = directory.hydrate(conn, rows)
    infos = [info for info in infos if include_closed or info["status"] != "closed"]
    return sorted(infos, key=lambda r: (r["last_activity_at"], r["created_at"], r["session_id"]), reverse=True)


def _reference_tree(db, caller: str, include_closed: bool = False) -> list[str]:
    """The breadth-first walk's whole subtree, default ACL, the list order (the oracle for scope=tree)."""
    from agent import session_directory as directory
    with db._read_ctx() as conn:
        mine = directory.lineage(db, conn, caller)
        saved, directory._MAX_TREE_ROWS = directory._MAX_TREE_ROWS, 10 ** 9  # the walk's whole subtree
        try:
            return [r["session_id"] for r in directory._list_tree_bfs(
                db, conn, caller, mine, needle="", limit=10 ** 6, include_closed=include_closed)]
        finally:
            directory._MAX_TREE_ROWS = saved


def _bound_holds(db) -> list:
    """Rows whose materialized bound is below their exact list key (must be none)."""
    from agent import session_directory as directory
    with db._read_ctx() as conn:
        rows = conn.execute("SELECT * FROM sessions").fetchall()
        exact = {info["session_id"]: info["last_activity_at"] for info in directory.hydrate(conn, rows)}
        return [(row["id"], row["conversation_activity_at"], exact[row["id"]]) for row in rows
                if row["conversation_activity_at"] < exact[row["id"]]]


def test_bounded_pages_stay_exact_through_every_recency_write_path(rig):
    """Every write that moves a row's exact key keeps the bound >= it (SQL triggers, whoever wrote it), and
    a stale-HIGH bound (a deleted newest message) never reorders a page: the scan verifies exact keys."""
    db, _, _ = rig
    c = db._conn
    store = DelegateSessionStore(db)
    # 1) stale high: c00003's newest message is deleted after it raised the bound.
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('c00003','user','spike',5e9)")
    c.execute("DELETE FROM messages WHERE session_id = 'c00003' AND content = 'spike'")
    # 2) a compression split whose continuation row exists before the parent is ended.
    c.execute("INSERT INTO sessions(id,source,parent_session_id,started_at,lineage_kind) "
              "VALUES('c11tip','subagent','c00011',951,'continuation')")
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('c11tip','user','late',4e9)")
    c.execute("UPDATE sessions SET ended_at = 950, end_reason = 'compression' WHERE id = 'c00011'")
    # 3) a continuation reparented under an already compression-ended parent (the join path).
    c.execute("UPDATE sessions SET ended_at = 960, end_reason = 'compression' WHERE id = 'c00012'")
    c.execute("INSERT INTO sessions(id,source,started_at,lineage_kind) VALUES('c12tip','subagent',961,'continuation')")
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('c12tip','user','moved',3e9)")
    c.execute("UPDATE sessions SET parent_session_id = 'c00012' WHERE id = 'c12tip'")
    # 4) a chain of two continuations (the family spans three rows).
    c.execute("UPDATE sessions SET ended_at = 970, end_reason = 'compression' WHERE id = 'c12tip'")
    c.execute("INSERT INTO sessions(id,source,parent_session_id,started_at,lineage_kind) "
              "VALUES('c12tip2','subagent','c12tip',971,'continuation')")
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('c12tip2','user','deep',3.5e9)")
    # 5) the closed filter: a closed child leaves; an ended opt-in with a running admission stays.
    c.execute("UPDATE sessions SET status = 'closed' WHERE id = 'c00013'")
    c.execute("INSERT INTO sessions(id,source,started_at,root_session_id,depth,lineage_kind,ended_at,end_reason,"
              "delivery_target) VALUES('hook','webhook',2e9,'hook',0,'webhook',2e9,'done',?)",
              (json.dumps({"kind": "session", "session_id": "parent", "attach": True}),))
    assert c.execute("SELECT list_closed FROM sessions WHERE id = 'hook'").fetchone()[0] == 1
    c.execute("INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,lineage_json,"
              "payload_json,payload_digest,intent,status,owner_epoch,generation) "
              "VALUES('hook-run','r','webhook:x','hook','[]','{}','d','queue','started',1,1)")
    # still flagged (no trigger on the admission path): the list's running arm reads it back
    assert c.execute("SELECT list_closed FROM sessions WHERE id = 'hook'").fetchone()[0] == 1
    # 6) mid-turn activity on a quiet child.
    c.execute("UPDATE sessions SET last_activity_at = 2.5e9 WHERE id = 'c00014'")
    c.commit()
    assert _bound_holds(db) == []
    reference = [r["session_id"] for r in _reference_list(db, "parent")]
    assert reference[:6] == ["c00011", "c00012", "c00014", "hook", "c00000", reference[5]]
    assert "c00003" in reference and "c00013" not in reference
    for limit in (1, 2, 5, 20, 100):
        assert [r["session_id"] for r in store.list("parent", limit=limit)] == reference[:limit], limit
    assert store.get("parent", "c00012")["current_session_id"] == "c12tip2"
    # include_closed / a query read both list_closed partitions.
    with db._read_ctx() as conn:
        from agent import session_directory as directory
        closed = [r["session_id"] for r in directory.list_sessions(db, conn, "parent", limit=100, include_closed=True)]
    assert closed == [r["session_id"] for r in _reference_list(db, "parent", include_closed=True)][:100]
    assert [r["session_id"] for r in store.list("parent", query="spike", limit=5)] == []
    assert [r["session_id"] for r in store.list("parent", query="late", limit=5)] == ["c00011"]


def test_bounded_tree_is_the_breadth_first_walk_head(rig):
    """scope=tree's upward membership equals the breadth-first walk (``_subtree`` + the default ACL) on a tree
    with compression-rotated parents, a memory fork, a foreign-targeted opt-in, a grandchild opt-in targeting
    its parent's continuation, a closed child's live subtree and a custom policy."""
    from agent import session_directory as directory
    db, _, _ = rig
    c = db._conn
    c.execute("UPDATE sessions SET ended_at = 950, end_reason = 'compression' WHERE id = 'c00007'")
    c.execute("INSERT INTO sessions(id,source,parent_session_id,started_at,lineage_kind) "
              "VALUES('c7tip','subagent','c00007',951,'continuation')")
    c.execute("INSERT INTO sessions(id,source,parent_session_id,root_session_id,depth,lineage_kind,started_at,"
              "delivery_target) VALUES('g7tip','subagent','c7tip','parent',2,'delegate',5e9,?)",
              (json.dumps({"kind": "session", "session_id": "c7tip", "attach": True}),))
    c.execute("INSERT INTO sessions(id,source,parent_session_id,root_session_id,depth,lineage_kind,started_at,"
              "delivery_target) VALUES('hook7','webhook','c7tip','parent',2,'webhook',4e9,?)",
              (json.dumps({"kind": "session", "session_id": "c7tip", "attach": True}),))
    c.execute("INSERT INTO sessions(id,source,parent_session_id,root_session_id,depth,started_at) "
              "VALUES('memfork','subagent','c00008','parent',2,6e9)")
    c.execute("INSERT INTO sessions(id,source,parent_session_id,root_session_id,depth,lineage_kind,started_at,"
              "delivery_target) VALUES('foreign','webhook','c00009','parent',2,'webhook',7e9,?)",
              (json.dumps({"kind": "session", "session_id": "elsewhere", "attach": True}),))
    c.execute("UPDATE sessions SET status = 'closed' WHERE id = 'c00010'")
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('c00010','user','closed',2.9e9)")
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('g00010_1','user','hot',3e9)")
    c.commit()
    store = DelegateSessionStore(db)
    reference = _reference_tree(db, "parent")
    assert reference[:4] == ["g7tip", "hook7", "g00010_1", "c00000"]
    assert not {"memfork", "foreign", "c00010", "tip", "c7tip"} & set(reference)
    for limit in (1, 3, 20, 100):
        got = [r["session_id"] for r in store.list("parent", scope="tree", limit=limit)]
        assert got == reference[:limit], limit
    with db._read_ctx() as conn:
        everything = [r["session_id"] for r in directory.list_sessions(db, conn, "parent", scope="tree", limit=100,
                                                                         include_closed=True)]
    assert everything == _reference_tree(db, "parent", include_closed=True)[:100]
    assert everything[:5] == ["g7tip", "hook7", "g00010_1", "c00010", "c00000"]
    lease = directory.install_session_acl(lambda db, conn, caller, row: row["id"].endswith("_1"), scope=db)
    try:
        got = [r["session_id"] for r in store.list("parent", scope="tree", limit=20)]
    finally:
        lease.dispose()
    assert got and all(sid.endswith("_1") for sid in got) and got[0] == "g00010_1"
    # A child caller's own subtree only.
    assert sorted(r["session_id"] for r in store.list("c00001", scope="tree", limit=20)) == ["g00001_0", "g00001_1"]


def test_a_store_that_predates_the_recency_columns_is_backfilled_once(tmp_path):
    """Rows written before the triggers existed (the column defaults) are computed once at open, after the
    triggers are installed; the marker keeps a settled store read-only; the result is the same list."""
    from hermes_state_list_recency import LIST_RECENCY_BACKFILL_KEY
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    _grow(db, 0, children=30, history=1, tip_messages=50)
    before = [r["session_id"] for r in DelegateSessionStore(db).list("parent", limit=30)]
    c = db._conn
    for name in [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                          "AND name LIKE '%list_recency%'")]:
        c.execute(f"DROP TRIGGER {name}")
    c.execute("UPDATE sessions SET conversation_root_id = NULL, conversation_activity_at = 0, list_closed = 0")
    c.execute("DELETE FROM state_meta WHERE key = ?", (LIST_RECENCY_BACKFILL_KEY,))
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('tip','user','after',9e9)")  # no trigger
    c.commit()
    db.close()
    db = SessionDB(db_path=path)
    try:
        assert db._conn.execute("SELECT value FROM state_meta WHERE key = ?", (LIST_RECENCY_BACKFILL_KEY,)).fetchone()
        assert _bound_holds(db) == []
        assert db._conn.execute("SELECT conversation_root_id FROM sessions WHERE id = 'tip'").fetchone()[0] == "c00000"
        after = [r["session_id"] for r in DelegateSessionStore(db).list("parent", limit=30)]
        assert after == before == [r["session_id"] for r in _reference_list(db, "parent")][:30]
    finally:
        db.close()
