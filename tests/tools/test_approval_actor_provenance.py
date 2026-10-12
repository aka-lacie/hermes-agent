"""Host attribution is immutable and parallel actors never share a consent prompt."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading

import pytest

from agent.approval_provenance import attach_child_actor, capture_origin, format_provenance
from agent.tool_invocation import CallContext, bind_call_context
from tools import approval, approval_gateway_wait as waits


def actor(sid, *, parent=None, title="", retained=False):
    value = SimpleNamespace(session_id=sid, _current_turn_id="activation-1", _approval_title=title,
                            _delegate_depth=0 if parent is None else parent._delegate_depth + 1)
    if retained:
        value._delegate_session_id = sid
    if parent:
        attach_child_actor(value, parent)
    return value


@contextmanager
def origin(agent, *, workflow=None):
    context = CallContext(agent, "task", "call", approval_workflow=workflow)
    context.approval_origin = capture_origin(context, "terminal")
    with bind_call_context(context):
        yield context.approval_origin


def test_retained_identity_lineage_and_redacted_snapshot_survive_resume():
    main = actor("main-000000")
    child = actor("20261011_173437_1c6dff", parent=main, title="Publish plan", retained=True)
    grandchild = actor("nested-111111", parent=child, title="Nested")
    with origin(grandchild, workflow=("bundle:skill:entry", "a" * 64)) as captured:
        public = captured.provenance.to_dict()
        assert public["actor"]["kind"] == "subagent"
        assert public["actor"]["parent"]["reference"] == "1c6dff"
        assert public["tool"] == "terminal" and public["workflow"] == "bundle:skill:entry"
        with pytest.raises(FrozenInstanceError):
            captured.provenance.tool = "forged"
    with origin(child) as first:
        child.session_id = "compacted-222222"
        child._current_turn_id = "activation-2"
        with origin(child) as resumed:
            assert resumed.provenance.actor == first.provenance.actor
            assert resumed.identity != first.identity
    child._approval_title = "changed"
    assert first.provenance.actor.title == "Publish plan"
    assert "20261011_173437" not in format_provenance(first.provenance)


def test_titles_and_missing_identity_never_become_authority_or_main_attribution():
    main = actor("main-000000")
    child = actor("child-111111", parent=main,
                  title="**@everyone** <@123> 文学\u202e https://secret.example/v1 sk-proj-abc123xyz4567890abcdef")
    with origin(child) as captured:
        text = format_provenance(captured.provenance)
    assert "@everyone" not in text and "<@123>" not in text
    assert "secret.example" not in text and "abc123xyz4567890abcdef" not in text
    assert "文学" in text and "\u202e" not in text
    assert format_provenance(None) == "Origin unavailable"
    assert capture_origin(CallContext(SimpleNamespace(), "task", "call"), "terminal") is None
    with origin(main) as captured:
        assert captured.provenance.actor.kind == "main"


@pytest.mark.parametrize("known", [True, False])
def test_identical_commands_from_different_or_unknown_origins_do_not_coalesce(monkeypatch, known):
    approval._gateway_queues.clear()
    monkeypatch.setattr(waits._ctx, "_get_approval_timeout", lambda: 10)
    monkeypatch.setattr(waits._ctx, "_fire_approval_hook", lambda *a, **k: None)
    posted, events, results = [], [threading.Event(), threading.Event()], []
    main = actor("main-000000")
    children = [actor("child-" + str(i) * 6, parent=main, title="Same title") for i in (1, 2)]

    def run(index):
        from contextlib import nullcontext
        with origin(children[index]) if known else nullcontext():
            def notify(data):
                posted.append(data)
                events[index].set()
            results.append(waits._await_gateway_decision("lane", notify,
                {"command": "fixture", "pattern_keys": ["fixture"], "provenance": {"actor": "forged"}}))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    try:
        for i, thread in enumerate(threads):
            thread.start()
            assert events[i].wait(5)
        assert len(posted) == 2
        assert approval.resolve_gateway_approval("lane", "once", request_id=posted[1]["request_id"]) == 1
        assert approval.resolve_gateway_approval("lane", "deny", request_id=posted[0]["request_id"]) == 1
    finally:
        approval.unregister_gateway_notify("lane")
        for thread in threads:
            if thread.ident:
                thread.join(5)
    assert {r["choice"] for r in results} == {"once", "deny"}
    assert not any(r.get("coalesced") for r in results)
    if known:
        assert posted[0]["provenance"]["actor"]["reference"] != posted[1]["provenance"]["actor"]["reference"]
    else:
        assert all(p["provenance"] is None for p in posted)


def test_transport_digest_and_snapshot_remain_bound_after_title_changes():
    from hermes_cli.approval_transport import ApprovalRequest
    child = actor("child-111111", parent=actor("main-000000"), title="Before")
    with origin(child):
        request = ApprovalRequest.create(command="fixture", description="fixture", pattern_key="p", pattern_keys=("p",),
            session_key="lane", surface="gateway", allow_session=True, allow_permanent=True)
    child._approval_title = "After"
    assert request.provenance.actor.title == "Before"
    assert request.respond("once").request_digest == request.digest


def test_exact_card_binding_never_falls_back_after_timeout_or_duplicate_click():
    from gateway.platforms.approval_cards import ApprovalCardBinding
    first, second = waits._ApprovalEntry({}), waits._ApprovalEntry({})
    approval._gateway_queues["race-lane"] = [first, second]
    old = ApprovalCardBinding("race-lane", first.data["request_id"], ["once"])
    fresh = ApprovalCardBinding("race-lane", second.data["request_id"], ["once"])
    try:
        # Timeout wins the same queue lock used by answer commitment.
        with approval._lock:
            approval._gateway_queues["race-lane"].remove(first)
        assert old.resolve(old.ids["once"], "once") == 0
        assert second.result is None
        assert fresh.resolve(fresh.ids["once"], "once") == 1
        assert fresh.resolve(fresh.ids["once"], "once") == 0
        assert second.result == "once"
    finally:
        approval._gateway_queues.pop("race-lane", None)


def test_long_card_keeps_command_reason_deadline_and_origin_within_platform_budget():
    from gateway.platforms.approval_cards import approval_metadata, build_approval_prompt
    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = object.__new__(DiscordAdapter)
    adapter._approval_mention_content = lambda: ""
    with origin(actor("child-111111", parent=actor("main-000000"), title="文" * 200)) as captured:
        metadata = approval_metadata(None, {"request_id": "host-request", "provenance": captured.provenance.to_dict()})
    prompt = build_approval_prompt(adapter, "chat", "fixture " * 1000, "lane", "Synthetic reason", metadata, True, True, False)
    assert adapter.message_len_fn(prompt.text) <= adapter.MAX_MESSAGE_LENGTH
    assert "fixture" in prompt.text and "Synthetic reason" in prompt.text and "Subagent" in prompt.text
    assert "Tool: terminal" in prompt.text and "NOT run" in prompt.text


def test_answer_during_card_presentation_still_settles_with_exact_outcome(monkeypatch):
    monkeypatch.setattr(waits._ctx, "_fire_approval_hook", lambda *a, **k: None)
    settled = []
    def notify(data):
        assert approval.resolve_gateway_approval("early-click", "once", request_id=data["request_id"]) == 1
        assert approval.register_gateway_settle("early-click", data["request_id"],
            lambda reason, choice: settled.append((reason, choice)), include_outcome=True)
        # The presenting index is never eligible for a second answer.
        assert approval.resolve_gateway_approval("early-click", "deny", request_id=data["request_id"]) == 0
    result = waits._await_gateway_decision("early-click", notify, {"command": "fixture"})
    assert result["choice"] == "once" and settled == [("resolved", "once")]
    assert not any(key[0] == "early-click" for key in waits._presenting_entries)
