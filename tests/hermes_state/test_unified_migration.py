"""Phase 2f offline migration against a small synthetic state.db: dry-run writes nothing, apply backfills the
session topology and message provenance, a second apply changes nothing, and SessionDB reads the result."""
import json
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_unified_migration import Options, main, run

NOW = 1793880000.0
SNOW = "1567863795106447531"
TRIGGER = f"[Triggering message id: `{SNOW}` — use as `message_id` for reply/react/pin via the discord tools.]\n\n"
END = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"

SESSIONS = [
    # id, source, parent, end_reason, model_config, chat_type, session_key, started
    ("dm", "discord", None, "compression", {"model": "gemini-4-argon", "provider": "cliproxy-gemini",
                                            "reasoning_config": {"enabled": True, "effort": "medium"}},
     "dm", "agent:main:discord:dm:1443", NOW - 30 * 86400),
    ("dm2", "discord", "dm", None, {"model": "gemini-4-argon", "provider": "cliproxy-gemini"}, "dm",
     "agent:main:discord:dm:1443", NOW - 3600),
    ("child", "subagent", "dm2", "compression", {"_delegate_from": "dm2"}, None, None, NOW - 1800),
    ("child2", "subagent", "child", None, {"_delegate_from": "dm2"}, None, None, NOW - 900),  # inherited marker
    ("grand", "subagent", "child2", None, {"_delegate_from": "child2"}, None, None, NOW - 600),
    ("branch", "discord", "dm2", None, {"_branched_from": "dm2"}, "dm", None, NOW - 500),
    ("hook", "webhook", None, None, None, "webhook", "agent:main:webhook:x", NOW - 400),
    ("cron1", "cron", None, "cron_complete", None, "dm", None, NOW - 300),
    ("old", "cli", None, "cli_close", None, None, None, NOW - 90 * 86400),
]


def _insert_session(conn, sid, source, parent, end_reason, config, chat_type, key, started):
    conn.execute("INSERT INTO sessions (id, source, user_id, model, model_config, parent_session_id, started_at, "
                 "ended_at, end_reason, chat_id, chat_type, session_key, last_activity_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (sid, source, "268" if source == "discord" else None, "m", json.dumps(config) if config else None,
                  parent, started, started + 60 if end_reason else None, end_reason,
                  "1443" if source == "discord" else None, chat_type, key, started + 60))


MESSAGES = [
    # session, role, content, extra columns
    ("dm2", "user", "[Thu 2026-11-05 03:30:58 PST] " + TRIGGER + "hello there", {}),
    ("dm2", "assistant", "hi!", {"finish_reason": "stop", "reasoning": "r"}),
    ("dm2", "user", "plain without evidence", {}),
    ("dm2", "user", "clean", {"platform_message_id": SNOW, "api_content": TRIGGER + "clean"}),
    ("dm2", "assistant", "Your request was not processed. Send it again if you still want me to carry it out.",
     {"display_kind": "failed_turn", "active": 0, "compacted": 1}),
    ("dm2", "assistant", "", {"api_content": "[interrupt: no assistant output for this turn]",
                              "display_kind": "hidden", "active": 0}),
    ("dm2", "user", "[Cron delivery: Watch forms]\nNew submission.", {}),
    ("dm2", "user", "[Replying to: \"x\"]\n\nyes", {"platform_message_id": SNOW}),
    ("dm2", "assistant", "PR review error for repo#30", {}),
    ("dm2", "tool", "{\"ok\": true}", {"tool_call_id": "c1"}),
    ("child2", "user", "[Parent assignment at 2026-11-05T09:00:00+00:00]\nDo the thing.", {}),
    ("child2", "assistant", "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier.\n\n" + END, {"active": 0}),
    ("child2", "assistant", "Operation interrupted: waiting for model response (3.1s elapsed).", {}),
    ("old", "user", "[Mon 2026-08-03 10:00:00 PDT] ancient", {}),
]


@pytest.fixture
def legacy_db(tmp_path):
    path = tmp_path / "state.db"
    SessionDB(db_path=path).close()  # current schema; the rows below are written as a legacy writer left them
    conn = sqlite3.connect(path)
    for session in SESSIONS:
        _insert_session(conn, *session)
    # Rows that predate the topology triggers: clear the stamps the insert trigger just wrote.
    conn.execute("UPDATE sessions SET root_session_id = NULL, depth = NULL")
    conn.execute("UPDATE sessions SET root_session_id = 'elsewhere' WHERE id = 'hook'")  # a stored value wins
    from agent.session_directory import LEGACY_DELEGATE_SESSIONS_SCHEMA as DELEGATE_SESSIONS_SCHEMA
    conn.executescript(DELEGATE_SESSIONS_SCHEMA)
    conn.execute("INSERT INTO delegate_sessions (session_id, owner_session_id, current_session_id, title, "
                 "initial_goal, assignment, context, workspace, forked_context, route_descriptor, model, provider, "
                 "role, depth, status, created_at, last_activity_at) VALUES ('child', 'dm2', 'child2', 't', 'g', "
                 "'a', 'c', '/ws', 1, '{\"kind\": \"named\", \"provider\": \"cliproxy-claude\"}', 'claude-opus-5-5', "
                 "'cliproxy-claude', 'leaf', 1, 'idle', ?, ?)", (NOW, NOW))
    for index, (sid, role, content, extra) in enumerate(MESSAGES):
        stamp = NOW - 90 * 86400 if sid == "old" else NOW - 1000 + index
        columns = {"session_id": sid, "role": role, "content": content, "timestamp": stamp, **extra}
        conn.execute(f"INSERT INTO messages ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                     tuple(columns.values()))
    conn.commit()
    conn.close()
    jobs = tmp_path / "cron" / "jobs.json"
    jobs.parent.mkdir()
    jobs.write_text(json.dumps({"jobs": [{"id": "fa3abb8746a6", "name": "Watch forms"}]}))
    return path


def _opts(path, **kw):
    return Options(db=path, now=NOW, cron_jobs=path.parent / "cron" / "jobs.json", mirror_ids=(9,), **kw)


def _rows(path, sql, args=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def _by_content(path, needle):
    [row] = _rows(path, "SELECT * FROM messages WHERE content LIKE ? OR api_content LIKE ?", (needle, needle))
    return row


def test_dry_run_writes_nothing(legacy_db):
    before = legacy_db.read_bytes()
    receipt = run(_opts(legacy_db))
    assert receipt["mode"] == "dry-run" and receipt["message_changes_total"] > 0
    assert legacy_db.read_bytes() == before


def test_apply_backfills_sessions(legacy_db):
    receipt = run(_opts(legacy_db, apply=True))
    sessions = {row["id"]: row for row in _rows(legacy_db, "SELECT * FROM sessions")}
    assert {sid: (r["lineage_kind"], r["root_session_id"], r["depth"]) for sid, r in sessions.items()} == {
        "dm": ("root", "dm", 0), "dm2": ("continuation", "dm", 0), "child": ("delegate", "dm", 1),
        "child2": ("continuation", "dm", 1), "grand": ("delegate", "dm", 2), "branch": ("branch", "dm", 0),
        "hook": ("webhook", "elsewhere", 0), "cron1": ("cron", "cron1", 0), "old": ("root", "old", 0)}
    assert receipt["sessions"]["conflict_root_session_id"] == 1
    assert (sessions["child2"]["role"], sessions["child2"]["status"], sessions["child2"]["workspace"]) == \
        ("leaf", "idle", "/ws")
    assert sessions["child"]["status"] == "closed"  # the compressed-away segment of the delegation
    assert json.loads(sessions["child2"]["route"]) == {"provider": "cliproxy-claude", "model": "claude-opus-5-5"}
    assert json.loads(sessions["dm"]["route"]) == {"provider": "cliproxy-gemini", "model": "gemini-4-argon",
                                                   "reasoning_effort": "medium"}
    assert json.loads(sessions["child"]["fork_origin"])["session_id"] == "dm2"
    assert sessions["dm2"]["status"] is None  # live, no delegation record: not derivable


def test_apply_types_messages_and_retags(legacy_db):
    receipt = run(_opts(legacy_db, apply=True))
    human = _by_content(legacy_db, "hello there")
    assert human["content"] == "hello there" and human["sender_kind"] == "human"
    assert human["sender_id"] == "discord:268" and human["source_event_id"] == SNOW
    assert json.loads(human["parts_json"])[0]["origin"]["message_id"] == SNOW
    clean = _by_content(legacy_db, "clean")
    assert clean["sender_kind"] == "human" and clean["api_content"] == TRIGGER + "clean"  # sidecar retained
    cron = _by_content(legacy_db, "New submission.")
    assert (cron["sender_kind"], cron["sender_id"], cron["part_kind"]) == ("cron", "cron:fa3abb8746a6", "cron")
    assignment = _by_content(legacy_db, "Do the thing.")
    assert (assignment["sender_kind"], assignment["sender_id"]) == ("parent", "session:dm2")
    for needle in ("plain without evidence", "[Replying to:%"):
        untouched = _by_content(legacy_db, needle)
        assert untouched["sender_kind"] is None and untouched["parts_json"] is None
    failed = _by_content(legacy_db, "Your request was not processed.%")
    assert (failed["role"], failed["sender_kind"], failed["part_kind"]) == ("user", "system", "error_notice")
    assert (failed["active"], failed["compacted"]) == (0, 1)  # flags preserved
    placeholder = _rows(legacy_db, "SELECT * FROM messages WHERE part_kind = 'interrupt' AND content = ''")[0]
    assert placeholder["role"] == "user" and placeholder["api_content"] is None
    assert _by_content(legacy_db, "Operation interrupted%")["role"] == "user"
    assert _by_content(legacy_db, "[CONTEXT COMPACTION%")["part_kind"] == "compaction"
    mirror = _by_content(legacy_db, "PR review error%")  # row 9, named by --retag-mirror-id
    assert (mirror["role"], mirror["part_kind"], mirror["sender_id"]) == ("user", "send_mirror", "cli:send")
    assert _by_content(legacy_db, "hi!")["role"] == "assistant"  # genuine model row untouched
    assert _rows(legacy_db, "SELECT sender_kind FROM messages WHERE role = 'tool'") == [{"sender_kind": "tool_result"}]
    ancient = _by_content(legacy_db, "%ancient")
    assert ancient["sender_kind"] is None  # 'old' is outside the active scope
    assert receipt["messages_before"] == {"assistant": 6, "tool": 1, "user": 7}
    assert receipt["messages_after"] == {"assistant": 1, "tool": 1, "user": 12}
    assert sum(v for k, v in receipt["messages"].items() if k.startswith("retag:")) == 5


def test_second_apply_changes_nothing(legacy_db):
    run(_opts(legacy_db, apply=True))
    snapshot = _rows(legacy_db, "SELECT * FROM messages ORDER BY id"), _rows(legacy_db, "SELECT * FROM sessions")
    again = run(_opts(legacy_db, apply=True))
    assert again["message_changes_total"] == 0 and again["sessions"]["sessions_changed"] == 0
    assert (_rows(legacy_db, "SELECT * FROM messages ORDER BY id"), _rows(legacy_db, "SELECT * FROM sessions")) \
        == snapshot


def test_scope_all_reaches_archived_sessions(legacy_db):
    run(_opts(legacy_db, apply=True, scope="all"))
    ancient = _by_content(legacy_db, "ancient")
    assert ancient["sender_kind"] == "human" and ancient["content"] == "ancient"


def test_sessiondb_reads_migrated_rows(legacy_db):
    run(_opts(legacy_db, apply=True))
    db = SessionDB(db_path=legacy_db)
    try:
        conversation = db.get_messages_as_conversation("dm2", include_inactive=True)
        typed = [m for m in conversation if m.get("_parts")]
        assert {m["content"] for m in typed} >= {"hello there", "clean", "New submission."}
        assert all(m["role"] == "user" for m in typed)
        notice = next(m for m in conversation if m.get("part_kind") == "error_notice")
        assert notice["_parts"][0]["kind"] == "error_notice" and notice["role"] == "user"
    finally:
        db.close()


def test_cli_refuses_missing_db_and_writes_receipt(legacy_db, capsys):
    assert main(["--db", str(legacy_db.parent / "missing.db")]) == 2
    receipt_path = legacy_db.parent / "receipt.json"
    assert main(["--db", str(legacy_db), "--now", str(NOW), "--receipt", str(receipt_path)]) == 0
    assert json.loads(receipt_path.read_text())["mode"] == "dry-run"
    assert '"mode": "dry-run"' in capsys.readouterr().out
