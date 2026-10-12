"""Tell the chat when an exec-approval prompt times out (messaging platforms).

``tools.approval_gateway_wait._await_gateway_decision`` calls ``entry.settle(reason)`` once the
wait ends. Only the TUI registered such a hook, so on Telegram / Slack / WhatsApp a card whose
timer ran out kept live buttons and the user never learned the command did NOT run. The turn
runner registers the hook here right after the prompt was delivered.

Best-effort by design: a failed notice is logged at debug — the approval already resolved as
"no", and nothing here may block the agent thread.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, Callable, Optional

from agent.i18n import t
from gateway.platforms.base_exec_approval import approval_timeout_seconds, format_approval_timed_out_notice

logger = logging.getLogger(__name__)


def register_timeout_notice(
    runner, approval_data: dict, *, command: str, card_message_id: Optional[str]) -> None:
    """Arm a settle hook that posts the timed-out notice for ``approval_data['request_id']``.

    ``runner`` is the ``TurnRunner`` (for ``_ctx`` and ``_schedule``); ``card_message_id`` is the
    delivered BUTTON card's id when the adapter returned one, so the card itself is edited in place
    (which also drops its buttons). The plain-text prompt passes ``None``: it has no buttons to
    drop and rewriting it would erase the record of what was asked. ``command`` is the
    already-redacted command shown to the user. The notice is skipped when the run is no longer
    current (``ctx._run_still_current``).
    """
    from tools.approval import register_gateway_settle

    request_id = approval_data.get("request_id")
    session_key = runner._ctx.session_key or ""
    if not request_id or not session_key:
        return
    timeout_s = approval_timeout_seconds()

    provenance = deepcopy(approval_data.get("provenance"))

    def settle(reason: str, choice=None) -> None:
        still_current = getattr(runner._ctx, "_run_still_current", None)
        current = not callable(still_current) or still_current()
        # An old card can be settled in place; never post a late notice into a new turn.
        if not current and not card_message_id:
            return
        runner._schedule(
            _post_settled_notice(runner._ctx, command, card_message_id, timeout_s,
                                 provenance=provenance, reason=reason, choice=choice, allow_send=current),
            "Approval settlement notice scheduling error")

    register_gateway_settle(session_key, request_id, settle, include_outcome=True)


async def _post_timeout_notice(ctx, command: str, card_message_id: Optional[str], timeout_s: int,
                               *, provenance=None) -> None:
    await _post_settled_notice(ctx, command, card_message_id, timeout_s,
                               provenance=provenance, reason="timeout")


async def _post_settled_notice(ctx, command, card_message_id, timeout_s, *, provenance=None,
                               reason, choice=None, allow_send=True):
    from agent.approval_provenance import format_provenance
    from gateway.run import _interim_metadata

    adapter = ctx._status_adapter
    if reason == "interrupted":
        notice = t("approval.summary.cancelled", noun=t("approval.noun.command"))
    elif choice in {"once", "session", "always"}:
        notice = t("approval.allowed_" + choice).strip()
    elif choice == "deny":
        notice = t("approval.denied").strip()
    elif reason == "timeout":
        notice = format_approval_timed_out_notice(timeout_s)
    else:
        notice = t("approval.summary.cancelled", noun=t("approval.noun.command"))
    origin = format_provenance(provenance)
    body = t("gateway.exec_approval.timed_out_card_body", notice=notice, command=command) + "\n\n" + origin
    try:
        if card_message_id and await _edit_card(adapter, ctx._status_chat_id, card_message_id, body):
            return
        if allow_send and reason == "timeout" and choice is None:
            await adapter.send(ctx._status_chat_id, notice + "\n\n" + origin,
                               metadata=_interim_metadata(ctx._status_thread_metadata))
    except Exception:
        logger.debug("Approval settlement notice failed", exc_info=True)


async def _edit_card(adapter, chat_id: str, message_id: str, content: str) -> bool:
    """Edit the card in place (drops the buttons on platforms whose edit replaces the markup)."""
    edit: Optional[Callable[..., Any]] = getattr(adapter, "edit_message", None)
    if edit is None:
        return False
    try:
        result = await edit(chat_id, message_id, content)
    except Exception:
        logger.debug("Approval card edit failed", exc_info=True)
        return False
    return bool(getattr(result, "success", False))
