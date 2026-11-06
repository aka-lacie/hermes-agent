"""Review R5: a typed row merged with an untyped (legacy) user row keeps BOTH sides in its parts.

After a thinking-only assistant turn is dropped from the API copy, the typed peer/event row and an untyped
legacy human row merge into one user turn. On an adapter-owned (``native``) route the adapter reads only
``_parts``, so the untyped side must join as the parts its columns give it (a human turn for a legacy plain
user row; an event for an event row, never the other way round). Media an untyped list carries cannot be
typed: that pair stays two rows. The core default route keeps both sides in its request bytes too, and
the merged request is identical live and replayed from state.db.
"""
from __future__ import annotations

import json
import logging

import pytest

from agent import message_parts as mp
from agent import wire_render as wr

HUMAN = "HUMAN_MARKER"
PEER = "PEER_MARKER"
PROVIDER = "fake-native"


def _peer():
    return mp.system_event_message("peer_message", PEER, sender_id="a", sender_kind="peer",
                                   meta={"sender_id": "a", "relation": "sibling"})


def _legacy(content=HUMAN, **extra):
    return {"role": "user", "content": content, **extra}


def _thinking():
    return {"role": "assistant", "content": "", "reasoning": "thinking-only", mp.ASSISTANT_ORIGIN: mp.MODEL_ORIGIN}


def _conversation(typed_first: bool, legacy=None):
    legacy = legacy if legacy is not None else _legacy()
    first, second = (_peer(), legacy) if typed_first else (legacy, _peer())
    return [first, _thinking(), second]


class _NativeRenderer(wr.WireRenderer):
    """A native route whose adapter reads ``_parts`` only (as the cliproxy Gemini adapter does)."""
    name = PROVIDER
    native = True


def _adapter_view(api_messages):
    """What a parts-reading native adapter sends: each typed row's parts (type, text), else the content."""
    out = []
    for msg in api_messages:
        if msg.get("role") != "user":
            continue
        if mp.has_parts(msg):
            out.append([(p["type"], p.get("text")) for p in msg[mp.MESSAGE_PARTS]])
        else:
            out.append([("untyped", msg.get("content"))])
    return out


@pytest.fixture
def agent():
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.base_url = "custom", "m", "http://127.0.0.1:1/v1"
    agent.api_mode = "chat_completions"
    agent._current_turn_timestamp = 1793834778.0
    agent._wire_message_timestamps_enabled = False
    agent.ephemeral_system_prompt = ""
    agent._copy_reasoning_content_for_api = lambda *args: None
    agent._should_sanitize_tool_calls = lambda: False
    agent.prefill_messages = []
    agent.tools = []
    agent.context_compressor = None
    agent._use_prompt_caching = False
    agent.session_id = "review"
    agent._session_db = None
    return agent


@pytest.fixture
def native(agent):
    agent.provider = PROVIDER
    wr.register_renderer(_NativeRenderer(), provider=PROVIDER)
    try:
        yield agent
    finally:
        wr.unregister_renderer(provider=PROVIDER)


def _assemble(agent, messages):
    from agent import agent_runtime_helpers as helpers
    from agent.turn_request_assembly import assemble_api_request
    helpers.repair_message_sequence(agent, messages)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]  # the canonical rows stay apart
    return assemble_api_request(
        agent, messages=messages, current_turn_user_idx=len(messages) - 1, _ext_prefetch_cache="",
        _plugin_user_context="", moa_config=None, active_system_prompt="sys", original_user_message="",
        pending_moa_prepared_request=None, request_logger=logging.getLogger(__name__)).api_messages


# ── the merge itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("typed_first", [True, False], ids=["typed-first", "legacy-first"])
def test_rendered_merge_carries_the_untyped_side_as_its_column_parts(typed_first):
    from agent.agent_runtime_helpers import drop_thinking_only_and_merge_users
    [rendered] = _NativeRenderer().render_row(_peer(), wr._MediaWindow(set()))
    rows = [rendered, _thinking(), _legacy()] if typed_first else [_legacy(), _thinking(), rendered]
    [merged] = drop_thinking_only_and_merge_users(rows)
    kinds = [(p["type"], p.get("kind"), p["text"]) for p in merged[mp.MESSAGE_PARTS]]
    expected = [("system_event", "peer_message", PEER), ("human", None, HUMAN)]
    assert kinds == (expected if typed_first else expected[::-1])
    assert merged[mp.RENDERED] is True
    assert HUMAN in merged["content"] and PEER in merged["content"]  # the caller's wire-text join stands
    assert mp.has_parts(rows[0 if typed_first else 2]) and len(rows[0 if typed_first else 2][mp.MESSAGE_PARTS]) == 1


def test_an_untyped_event_row_never_joins_as_human():
    from agent.prompt_builder import STEER_DISPLAY_KIND
    rendered = {**_peer(), mp.RENDERED: True}
    cases = {
        "display kind": (_legacy("Command done", display_kind="process_complete"), ("system_event", "process_complete")),
        "summary": (_legacy("[CONTEXT COMPACTION] earlier", _compressed_summary=True),
                    ("system_event", mp.COMPACTION_EVENT_KIND)),
        "stored sender": (_legacy("New submission.", sender_kind="cron", part_kind="cron"), ("system_event", "cron")),
        "steer": (_legacy("also this", display_kind=STEER_DISPLAY_KIND), ("human", None)),
        "plain legacy": (_legacy(), ("human", None)),
    }
    for label, (row, (ptype, kind)) in cases.items():
        prev = dict(rendered)
        assert mp.parts_mergeable(prev, row), label
        mp.merge(prev, row)
        part = prev[mp.MESSAGE_PARTS][-1]
        assert (part["type"], part.get("kind")) == (ptype, kind), label
        assert part["text"] == row["content"], label
    text_list = _legacy([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    prev = dict(rendered)
    mp.merge(prev, text_list)
    assert [(p["type"], p["text"]) for p in prev[mp.MESSAGE_PARTS][1:]] == [("human", "a"), ("human", "b")]


@pytest.mark.parametrize("typed_first", [True, False], ids=["typed-first", "legacy-first"])
def test_an_untyped_image_side_stays_its_own_row_beside_a_rendered_one(typed_first):
    from agent.agent_runtime_helpers import drop_thinking_only_and_merge_users
    image = _legacy([{"type": "text", "text": HUMAN}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}])
    rendered = {**_peer(), mp.RENDERED: True}
    rows = [rendered, _thinking(), image] if typed_first else [image, _thinking(), rendered]
    assert not mp.parts_mergeable(rows[0], rows[2])
    out = drop_thinking_only_and_merge_users(rows)
    assert [m["role"] for m in out] == ["user", "user"]
    assert out[0 if typed_first else 1][mp.MESSAGE_PARTS][0]["text"] == PEER
    assert out[1 if typed_first else 0]["content"] == image["content"]  # image bytes and text untouched
    # Without an adapter-owned row nothing changes: the text/list pair merges as before.
    plain = drop_thinking_only_and_merge_users([_legacy("x"), _thinking(), image])
    assert len(plain) == 1 and plain[0]["content"][-1]["type"] == "image_url"


# ── through the real request assembly ────────────────────────────────────────

@pytest.mark.parametrize("typed_first", [True, False], ids=["typed-first", "legacy-first"])
def test_native_route_parts_hold_both_sides_after_assembly(native, typed_first):
    api = _assemble(native, _conversation(typed_first))
    [user] = [m for m in api if m["role"] == "user"]
    view = _adapter_view([user])[0]
    assert ("human", HUMAN) in view and ("system_event", PEER) in view
    assert view.index(("human", HUMAN)) == (1 if typed_first else 0)  # source order kept
    assert HUMAN in user["content"] and PEER in user["content"]


@pytest.mark.parametrize("typed_first", [True, False], ids=["typed-first", "legacy-first"])
def test_default_route_request_bytes_hold_both_sides(agent, typed_first):
    api = _assemble(agent, _conversation(typed_first))
    [user] = [m for m in api if m["role"] == "user"]
    assert mp.MESSAGE_PARTS not in user
    assert HUMAN in user["content"] and PEER in user["content"]
    assert (user["content"].index(PEER) < user["content"].index(HUMAN)) is typed_first


def _replayed(tmp_path, messages):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", source="discord")
        db.append_messages_batch("s", [dict(m) for m in messages])
        return db.get_messages_as_conversation("s")
    finally:
        db.close()


def _wire_bytes(api):
    return json.dumps([{k: v for k, v in m.items() if not str(k).startswith("_")} for m in api], sort_keys=True)


@pytest.mark.parametrize("route", ["default", "native"])
@pytest.mark.parametrize("typed_first", [True, False], ids=["typed-first", "legacy-first"])
def test_merged_request_is_identical_live_and_replayed(agent, tmp_path, monkeypatch, route, typed_first):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if route == "native":
        agent.provider = PROVIDER
        wr.register_renderer(_NativeRenderer(), provider=PROVIDER)
    try:
        live_messages = _conversation(typed_first)
        replay = _replayed(tmp_path, live_messages)
        assert [m["role"] for m in replay] == ["user", "assistant", "user"]
        assert [mp.has_parts(m) for m in replay] == ([True, False, False] if typed_first else [False, False, True])
        live = _assemble(agent, [dict(m) for m in live_messages])
        replayed = _assemble(agent, replay)
        assert _wire_bytes(replayed) == _wire_bytes(live)
        if route == "native":
            assert _adapter_view(replayed) == _adapter_view(live)
            assert ("human", HUMAN) in _adapter_view(live)[0]
    finally:
        wr.unregister_renderer(provider=PROVIDER)
