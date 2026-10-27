"""Typing is cosmetic: transport cancellation must not hold turn completion open."""
import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
import plugins.platforms.discord.adapter as discord_adapter


@pytest.fixture(autouse=True)
def typing_route(monkeypatch):
    # The test environment need not install the optional Discord SDK. Only its
    # transport boundary is replaced; task ownership and cancellation are real.
    monkeypatch.setattr(discord_adapter, "discord", SimpleNamespace(
        http=SimpleNamespace(Route=lambda *args, **kwargs: SimpleNamespace(**kwargs))))


@pytest.mark.asyncio
async def test_slow_typing_cleanup_releases_turn_and_preserves_replacement():
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    replacement_entered = asyncio.Event()
    calls = 0

    async def request(route):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()  # transport cleanup can outlive cancellation
        else:
            replacement_entered.set()
            await asyncio.Event().wait()

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    adapter._client = SimpleNamespace(http=SimpleNamespace(request=request))
    await adapter.send_typing("42")
    old = adapter._typing_tasks["42"]
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(adapter.stop_typing("42"))
    tasks = [old, stop]
    try:
        await asyncio.wait_for(cancelling.wait(), 2)
        done, _ = await asyncio.wait({stop}, timeout=2)
        assert stop in done, "typing cleanup blocked turn completion"
        await stop
        await adapter.send_typing("42")
        replacement = adapter._typing_tasks["42"]
        tasks.append(replacement)
        await asyncio.wait_for(replacement_entered.wait(), 2)
        release.set()
        done, _ = await asyncio.wait({old}, timeout=2)
        assert old in done, "retired typing loop continued after transport cleanup"
        assert adapter._typing_tasks.get("42") is replacement
        await adapter.stop_typing("42")
        assert replacement.done()
        assert calls == 2
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_typing_stop_does_not_swallow_turn_cancellation():
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def request(route):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test"))
    adapter._client = SimpleNamespace(http=SimpleNamespace(request=request))
    await adapter.send_typing("42")
    old = adapter._typing_tasks["42"]
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(adapter.stop_typing("42"))
    try:
        await asyncio.wait_for(cancelling.wait(), 2)
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
    finally:
        release.set()
        old.cancel()
        with suppress(asyncio.CancelledError):
            await old
