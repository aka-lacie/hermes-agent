"""Process-local signal for waking the embedded kanban dispatcher."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_callbacks: set[Callable[[dict[str, Any]], None]] = set()
_lock = threading.Lock()


def register_kanban_dispatch_nudge(
    callback: Callable[[dict[str, Any]], None]
) -> None:
    """Register a callback invoked when new kanban work should dispatch now."""
    with _lock:
        _callbacks.add(callback)


def unregister_kanban_dispatch_nudge(
    callback: Callable[[dict[str, Any]], None]
) -> None:
    """Remove a previously registered dispatcher nudge callback."""
    with _lock:
        _callbacks.discard(callback)


def nudge_kanban_dispatch(**payload: Any) -> None:
    """Wake any in-process gateway dispatcher watchers.

    This is intentionally best-effort. Tool calls should not fail just because
    no gateway dispatcher is running in this process.
    """
    with _lock:
        callbacks = tuple(_callbacks)
    for callback in callbacks:
        try:
            callback(dict(payload))
        except Exception:
            logger.debug("kanban dispatcher nudge callback failed", exc_info=True)
