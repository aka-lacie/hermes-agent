"""Responses API: ``role="developer"`` for harness turn injections only (Phase 2c step 4).

The chat->Responses conversion, its user branch and the role-message preflight accept ``developer``,
routing it like a user message (``input_text``; typed parts on the ChatGPT Codex backend, string
shorthand elsewhere). The Responses-route renderer emits injections as developer items and NEVER a
system event: their text comes from other sessions, external systems or summaries, and developer
outranks user in the instruction hierarchy.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import message_parts as mp
from agent.codex_responses_adapter import _chat_messages_to_responses_input, _preflight_codex_input_items
from agent.wire_render import RESPONSES_RENDERER, render_messages, resolve_renderer

# Every SystemEvent kind the plan names (§4.8 Layer 2, §4.7) plus every kind core produces today.
SYSTEM_EVENT_KINDS = (
    "subagent_complete", "subagent_failed", "process_complete", "process_watch", "process_heartbeat", "cron",
    "webhook", "compaction", "continuation_nudge", "error_notice", "interrupt", "peer_message", "notification",
    "delivery_mirror", "unattributed_assistant_write", "mcp_reload", "model_switch", "parent_message",
)


@pytest.mark.parametrize("issuer,expected_content", [
    ("codex_backend", [{"type": "input_text", "text": "route hint"}]),
    (None, "route hint"),
])
def test_developer_rows_reach_the_wire_as_developer_items(issuer, expected_content):
    items = _chat_messages_to_responses_input(
        [{"role": "user", "content": "hi"}, {"role": "developer", "content": "route hint"}],
        current_issuer_kind=issuer)
    assert items[1] == {"type": "message", "role": "developer", "content": expected_content}
    # ...and preflight keeps them (it used to raise "unsupported item shape" for this role).
    assert _preflight_codex_input_items(items)[1]["role"] == "developer"


def test_preflight_types_developer_parts_as_input_text():
    [item] = _preflight_codex_input_items(
        [{"type": "message", "role": "developer", "content": [{"type": "text", "text": "x"}]}])
    assert item == {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "x"}]}


def test_the_responses_route_renders_injections_as_developer_items():
    agent = SimpleNamespace(provider="openai-codex", api_mode="codex_responses")
    assert resolve_renderer(agent) is RESPONSES_RENDERER
    turn = {"role": "user"}
    mp.set_parts(turn, [mp.human_message("what changed?", origin={"message_id": "9"}),
                        mp.turn_injection("jev_system1", "route hint"),
                        mp.turn_injection("post_compaction_reminder", "re-read the plan")])
    out = render_messages(agent, [turn])
    assert out == [
        {"role": "user", "content": '<origin message_id="9"/>\nwhat changed?'},
        {"role": "developer", "content": '<system-reminder kind="jev_system1">\nroute hint\n</system-reminder>'},
        {"role": "developer",
         "content": '<system-reminder kind="post_compaction_reminder">\nre-read the plan\n</system-reminder>'},
    ]
    items = _chat_messages_to_responses_input(out, current_issuer_kind="codex_backend")
    assert [i["role"] for i in items] == ["user", "developer", "developer"]


@pytest.mark.parametrize("kind", SYSTEM_EVENT_KINDS)
def test_no_system_event_kind_ever_renders_to_a_developer_item(kind):
    agent = SimpleNamespace(provider="openai-codex", api_mode="codex_responses")
    meta = {"sender_id": "s2", "relation": "sibling"} if kind == "peer_message" else {"status": "ok"}
    event = mp.system_event_message(kind, f"{kind} text", sender_id=f"system:{kind}", meta=meta)
    human = {"role": "user"}
    mp.set_parts(human, [mp.human_message("me"), mp.system_event(kind, "folded event", meta=meta)])
    out = render_messages(agent, [event, human])
    assert all(m["role"] == "user" for m in out), out
    items = _chat_messages_to_responses_input(out, current_issuer_kind="codex_backend")
    assert all(i.get("role") != "developer" for i in items), items
