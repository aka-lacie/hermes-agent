"""Logical selectors for opt-in delivery targets and delegate lookups (Freya's ruling, Phase 3 reconcile).

"do the opt ins point only to a session or can it point to a class of sessions (e.g. current bot chat, or
current discord session)": an opt-in ``delivery_target`` names a conversation through a selector
(``discord:dm:<chat_id>``, ``discord``, ``bot-chat``, ``webhook:<route>:<key>``); delivery, list, observe,
resume and steer resolve it to the session the conversation holds NOW. Real temporary SessionDBs; the only
stand-in is a capture sink in front of the dispatcher's sinks (what the gateway owner would admit).
"""

import json
from types import SimpleNamespace

import pytest

from gateway import delivery_targets as dt
from gateway.session_selectors import (
    SelectorError, current_session_for_key, resolve_session_selector, selector_aliases, validate_selector,
)
from hermes_state import SessionDB
from tools.delegate_session_store import DelegateSessionError, DelegateSessionStore

DM = "1443211608653430826"
SELECTOR = f"discord:dm:{DM}"
KEY = f"agent:main:discord:dm:{DM}"  # a store outside the profile tree keys in the default namespace


@pytest.fixture
def state(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    yield db, DelegateSessionStore(db)
    db.close()


@pytest.fixture
def captured():
    """A sink in front of every other: records each admission the dispatcher hands to a session owner."""
    seen = []

    def deliver(db, row, target, event, *, wake_policy=None, request_id=None):
        seen.append({"target_id": row["id"], "event": event, "request_id": request_id})
        return dt.DeliveryReceipt(dt.KIND_SESSION, True, "test_capture", row["id"], f"adm-{len(seen)}")
    dt.register_sink("test_capture", lambda db, row, target: True, deliver)
    yield seen
    dt.unregister_sink("test_capture")


def _chat(db, sid, *, key=KEY, chat_id=DM, title=None, routed=True):
    db.create_session(sid, "discord")
    db._write_sql("UPDATE sessions SET session_key = ?, chat_id = ?, chat_type = 'dm', title = ? WHERE id = ?",
                  (key, chat_id, title, sid))
    if routed:  # the gateway's routing entry: the session the chat's next message runs in
        db.save_gateway_routing_entry(key, json.dumps({"session_id": sid, "session_key": key}))


def _new(db, old, new):
    """``/new`` in the same Discord DM: the old session ends at a reset boundary, the key moves on."""
    db.end_session(old, "session_reset")
    _chat(db, new)


def _webhook(db, sid, target, kind="webhook"):
    db.create_session(sid, "webhook")
    db._write_sql("UPDATE sessions SET lineage_kind = ?, delivery_target = ?, status = 'idle' WHERE id = ?",
                  (kind, dt.encode_delivery_target(target), sid))
    return dt.bind_target(db, sid)  # creation binds the opt-in to the session its target holds now


def _listed(store, caller):
    return {row["session_id"]: row for row in store.list(caller)}


def _completion(db, sid, text="PR #90: review posted"):
    from gateway.platforms.webhook_session_targets import webhook_result_event
    row = db._read_one("SELECT * FROM sessions WHERE id = ?", (sid,))
    event = webhook_result_event(sid, text, route="github-pr")
    return dt.dispatch(db, row, event, request_id=event.source_event_id)


def _attached(db, sid):
    return db._read_one("SELECT attached_session_id, parent_session_id FROM sessions WHERE id = ?", (sid,))


# ── grammar and resolution ───────────────────────────────────────────────────

def test_selector_grammar_and_shapes():
    assert validate_selector(" discord:dm:1 ") == "discord:dm:1"
    assert validate_selector("Bot-Chat") == "bot-chat"
    for bad in ("", "bot chat", "webhook:route-only", "discord:", 7, None):
        with pytest.raises(SelectorError):
            validate_selector(bad)
    assert dt.parse_delivery_target({"kind": "session", "selector": SELECTOR, "attach": True}) == {
        "kind": "session", "selector": SELECTOR, "attach": True}
    assert dt.session_target(selector="bot-chat", attach=True) == {"kind": "session", "selector": "bot-chat",
                                                                    "attach": True}
    for bad in ({"kind": "session", "attach": True}, {"kind": "session", "session_id": "s", "selector": "discord"},
                {"kind": "session", "selector": "two words"}):
        with pytest.raises(dt.DeliveryTargetError):
            dt.parse_delivery_target(bad)


def test_key_selectors_follow_the_routing_entry_then_the_newest_keyed_row(state):
    db, _ = state
    _chat(db, "s1", routed=False)
    assert resolve_session_selector(db, SELECTOR) == "s1" == resolve_session_selector(db, KEY)
    _chat(db, "s2", routed=False)
    db.end_session("s1", "session_reset")
    assert current_session_for_key(db, KEY) == "s2"  # newest open row
    db.save_gateway_routing_entry(KEY, json.dumps({"session_id": "s1"}))
    assert resolve_session_selector(db, SELECTOR) == "s1"  # the gateway's routing entry wins
    assert resolve_session_selector(db, "session:s2") == "s2"
    assert resolve_session_selector(db, "discord:dm:404") is None
    assert resolve_session_selector(db, "session:nope") is None
    assert resolve_session_selector(db, "no-such-alias") is None


def test_aliases_bot_chat_and_the_discord_home_channel(state):
    db, _ = state
    db.create_session("bot", "local")
    db._write_sql("UPDATE sessions SET title = 'Bot Chat' WHERE id = 'bot'")
    assert resolve_session_selector(db, "bot-chat") == "bot" == resolve_session_selector(db, "title:Bot Chat")
    _chat(db, "home-chat", key="agent:main:discord:group:777", chat_id="777")
    homes = {"home_channels": {"discord": {"chat_id": "777"}}}
    assert resolve_session_selector(db, "discord", config=homes) == "home-chat"
    assert resolve_session_selector(db, "discord", config={"home_channels": {}}) is None  # no home: nothing
    # The alias table is configurable (sessions.selector_aliases) and never chains.
    _chat(db, "dm", key=KEY)
    config = {"sessions": {"selector_aliases": {"discord": SELECTOR, "loop": "other", "other": "bot-chat"}}}
    assert selector_aliases(config)["discord"] == SELECTOR
    assert resolve_session_selector(db, "discord", config=config) == "dm"
    assert resolve_session_selector(db, "loop", config=config) is None


# ── Freya's scenario: the opt-in follows the chat through /new ───────────────

def test_attached_webhook_follows_the_discord_dm_through_new(state, captured):
    db, store = state
    _chat(db, "s1")
    assert _webhook(db, "pr-90", {"kind": "session", "selector": SELECTOR, "attach": True}) == "s1"
    assert _attached(db, "pr-90")["attached_session_id"] == "s1"
    assert _attached(db, "pr-90")["parent_session_id"] is None  # the attach primitive, never a parent link
    assert _listed(store, "s1")["pr-90"]["badge"] == "webhook"

    _new(db, "s1", "s2")
    listed = _listed(store, "s2")
    assert set(listed) == {"pr-90"} and listed["pr-90"]["badge"] == "webhook"
    assert "pr-90" not in _listed(store, "s1")  # the chat moved on: its old session no longer holds it
    assert store.get("s2", "pr-90")["session_id"] == "pr-90"
    assert store.observe("s2", "pr-90")["session"]["session_id"] == "pr-90"
    with pytest.raises(DelegateSessionError):
        store.get("s1", "pr-90")

    receipt = _completion(db, "pr-90")
    assert receipt.delivered and receipt.target_session_id == "s2"
    assert [c["target_id"] for c in captured] == ["s2"]  # admitted into S2, not S1, exactly once
    event = captured[0]["event"]
    assert (event.kind, event.sender_kind, event.sender_id) == ("webhook", "webhook", "pr-90")
    assert _attached(db, "pr-90")["attached_session_id"] == "s2"  # rebound on delivery

    from tools.delegate_sessions import control
    parent = SimpleNamespace(session_id="s2", _session_db=db)
    resumed = json.loads(control("resume", parent, session_id="pr-90", message="Answer the reviewer"))
    steered = json.loads(control("steer", parent, session_id="pr-90", message="Only the tests"))
    observed = json.loads(control("observe", parent, session_id="webhook-free-ref-unknown"))
    assert resumed["status"] == "queued" and steered["status"] == "queued" and steered["action"] == "steer"
    assert "error" in observed
    assert [(c["target_id"], c["event"].sender_kind, c["event"].text) for c in captured[1:]] == [
        ("pr-90", "parent", "Answer the reviewer"), ("pr-90", "parent", "Only the tests")]


def test_literal_session_targets_still_name_exactly_that_session(state, captured):
    db, store = state
    _chat(db, "s1")
    _webhook(db, "inbox", {"kind": "session", "session_id": "s1", "attach": True})
    _new(db, "s1", "s2")
    assert "inbox" in _listed(store, "s1") and "inbox" not in _listed(store, "s2")
    receipt = _completion(db, "inbox")
    assert receipt.delivered and [c["target_id"] for c in captured] == ["s1"]


def test_compression_continuation_of_the_target_receives_the_delivery(state, captured):
    db, store = state
    _chat(db, "s2")
    _webhook(db, "pr-90", {"kind": "session", "selector": SELECTOR, "attach": True})
    # The DM compresses: s2 ends at 'compression' and its continuation carries the same routing key.
    db.end_session("s2", "compression")
    db.create_session("s2-tip", "discord", parent_session_id="s2")
    db._write_sql("UPDATE sessions SET session_key = ?, chat_id = ? WHERE id = 's2-tip'", (KEY, DM))
    assert resolve_session_selector(db, SELECTOR) == "s2-tip"  # routing entry still names s2: its tip
    assert "pr-90" in _listed(store, "s2-tip")
    assert _completion(db, "pr-90").target_session_id == "s2-tip"
    assert [c["target_id"] for c in captured] == ["s2-tip"]
    # The target's compression never absorbs the attached session: it is no segment of the DM's chain.
    assert db.get_compression_chain("s2") == ["s2", "s2-tip"]
    assert db.get_compression_lineage("pr-90") == ["pr-90"]
    assert not db._is_compression_child_row(dict(db._read_one("SELECT * FROM sessions WHERE id='pr-90'")))
    # Even a stray parent link next to the binding is not read as a continuation of the bound session.
    row = dict(db._read_one("SELECT * FROM sessions WHERE id='pr-90'"), parent_session_id="s2-tip")
    assert row["attached_session_id"] == "s2-tip" and db._is_explicit_fork_child_row(row)


def test_a_selector_holding_no_session_delivers_nothing_and_lists_nowhere(state, captured):
    db, store = state
    _chat(db, "s1")
    assert _webhook(db, "orphan", {"kind": "session", "selector": "discord:dm:999", "attach": True}) is None
    receipt = _completion(db, "orphan")
    assert not receipt.delivered and receipt.reason == "selector_unresolved" and captured == []
    assert "orphan" not in _listed(store, "s1")
    # A bound opt-in whose selector stops resolving stays with the conversation it was last bound to.
    _webhook(db, "bound", {"kind": "session", "selector": "bot-chat", "attach": True})
    db._write_sql("UPDATE sessions SET attached_session_id = 's1' WHERE id = 'bound'")
    assert "bound" in _listed(store, "s1")


def test_acl_still_denies_unrelated_callers(state, captured):
    db, store = state
    _chat(db, "s1")
    _chat(db, "stranger", key="agent:main:discord:dm:555", chat_id="555")
    _webhook(db, "pr-90", {"kind": "session", "selector": SELECTOR, "attach": True})
    assert "pr-90" not in _listed(store, "stranger")
    for call in (lambda: store.get("stranger", "pr-90"), lambda: store.observe("stranger", "pr-90"),
                 lambda: store.get("stranger", "webhook:github-pr:nope")):
        with pytest.raises(DelegateSessionError):
            call()
    from agent.sessions_api import SessionsAPI
    with pytest.raises(LookupError):
        SessionsAPI(db).admit("stranger", "pr-90", "hello")
    assert captured == []
    assert SessionsAPI(db).resolve(SELECTOR) == "s1"


def test_selectors_name_sessions_in_delegate_lookups_and_bot_chat_opt_ins(state, captured):
    db, store = state
    db.create_session("bot", "local")
    db._write_sql("UPDATE sessions SET title = 'Bot Chat' WHERE id = 'bot'")
    _webhook(db, "inbox", {"kind": "session", "selector": "bot-chat", "attach": True})
    assert "inbox" in _listed(store, "bot")
    from agent.sessions_api import SessionAdmissionError, SessionsAPI
    # The main chat addresses the webhook session attached to it as its parent-side sender (a peer message
    # is siblings-only: the opt-in is not a sibling of the chat).
    receipt = SessionsAPI(db).admit("bot", "inbox", "Check the thread", sender_kind="parent", kind="parent_message")
    assert receipt["delivered"] and captured[-1]["target_id"] == "inbox"
    with pytest.raises(SessionAdmissionError):
        SessionsAPI(db).admit("bot", "inbox", "as a peer")


def test_cron_opt_in_by_selector_is_recorded_bound_and_delivered_to_the_current_session(state, captured):
    from cron.scheduler_routes import stamp_cron_session_route
    from cron.session_delivery import deliver_to_session, job_delivery_target
    db, store = state
    _chat(db, "s1")
    job = {"id": "digest", "delivery_target": {"kind": "session", "selector": SELECTOR, "attach": True}}
    db.create_session("cron-fire", "cron")
    stamp_cron_session_route(db, "cron-fire", job)
    assert _attached(db, "cron-fire")["attached_session_id"] == "s1"
    assert _listed(store, "s1")["cron-fire"]["badge"] == "cron"
    _new(db, "s1", "s2")
    assert deliver_to_session(job, "Morning digest", job_delivery_target(job), db=db) is None
    assert [c["target_id"] for c in captured] == ["s2"] and captured[0]["event"].sender_kind == "cron"
    assert "cron-fire" in _listed(store, "s2")
    missing = {"id": "lost", "delivery_target": {"kind": "session", "selector": "discord:dm:0", "attach": True}}
    assert "selector_unresolved" in deliver_to_session(missing, "x", job_delivery_target(missing), db=db)
