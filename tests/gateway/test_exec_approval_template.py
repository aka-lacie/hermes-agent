"""Invariants for the exec-approval template method on BasePlatformAdapter.

The choice set (once / session / always / deny, minus the persistent tiers under a smart deny)
used to be re-derived in every button adapter and needed three separate "same fix × N adapters"
sweeps. It now comes from one place; adapters only render it.
"""

from typing import Any, Dict

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, ExecApprovalPrompt, SendResult
from gateway.run_turn_runner import _renders_exec_approval_buttons


class _Plain(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(self, *a: Any, **k: Any) -> SendResult:
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {}


class _Buttons(_Plain):
    def __init__(self):
        super().__init__()
        self.prompts: list = []

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        self.prompts.append(prompt)
        return SendResult(success=True, message_id="m1")


@pytest.mark.parametrize(
    "allow_permanent, allow_session, smart_denied, expected",
    [
        (True, True, False, ["once", "session", "always", "deny"]),
        (False, True, False, ["once", "session", "deny"]),
        (True, False, False, ["once", "deny"]),          # no session tier → no permanent tier either
        (True, True, True, ["once", "deny"]),            # smart deny: owner override is one-shot
        (False, False, True, ["once", "deny"]),
    ],
)
@pytest.mark.asyncio
async def test_choice_set_follows_the_shared_rule(allow_permanent, allow_session, smart_denied, expected):
    adapter = _Buttons()
    result = await adapter.send_exec_approval(
        "chat", "rm -rf /tmp/x", "sess", description="cleanup",
        allow_permanent=allow_permanent, allow_session=allow_session, smart_denied=smart_denied)
    assert result.success
    (prompt,) = adapter.prompts
    assert prompt.choices == expected
    assert "rm -rf /tmp/x" in prompt.text and "cleanup" in prompt.text


@pytest.mark.asyncio
async def test_command_is_truncated_to_the_platform_budget():
    class _Small(_Buttons):
        # The frozen origin line is part of the platform budget; leave exactly 20 command chars.
        _EA_CMD_BUDGET = 20 + len("\n\nOrigin unavailable")

    adapter = _Small()
    await adapter.send_exec_approval("chat", "x" * 100, "sess")
    (prompt,) = adapter.prompts
    assert "x" * 20 + "..." in prompt.text and "x" * 21 not in prompt.text
    assert prompt.text.endswith("Origin unavailable")
    assert prompt.command == "x" * 100  # the raw command stays available for embeds/cards


def test_runner_only_offers_buttons_to_adapters_that_render_them():
    """Plain adapters get the text ``/approve`` prompt; the base send_exec_approval must not make
    the runner believe every adapter has buttons (its default reports failure and the runner would
    log a spurious fallback on every approval)."""
    assert _renders_exec_approval_buttons(_Buttons) is True
    assert _renders_exec_approval_buttons(_Plain) is False

    class _DuckTyped:
        async def send_exec_approval(self, *a, **k):
            return SendResult(success=True)

    assert _renders_exec_approval_buttons(_DuckTyped) is True
    assert _renders_exec_approval_buttons(object) is False


@pytest.mark.asyncio
async def test_explicit_request_id_reads_the_frozen_origin_from_the_registry(monkeypatch):
    """The runner passes only thread routing as metadata plus the explicit request id (upstream
    cfed2ca71f); the card must still show that exact request's frozen origin (c2d2d3f768)."""
    from types import SimpleNamespace

    from agent.approval_provenance import format_provenance
    from tests.tools.test_approval_actor_provenance import actor, origin
    from tools import approval_gateway_wait as waits

    child = actor("child-111111", parent=actor("main-000000"), title="Fix the build")
    with origin(child) as captured:
        frozen = captured.provenance.to_dict()
    monkeypatch.setitem(waits._presenting_entries, ("sess", "req-1"),
                        SimpleNamespace(data={"request_id": "req-1", "provenance": frozen}))
    adapter = _Buttons()
    await adapter.send_exec_approval("chat", "ls", "sess", metadata={"thread_id": "t1"}, request_id="req-1")
    (prompt,) = adapter.prompts
    assert prompt.request_id == "req-1" and prompt.provenance == frozen
    assert format_provenance(frozen) != "Origin unavailable"
    assert prompt.text.endswith(adapter._ea_escape(format_provenance(frozen)))
    assert prompt.metadata == {"thread_id": "t1"}  # nothing private reaches the platform


@pytest.mark.asyncio
async def test_legacy_metadata_key_never_reaches_the_platform():
    from gateway.platforms.approval_cards import approval_metadata

    adapter = _Buttons()
    metadata = approval_metadata({"thread_id": "t1"}, {"request_id": "req-2", "provenance": None})
    await adapter.send_exec_approval("chat", "ls", "sess", metadata=metadata)
    (prompt,) = adapter.prompts
    assert prompt.request_id == "req-2"
    assert prompt.metadata == {"thread_id": "t1"}
