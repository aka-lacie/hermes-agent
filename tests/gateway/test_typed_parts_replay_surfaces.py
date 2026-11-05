"""Key-preservation rule 1: every allowlist copy that keeps ``message_uid`` keeps the typed parts too.

One typed transcript runs through every replay/copy surface (store load, agent flush row, gateway replay,
gateway transcript append and its shutdown-spool replay, fork validation, branch/seed copies, the two
consecutive-user merges) and must come out with its ``_parts`` and provenance intact.
"""
import json
from types import SimpleNamespace

from agent import message_parts as mp
from hermes_state import SessionDB


def _typed_transcript():
    return [
        mp.system_event_message("delivery_mirror", "sent from the other chat", sender_id="session:other",
                                meta={"source": "discord"}, sent_at=100.0, source_event_id="e1"),
        {"role": "assistant", "content": "noted", mp.ASSISTANT_ORIGIN: mp.MODEL_ORIGIN},
        mp.system_event_message("cron", "nightly ok", sender_id="cron:nightly"),
    ]


def _assert_typed(msg, source):
    assert msg["_parts"] == source["_parts"], msg
    assert msg["content"] == mp.plain_text(source["_parts"])
    for key in ("sender_kind", "sender_id", "part_kind"):
        assert msg[key] == source[key]


def _stored(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", source="cli")
    db.append_messages_batch("s", _typed_transcript())
    return db


def test_store_load_and_agent_flush_keep_parts(tmp_path):
    from agent.session_persistence import _db_flush_row
    db = _stored(tmp_path)
    try:
        source = _typed_transcript()
        for repair in (False, True):
            loaded = db.get_messages_as_conversation("s", repair_alternation=repair)
            _assert_typed(loaded[0], source[0])
            _assert_typed(loaded[2], source[2])
        model_history, display_history = db.get_resume_conversations("s")
        _assert_typed(model_history[0], source[0])
        _assert_typed(display_history[0], source[0])
        row = _db_flush_row(SimpleNamespace(), loaded[0], False)
        _assert_typed(row, source[0])
        assert _db_flush_row(SimpleNamespace(), loaded[1], False)[mp.ASSISTANT_ORIGIN] == mp.MODEL_ORIGIN
    finally:
        db.close()


def test_gateway_replay_keeps_parts_and_leaves_typed_content_alone(tmp_path):
    from gateway.run import _build_gateway_agent_history
    db = _stored(tmp_path)
    try:
        history = db.get_messages_as_conversation("s")
        replay, _ = _build_gateway_agent_history(history, inject_timestamps=True)
        source = _typed_transcript()
        _assert_typed(replay[0], source[0])  # no timestamp rendered into a typed row's content
        _assert_typed(replay[2], source[2])
    finally:
        db.close()


def test_gateway_transcript_append_and_spool_replay_keep_parts(tmp_path):
    from gateway.session_transcript import transcript_append_kwargs
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", source="cli")
        for message in _typed_transcript():
            db.append_message(**transcript_append_kwargs("s", message))
        spooled = json.loads(json.dumps(_typed_transcript()))  # the shutdown spool is JSON on disk
        for message in spooled:
            db.append_message(**transcript_append_kwargs("s", message))  # the origin rides in the spool
        loaded = db.get_messages_as_conversation("s")
        source = _typed_transcript()
        for offset in (0, 3):
            _assert_typed(loaded[offset], source[0])
            _assert_typed(loaded[offset + 2], source[2])
        assert [m["role"] for m in loaded] == ["user", "assistant", "user"] * 2
    finally:
        db.close()


def test_fork_and_branch_copies_keep_parts(tmp_path):
    from agent.message_metadata import message_identity
    from agent.subagent_fork_validation import validate_fork_history
    from gateway.slash_commands_session import _branch_row
    db = _stored(tmp_path)
    try:
        history = db.get_messages_as_conversation("s")
        source = _typed_transcript()
        forked = validate_fork_history(history)
        _assert_typed(forked[0], source[0])
        assert "_db_persisted" not in forked[0]  # persistence identity still resets
        gateway_rows = [_branch_row(msg) for msg in history]
        seed_rows = [{"role": msg["role"], "content": msg["content"], **message_identity(msg)} for msg in history]
        for rows in (gateway_rows, seed_rows):
            _assert_typed(rows[0], source[0])
            assert rows[1][mp.ASSISTANT_ORIGIN] == mp.COPY_ORIGIN
            db.create_session(f"branch-{id(rows)}", source="cli", parent_session_id="s",
                              model_config={"_branched_from": "s"})
            db.append_messages_batch(f"branch-{id(rows)}", rows)
            _assert_typed(db.get_messages_as_conversation(f"branch-{id(rows)}")[0], source[0])
    finally:
        db.close()


def test_consecutive_user_merges_concatenate_parts():
    from agent.agent_runtime_helpers import _merge_consecutive_users, drop_thinking_only_and_merge_users
    event = mp.system_event_message("delivery_mirror", "mirrored", sender_id="session:other")
    human = {"role": "user", "content": "my next message"}
    merged, repairs = _merge_consecutive_users([dict(event), dict(human)])
    assert repairs == 1 and len(merged) == 1
    assert [p["type"] for p in merged[0]["_parts"]] == ["system_event", "human"]
    assert merged[0]["content"] == "mirrored\n\nmy next message"  # the legacy merged text
    original = [dict(event), {"role": "assistant", "content": "", "reasoning": "only thinking"}, dict(human)]
    api = drop_thinking_only_and_merge_users(original)
    assert len(api) == 1 and [p["type"] for p in api[0]["_parts"]] == ["system_event", "human"]
    assert original[0]["_parts"] == event["_parts"]  # the caller's dicts are never mutated
