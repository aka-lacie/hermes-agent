"""Real Discord callbacks resolve the clicked request, never another pending card."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry
pytest.importorskip("discord")
from plugins.platforms.discord.adapter import ExecApprovalView


@pytest.fixture(autouse=True)
def queues():
    approval._gateway_queues.clear()
    yield
    approval._gateway_queues.clear()


def card(request_id):
    view = ExecApprovalView("lane", {"123"}, request_id=request_id)
    view._message = SimpleNamespace(id=12)
    return view


async def click(view, choice="once", user="123", custom_id=None, message=12):
    import discord
    interaction = SimpleNamespace(user=SimpleNamespace(id=user, display_name="Human"),
        message=SimpleNamespace(id=message, embeds=[]),
        data={"custom_id": custom_id or view._approval_binding.ids[choice]},
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
    await view._resolve(interaction, choice, discord.Color.green(), "gateway.exec_approval.action_once")
    return interaction


@pytest.mark.asyncio
async def test_out_of_order_and_duplicate_clicks_only_settle_the_bound_request():
    first, second = _ApprovalEntry({}), _ApprovalEntry({})
    approval._gateway_queues["lane"] = [first, second]
    view = card(second.data["request_id"])
    await click(view)
    assert second.result == "once" and first.result is None
    await click(view)
    assert first.result is None
    await click(card(first.data["request_id"]), "deny")
    assert first.result == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["expired", "missing", "wrong-token", "wrong-user", "wrong-message", "restart"])
async def test_stale_or_unbound_cards_never_answer_newer_requests(bad):
    old, newer = _ApprovalEntry({}), _ApprovalEntry({})
    approval._gateway_queues["lane"] = [newer]
    view = card(None if bad == "missing" else old.data["request_id"])
    if bad not in {"expired", "missing"}:
        view = card(newer.data["request_id"])
    kwargs = {}
    if bad in {"wrong-token", "restart"}:
        kwargs["custom_id"] = card(newer.data["request_id"])._approval_binding.ids["once"]
    if bad == "wrong-user":
        kwargs["user"] = "456"
    if bad == "wrong-message":
        kwargs["message"] = 13
    await click(view, **kwargs)
    assert newer.result is None and not newer.event.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,choice", [("timeout", None), ("session_closed", None), ("interrupted", None),
                                           ("resolved", "once"), ("resolved", "deny"), ("interrupted", "once")])
async def test_every_settlement_replacement_retains_frozen_origin(reason, choice):
    from agent.approval_provenance import ApprovalActor, ApprovalProvenance
    from gateway.run_turn_runner_approval_settle import _post_settled_notice
    public = ApprovalProvenance(1, ApprovalActor("subagent", "Publish plan", "1c6dff"), "terminal").to_dict()
    adapter = SimpleNamespace(edit_message=AsyncMock(return_value=SimpleNamespace(success=True)), send=AsyncMock())
    ctx = SimpleNamespace(_status_adapter=adapter, _status_chat_id="chat", _status_thread_metadata=None)
    await _post_settled_notice(ctx, "fixture", "card", 300, provenance=public, reason=reason, choice=choice)
    content = adapter.edit_message.call_args.args[2]
    assert "Publish plan" in content and "#1c6dff" in content and "Tool: terminal" in content
    assert not adapter.send.called
    if reason == "interrupted":
        assert "cancelled" in content and "Approved" not in content
