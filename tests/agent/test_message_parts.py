"""agent.message_parts: typed semantic parts riding on message dicts as ``_parts``.

Contract: JSON-shaped parts, ``content`` is always their deterministic plain view, only the helpers edit
them, and typing is built from structured fields only (never by parsing text).
"""
import json

import pytest

from agent import message_parts as mp


def test_constructors_emit_the_canonical_json_shape():
    human = mp.human_message("hi", delivery="steer",
                             origin={"platform": "discord", "message_id": "42", "sent_at": 1.5, "channel_id": None})
    assert human == {"type": "human", "v": 1, "text": "hi", "delivery": "steer",
                     "origin": {"platform": "discord", "message_id": "42", "sent_at": 1.5}}
    event = mp.system_event("cron", "done", meta={"job": "j1"})
    assert event == {"type": "system_event", "v": 1, "kind": "cron", "text": "done", "meta": {"job": "j1"}}
    assert mp.turn_injection("jev_system1", "hint") == {"type": "injection", "v": 1, "kind": "jev_system1",
                                                        "text": "hint"}
    sha = "a" * 64
    assert mp.media_ref(sha, "image/png", "/tmp/x.png") == {"type": "media", "v": 1, "sha256": sha,
                                                             "mime": "image/png", "path": "/tmp/x.png"}
    for part in (human, event):
        assert json.loads(json.dumps(part)) == part


@pytest.mark.parametrize("part", [
    {"type": "human", "v": 1, "text": "x", "delivery": "shout", "origin": {}},
    {"type": "human", "v": 2, "text": "x", "delivery": "turn", "origin": {}},
    {"type": "human", "v": 1, "text": "x", "delivery": "turn", "origin": {"sent_at": float("nan")}},
    {"type": "human", "v": 1, "text": "x", "delivery": "turn", "origin": {"user": "u"}},
    {"type": "human", "v": 1, "text": "x", "delivery": "turn"},
    {"type": "system_event", "v": 1, "kind": "", "text": "x", "meta": {}},
    {"type": "system_event", "v": 1, "kind": "k", "text": "x", "meta": {"bad": object()}},
    {"type": "injection", "v": 1, "kind": "k", "text": 3},
    {"type": "media", "v": 1, "sha256": "ABC", "mime": "image/png", "path": "p"},
    {"type": "media", "v": 1, "sha256": "a" * 64, "mime": "png", "path": "p"},
    {"type": "steer", "v": 1, "text": "x"},
    "text",
])
def test_validate_rejects_malformed_parts(part):
    with pytest.raises(ValueError):
        mp.validate_parts([part])


def test_plain_text_is_the_deterministic_text_projection():
    # What the row's senders wrote: harness injections and media never reach ``content``.
    parts = [mp.human_message("one"), mp.system_event("cron", ""), mp.media_ref("b" * 64, "image/png", "p"),
             mp.turn_injection("hint", "two"), mp.system_event("webhook", "three")]
    assert mp.plain_text(parts) == "one\n\nthree"
    assert mp.plain_text([]) == ""


def test_set_parts_regenerates_content_and_drops_stale_state():
    msg = {"role": "user", "content": "stale", "api_content": "stale bytes", "_db_persisted": True}
    mp.set_parts(msg, [mp.human_message("fresh")])
    assert msg["content"] == "fresh" and "api_content" not in msg and "_db_persisted" not in msg
    mp.set_parts(msg, msg["_parts"])  # unchanged parts on a durable dict keep the stamp
    msg["_db_persisted"] = True
    mp.set_parts(msg, [mp.human_message("fresh")])
    assert msg["_db_persisted"] is True
    with pytest.raises(ValueError):
        mp.set_parts({"role": "assistant", "content": ""}, [mp.human_message("x")])


def test_set_parts_stores_a_copy():
    parts = [mp.system_event("cron", "x", meta={"ids": [1]})]
    msg = mp.set_parts({"role": "user"}, parts)
    parts[0]["meta"]["ids"].append(2)
    assert msg["_parts"][0]["meta"] == {"ids": [1]}


def test_append_parts_types_an_untyped_turn_as_it_was_rendered():
    msg = {"role": "user", "content": "the human's words"}
    mp.append_parts(msg, [mp.turn_injection("hint", "context")])
    assert [p["type"] for p in msg["_parts"]] == ["human", "injection"]
    assert msg["content"] == "the human's words"  # the injection rides the parts, not the plain view
    with pytest.raises(ValueError):
        mp.append_parts({"role": "user", "content": [{"type": "text", "text": "x"}]}, [mp.human_message("y")])


def test_merge_concatenates_parts_and_matches_the_legacy_text_join():
    prev = mp.system_event_message("delivery_mirror", "sent elsewhere", sender_id="session:a")
    human = {"role": "user", "content": "and now me"}
    assert mp.merge(prev, human) is True
    assert [p["type"] for p in prev["_parts"]] == ["system_event", "human"]
    assert prev["content"] == "sent elsewhere\n\nand now me"
    assert prev["sender_kind"] == "system"  # the survivor keeps its own row provenance
    untyped = {"role": "user", "content": "a"}
    assert mp.merge(untyped, {"role": "user", "content": "b"}) is False
    assert untyped == {"role": "user", "content": "a"}  # nothing touched: the caller's text merge runs
    typed = mp.system_event_message("cron", "c", sender_id="cron:j")
    assert mp.merge(typed, {"role": "user", "content": [{"type": "text", "text": "img"}]}) is False


def test_a_loaded_typed_row_is_still_its_parts_plain_view():
    """SessionDB loads a user row's string stripped; its parts keep their whitespace. That row is still
    consistent (it is re-stored and rendered typed), while a real rewrite of its text is not."""
    msg = mp.system_event_message("peer_message", "  from a peer  ", sender_id="a", sender_kind="peer")
    loaded = {**msg, "content": msg["content"].strip()}
    assert mp.parts_consistent(loaded) and mp.is_plain_view(loaded["content"], loaded["_parts"])
    assert not mp.parts_consistent({**msg, "content": "from a"})


def test_an_untyped_side_is_typed_from_the_text_every_reader_sees():
    assert mp.untyped_content({"role": "user", "content": "  hi  "}) == "hi"
    assert [p["text"] for p in mp.untyped_parts({"role": "user", "content": "  hi  "}) or []] == ["hi"]
    blocks = [{"type": "text", "text": " a "}]  # list content is never normalized (neither loader nor send path)
    assert [p["text"] for p in mp.untyped_parts({"role": "user", "content": blocks}) or []] == [" a "]


def test_build_parts_reads_columns_never_text():
    # A legacy row whose text LOOKS like a cron envelope stays untyped: no text is parsed.
    legacy = {"role": "user", "content": "[Cron delivery: nightly]\nall good"}
    assert mp.build_parts(legacy) is None
    assert mp.build_parts({"role": "assistant", "content": "x", "sender_kind": "human"}) is None
    human = mp.build_parts({"role": "user", "content": "hi", "sender_kind": "human", "sent_at": 10.0,
                            "source_event_id": "m1"})
    assert human == [mp.human_message("hi", origin={"sent_at": 10.0, "message_id": "m1"})]
    steer = mp.build_parts({"role": "user", "content": "wait", "sender_kind": "human", "intent": "steer"})
    assert steer[0]["delivery"] == "steer"
    event = mp.build_parts({"role": "user", "content": "done", "sender_kind": "cron", "part_kind": "cron"})
    assert event == [mp.system_event("cron", "done")]
    assert mp.build_parts({"role": "user", "content": "out", "sender_kind": "tool_result"}) is None
    stored = mp.encode_parts_json([mp.human_message("x")])
    assert mp.build_parts({"role": "user", "content": "x", "parts_json": stored}) == [mp.human_message("x")]


def test_build_parts_types_untyped_rows_only_on_request():
    marker = {"role": "user", "content": "switched model", "display_kind": "model_switch"}
    assert mp.build_parts(marker) is None
    assert mp.build_parts(marker, type_untyped=True) == [mp.system_event("model_switch", "switched model")]
    steer = mp.build_parts({"role": "user", "content": "s", "display_kind": "steer"}, type_untyped=True)
    assert steer == [mp.human_message("s", delivery="steer")]


def test_parts_json_round_trip_is_canonical_and_fails_closed():
    parts = [mp.human_message("é"), mp.system_event("k", "t", meta={"b": 1, "a": 2})]
    raw = mp.encode_parts_json(parts)
    assert raw == mp.encode_parts_json(json.loads(raw))
    assert mp.decode_parts_json(raw) == parts
    assert mp.decode_parts_json("not json") is None
    assert mp.decode_parts_json(json.dumps([{"type": "human", "v": 9}])) is None
    assert mp.decode_parts_json(None) is None


def test_typed_and_copy_fields_carry_typing_without_aliasing():
    msg = mp.system_event_message("cron", "x", sender_id="cron:j", sent_at=5, source_event_id=7)
    fields = mp.typed_fields(msg)
    assert fields["sender_id"] == "cron:j" and fields["sent_at"] == 5.0 and fields["source_event_id"] == "7"
    fields["_parts"][0]["text"] = "mutated"
    assert msg["_parts"][0]["text"] == "x"
    assert mp.typed_fields({"role": "user", "content": "plain"}) == {}
    assert mp.copy_row_fields({"role": "assistant", "content": "a"}) == {mp.ASSISTANT_ORIGIN: mp.COPY_ORIGIN}


def test_system_event_message_rejects_human_or_anonymous_senders():
    with pytest.raises(ValueError):
        mp.system_event_message("k", "t", sender_id="discord:1", sender_kind="human")
    with pytest.raises(ValueError):
        mp.system_event_message("k", "t", sender_id="")


def test_current_turn_inputs_is_a_read_only_copy():
    messages = [{"role": "user", "content": "old"}, mp.system_event_message("cron", "now", sender_id="cron:j")]
    inputs = mp.current_turn_inputs(messages, 1)
    inputs[0]["text"] = "changed"
    assert messages[1]["_parts"][0]["text"] == "now"
    assert mp.current_turn_inputs(messages, 0) == []
    assert mp.current_turn_inputs(messages, None) == []
    assert mp.current_turn_inputs(messages, 9) == []
