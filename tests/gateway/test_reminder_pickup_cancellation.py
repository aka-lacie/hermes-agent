"""A retired reminder pickup cannot consume and discard its successor's wake marker.

The real destructive collector is paused on a thread while hot unbind/rebind cancels its
awaiting task. Cancellation must join that collection and rearm its unscheduled markers.
"""
import asyncio
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import session_automation_handoff as handoff
from gateway import session_cron
from tests.waiting import async_wait_until


def _authority(home, name):
    return SimpleNamespace(db=SimpleNamespace(db_path=home / "state.db"), name=name,
                           profile_id=name, runner=SimpleNamespace(), native_waiters=set(), waiters={})


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_again", [False, True])
async def test_cancelled_collector_rearms_successor_wake(tmp_path, monkeypatch, cancel_again):
    started, release_old, release_new = (threading.Event() for _ in range(3))
    successor_started = threading.Event()
    picked = asyncio.Event()
    collected, pickups = [], []
    real_collect = handoff._collect
    lock = threading.Lock()
    first = True

    def collect(home):
        nonlocal first
        with lock:
            old, first = first, False
        if old:
            started.set()
            assert release_old.wait(10), "old collector was not released"
            result = real_collect(home)
            collected.extend(result)
            return result
        successor_started.set()
        assert release_new.wait(10), "successor collector was not released"
        return real_collect(home)

    def pick_up(authority, session_ids):
        pickups.extend((authority.name, sid) for sid in session_ids)
        picked.set()

    monkeypatch.setattr(handoff, "_collect", collect)
    monkeypatch.setattr(handoff, "pick_up", pick_up)
    old, new = _authority(tmp_path, "old"), _authority(tmp_path, "new")
    session_cron.bind_owner(old)
    old_task = session_cron._handoff_consumers[tmp_path][1]
    new_task = None
    try:
        assert await asyncio.to_thread(started.wait, 10)
        session_cron.unbind_owner(old)
        session_cron.bind_owner(new)
        new_task = session_cron._handoff_consumers[tmp_path][1]
        assert await asyncio.to_thread(successor_started.wait, 10)
        await async_wait_until(lambda: old_task.cancelling(), what="predecessor cancellation")
        if cancel_again:
            old_task.cancel()
        # Producer commits after successor recovery, then signals this fresh work.
        handoff.signal(tmp_path, "fresh-chat")
        release_old.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(old_task), 10)
        assert collected == ["fresh-chat"]
        assert pickups == []  # the retired owner never schedules the result
        release_new.set()
        await asyncio.wait_for(picked.wait(), 10)
        assert pickups == [("new", "fresh-chat")]
        assert real_collect(tmp_path) == []
    finally:
        release_old.set()
        release_new.set()
        session_cron.unbind_owner(old)
        session_cron.unbind_owner(new)
        await asyncio.wait_for(asyncio.gather(
            *[task for task in (old_task, new_task) if task is not None], return_exceptions=True), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_retirement", [False, True])
async def test_profile_retirement_joins_collector_before_releasing_ownership(
    tmp_path, monkeypatch, interrupt_retirement,
):
    from gateway import run_runtime

    started, release = threading.Event(), threading.Event()
    real_collect = handoff._collect

    def collect(home):
        started.set()
        assert release.wait(10), "collector was not released"
        return real_collect(home)

    monkeypatch.setattr(handoff, "_collect", collect)
    monkeypatch.setattr("gateway.session_runtime_workers.stop_authority_work", lambda authority: None)
    monkeypatch.setattr("gateway.session_runtime_workers.join_authority_work", AsyncMock())
    monkeypatch.setattr("gateway.session_llm_oneshot.stop", lambda authority: None)
    monkeypatch.setattr(run_runtime, "_authority_tasks", lambda authority: [])
    authority = _authority(tmp_path, "retiring")
    session_cron.bind_owner(authority)
    pickup = session_cron._handoff_consumers[tmp_path][1]
    retirement = None
    try:
        assert await asyncio.to_thread(started.wait, 10)
        retirement = asyncio.create_task(run_runtime._retire_profile_authority(authority))
        await async_wait_until(lambda: pickup.cancelling(), what="retirement cancellation")
        assert not retirement.done(), "ownership released with a destructive collector still in flight"
        assert not getattr(authority, "retired", False)
        if interrupt_retirement:
            retirement.cancel()
            with pytest.raises(asyncio.CancelledError):
                await retirement
            retirement = asyncio.create_task(run_runtime._retire_profile_authority(authority))
            await async_wait_until(lambda: pickup.cancelling() >= 2, what="repeated retirement cancellation")
            assert not retirement.done()
            assert not getattr(authority, "retired", False)
        handoff.signal(tmp_path, "fresh-chat")
        release.set()
        assert await asyncio.wait_for(retirement, 10) is True
        assert pickup.done() and authority.retired
        assert real_collect(tmp_path) == ["fresh-chat"]
    finally:
        release.set()
        session_cron.unbind_owner(authority)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(pickup), 10)
        if retirement is not None:
            await asyncio.wait_for(asyncio.gather(retirement, return_exceptions=True), 10)


def test_rearm_and_producer_can_signal_the_same_chat_concurrently(tmp_path, monkeypatch):
    published = threading.Barrier(2)
    replace = handoff.os.replace

    def simultaneous_replace(source, target):
        published.wait(10)  # both temporary files exist before either is published
        return replace(source, target)

    monkeypatch.setattr(handoff.os, "replace", simultaneous_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(handoff.signal, tmp_path, "fresh-chat") for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert handoff._collect(tmp_path) == ["fresh-chat"]
    assert list(handoff.signal_dir(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_rearm_io_failure_does_not_resurrect_cancelled_consumer(tmp_path, monkeypatch, caplog):
    started, release = threading.Event(), threading.Event()
    real_collect = handoff._collect
    collections = []

    def collect(home):
        collections.append(home)
        started.set()
        assert release.wait(10), "collector was not released"
        return real_collect(home)

    def failed_signal(home, session_id):
        raise OSError("marker storage unavailable")

    monkeypatch.setattr(handoff, "_collect", collect)
    authority = _authority(tmp_path, "retiring")
    handoff.signal(tmp_path, "fresh-chat")
    session_cron.bind_owner(authority)
    pickup = session_cron._handoff_consumers[tmp_path][1]
    try:
        assert await asyncio.to_thread(started.wait, 10)
        monkeypatch.setattr(handoff, "signal", failed_signal)
        session_cron.unbind_owner(authority)
        await async_wait_until(lambda: pickup.cancelling(), what="pickup cancellation")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(pickup), 10)
        assert pickup.cancelled() and collections == [tmp_path]
        assert "durable admissions require recovery" in caplog.text
    finally:
        release.set()
        session_cron.unbind_owner(authority)
        await asyncio.wait_for(asyncio.gather(pickup, return_exceptions=True), 10)
