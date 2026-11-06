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
