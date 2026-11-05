"""SessionDB persists and restores message provenance: the ``messages`` provenance columns, and typed
parts as ``parts_json`` <-> ``msg["_parts"]``. Stored parts never disagree with ``content``."""
from agent import message_parts as mp
from hermes_state import SessionDB


def _db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", source="cli")
    return db


def test_append_message_round_trips_parts_and_provenance(tmp_path):
    db = _db(tmp_path)
    try:
        parts = [mp.human_message("hello", origin={"platform": "discord", "message_id": "m1", "sent_at": 100.0})]
        db.append_message("s", "user", parts=parts, sender_kind="human", sender_id="discord:1", sent_at=100.0,
                          source_event_id="m1", intent="queue", turn_outcome="completed")
        [loaded] = db.get_messages_as_conversation("s")
        assert loaded["content"] == "hello"  # content is the parts' plain view
        assert loaded["_parts"] == parts
        assert (loaded["sender_kind"], loaded["sender_id"], loaded["sent_at"], loaded["source_event_id"],
                loaded["intent"], loaded["turn_outcome"]) == ("human", "discord:1", 100.0, "m1", "queue", "completed")
    finally:
        db.close()


def test_batch_append_round_trips_typed_rows_and_leaves_legacy_rows_untyped(tmp_path):
    db = _db(tmp_path)
    try:
        event = mp.system_event_message("cron", "nightly ok", sender_id="cron:nightly", sent_at=5.0)
        db.append_messages_batch("s", [event, {"role": "user", "content": "plain"},
                                       {"role": "assistant", "content": "reply", mp.ASSISTANT_ORIGIN: mp.MODEL_ORIGIN}])
        typed, plain, reply = db.get_messages_as_conversation("s")
        assert typed["_parts"] == event["_parts"] and typed["part_kind"] == "cron"
        assert typed["sender_kind"] == "system" and typed["sender_id"] == "cron:nightly"
        assert "_parts" not in plain and "sender_kind" not in plain
        assert "_parts" not in reply and mp.ASSISTANT_ORIGIN not in reply  # the origin is a write-time claim
    finally:
        db.close()


def test_content_rewritten_around_its_parts_is_stored_untyped(tmp_path, caplog):
    db = _db(tmp_path)
    try:
        msg = mp.system_event_message("cron", "original", sender_id="cron:j")
        msg["content"] = "rewritten without the helpers"
        db.append_messages_batch("s", [msg])
        [loaded] = db.get_messages_as_conversation("s")
        assert loaded["content"] == "rewritten without the helpers"
        assert "_parts" not in loaded
        assert loaded["sender_id"] == "cron:j"  # provenance is identity and survives
        assert "rewritten around its typed parts" in caplog.text
    finally:
        db.close()


def test_rewrite_of_a_typed_row_keeps_its_provenance(tmp_path):
    db = _db(tmp_path)
    try:
        db.append_messages_batch("s", [mp.system_event_message("cron", "v1", sender_id="cron:j")])
        [live] = db.get_messages_as_conversation("s", repair_alternation=True)
        mp.set_parts(live, [mp.system_event("cron", "v2")])
        live.pop("sender_id")  # a lossy copy dropped the provenance; the row keeps it
        db.append_messages_batch("s", [live])
        [loaded] = db.get_messages_as_conversation("s")
        assert loaded["content"] == "v2" and loaded["_parts"][0]["text"] == "v2"
        assert loaded["sender_id"] == "cron:j"
        assert db._conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = 's'").fetchone()[0] == 1
    finally:
        db.close()


def test_restored_copy_without_parts_is_not_reinserted(tmp_path):
    db = _db(tmp_path)
    try:
        db.append_messages_batch("s", [mp.system_event_message("cron", "once", sender_id="cron:j")])
        [loaded] = db.get_messages_as_conversation("s")
        copy = {k: v for k, v in loaded.items() if not k.startswith("_")}  # a wire-crossing handoff
        copy["_restored_from_store"] = True
        db.append_messages_batch("s", [copy])
        assert db._conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = 's'").fetchone()[0] == 1
    finally:
        db.close()
