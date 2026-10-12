"""Discord button callbacks resolve the clicked request, never another pending card."""
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import approval
from tools.approval_gateway_wait import _ApprovalEntry
from tests.gateway.conftest import _ensure_discord_mock
_ensure_discord_mock()
from plugins.platforms.discord.adapter import ExecApprovalView


@pytest.fixture(autouse=True)
def decorated_buttons(monkeypatch):
    # The repository mock deliberately leaves @button methods unmaterialized.
    # Supply only that SDK behavior; production binding, callbacks and queue
    # resolution remain real. Real discord.py already supplies these buttons.
    import discord
    base = ExecApprovalView.__bases__[0]
    localize = base._localize_buttons
    def materialize(view, **keys):
        for name in keys:
            callback = getattr(view, name)
            if not inspect.ismethod(callback):
                continue
            button = discord.ui.Button()
            async def invoke(interaction, callback=callback, button=button):
                await callback(interaction, button)
            button.callback = invoke
            setattr(view, name, button)
            view.add_item(button)
        localize(view, **keys)
    monkeypatch.setattr(base, "_localize_buttons", materialize)


_BUTTONS = {"once": "allow_once", "session": "allow_session", "always": "allow_always", "deny": "deny"}


def button(view, choice):
    return getattr(view, _BUTTONS[choice])


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
    interaction = SimpleNamespace(user=SimpleNamespace(id=user, display_name="Human"),
        message=SimpleNamespace(id=message, embeds=[]),
        data={"custom_id": custom_id or button(view, choice).custom_id},
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
    await button(view, choice).callback(interaction)
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
        kwargs["custom_id"] = button(card(newer.data["request_id"]), "once").custom_id
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
        from agent.i18n import t
        assert t("approval.summary.cancelled", noun=t("approval.noun.command")) in content
        assert t("approval.allowed_once").strip() not in content
