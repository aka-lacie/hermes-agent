"""Typed live input (unified-session plan §4.8 Layer 2, Phase 2c step 1).

A live gateway turn carries typed parts + provenance into the agent loop instead of a pre-flattened
string: a person's message is one ``human`` part whose origin holds the ids/time (no triggering note,
no timestamp head), a mid-turn steer is a ``human`` part with ``delivery="steer"``, and a self-injected
notification is a ``system_event`` part that can never speak with the human's authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from agent import message_parts as mp
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.typed_input import turn_typed_input


def _discord_event(text="hello there", message_id="1567683734684041408", **kw):
    source = SessionSource(platform=Platform.DISCORD, chat_id="42", chat_type="dm", user_id="268934270939037698")
    return SimpleNamespace(text=text, message_id=message_id, source=source, internal=False, metadata=kw.pop("metadata", {}),
                           timestamp=datetime(2026, 11, 4, 22, 0, 40, tzinfo=timezone.utc), **kw), source


def test_a_platform_message_is_one_human_part_with_its_origin():
    event, source = _discord_event()
    typed = turn_typed_input(event, source, "hello there", 1762293640.0, None)
    assert typed["sender_kind"] == "human" and typed["sender_id"] == "discord:268934270939037698"
    assert typed["source_event_id"] == "1567683734684041408" and typed["sent_at"] == 1762293640.0
    (part,) = typed["parts"]
    assert part["type"] == "human" and part["delivery"] == "turn" and part["text"] == "hello there"
    assert part["origin"] == {"platform": "discord", "message_id": "1567683734684041408", "channel_id": "42",
                              "sent_at": 1762293640.0}
    # The plain view is exactly what the human wrote: no note, no timestamp head.
    assert mp.plain_text(typed["parts"]) == "hello there"


def test_a_turn_start_steer_is_typed_as_a_steer_delivery():
    event, source = _discord_event(metadata={"delivery": "steer"})
    typed = turn_typed_input(event, source, "also this", None, None)
    assert typed["parts"][0]["delivery"] == "steer" and typed["intent"] == "steer"


def test_self_injected_notifications_become_system_events_never_human():
    event, source = _discord_event(text="[SYSTEM: done]")
    event.internal = True
    for display_kind, kind in (("process_complete", "process_complete"),
                               ("async_delegation_complete", "subagent_complete"),
                               ("internal_notification", "notification")):
        typed = turn_typed_input(event, source, "done", None, display_kind)
        assert typed["sender_kind"] == "system" and typed["sender_id"] == f"system:{kind}"
        assert typed["part_kind"] == kind
        assert [(p["type"], p["kind"]) for p in typed["parts"]] == [("system_event", kind)]
    # An internal event of a kind this module does not type stays untyped.
    assert turn_typed_input(event, source, "done", None, "hidden") is None


def test_untyped_surfaces_stay_untyped():
    event, source = _discord_event()
    local = SessionSource(platform=Platform.LOCAL, chat_id="cli")
    assert turn_typed_input(event, local, "hi", None, None) is None
    assert turn_typed_input(event, source, "", None, None) is None
    assert turn_typed_input(event, source, [{"type": "text", "text": "x"}], None, None) is None
    heartbeat, _ = _discord_event(_heartbeat_session_id="s")
    assert turn_typed_input(heartbeat, source, "tick", None, None) is None


def test_pii_redaction_hashes_the_model_facing_ids():
    event, _ = _discord_event()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", user_id="7")
    typed = turn_typed_input(event, source, "hi", None, None, redact_pii=True)
    origin = typed["parts"][0]["origin"]
    assert origin["message_id"] != "1567683734684041408" and origin["channel_id"] != "42"
    assert typed["sender_id"].startswith("telegram:user_")


def test_turn_context_types_the_turn_dict_only_when_the_text_matches():
    from agent.turn_context import _stage_turn_user_message

    agent = SimpleNamespace(_pending_cli_user_message=None)
    payload = mp.human_turn_input("hello", origin={"platform": "discord", "message_id": "9"})
    msg, _ = _stage_turn_user_message(agent, "hello", "hello", 1.0, "9", None, None, typed_input=payload)
    assert msg["content"] == "hello" and msg["sender_kind"] == "human" and msg["source_event_id"] == "9"
    assert msg["_parts"][0]["origin"]["message_id"] == "9"
    # A decorated model-facing text never gets parts that would drop the decoration.
    msg, _ = _stage_turn_user_message(agent, "[note]\n\nhello", "hello", 1.0, "9", None, None, typed_input=payload)
    assert "_parts" not in msg and msg["content"] == "[note]\n\nhello"


def test_gateway_sends_the_plain_view_only_for_an_undecorated_turn():
    from gateway.run_turn_runner import _typed_input_for

    payload = mp.human_turn_input("hello", origin={"platform": "discord", "message_id": "9"})
    ctx = SimpleNamespace(typed_input={"input": payload, "untyped_message": "[Triggering message id: `9`]\n\nhello"})
    assert _typed_input_for(ctx, "[Triggering message id: `9`]\n\nhello", "hello") == (payload, "hello")
    # The runner prepended an API-only note: the turn keeps its untyped bytes.
    assert _typed_input_for(ctx, "[System note: x]\n\n[Triggering message id: `9`]\n\nhello", "hello") is None
    assert _typed_input_for(SimpleNamespace(typed_input=None), "hello", None) is None


def test_typed_steer_carries_its_origin_into_the_steer_row():
    from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._pending_steer = None
    agent.steer("check auth.log", origin={"platform": "discord", "message_id": "77", "sent_at": 5.0})
    agent.steer("and syslog")
    messages = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
                {"role": "tool", "content": "out", "tool_call_id": "a"}]
    apply_pending_steer_to_tool_results(agent, messages, 1)
    row = messages[-1]
    assert row["display_kind"] == "steer" and row["sender_kind"] == "human" and row["intent"] == "steer"
    assert row["source_event_id"] == "77" and row["sent_at"] == 5.0
    assert [(p["text"], p["origin"]) for p in row["_parts"]] == [
        ("check auth.log", {"platform": "discord", "message_id": "77", "sent_at": 5.0}), ("and syslog", {})]
    assert row["content"] == "check auth.log\n\nand syslog"
