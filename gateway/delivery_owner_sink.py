"""The gateway owner's delivery sink: admit a delivered event into a chat this process's authority serves.

Registered by ``gateway.session_cron.bind_owner`` for the authority that owns a profile home (and removed by
``unbind_owner``). A target session that is a messaging chat or a local (Desktop/TUI/CLI-app) conversation is
not a delegate FIFO this process drains directly: the event goes through the authority's own producer
admission (``gateway.session_automation.admit_route_wake``), which mounts the route, folds, types the row
from ``typed_sender`` and honours the wake policy. Called off the owner loop (cron ticker, delegate drain).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SINK_NAME = "gateway_owner"


def _home(db) -> Path:
    return Path(db.db_path).resolve().parent


def _owner(db):
    from gateway.session_cron import owner_for_home
    try:
        return owner_for_home(_home(db))
    except (OSError, TypeError, AttributeError):  # a store without a resolvable file path has no owner here
        return None


def accept(db, row, target) -> bool:
    from gateway.delivery_targets import _is_delegate_row
    return not _is_delegate_row(row) and _owner(db) is not None


async def _admit(authority, row, event, wake_policy):
    from gateway.config import Platform
    from gateway.session_authorities import owner_scope
    from gateway.session_automation import admit_route_wake
    from gateway.session_detached import route_entry
    metadata = {"typed_sender": {"sender_kind": event.sender_kind, "sender_id": event.sender_id,
                                 "kind": event.kind, "meta": dict(event.meta)}}
    if wake_policy:
        metadata["wake_policy"] = wake_policy
    with owner_scope(authority):
        session_id = row["id"]
        live = authority.sessions.get(session_id)
        if live is not None and live.source is not None and live.source.platform == Platform.LOCAL or (
                live is None and str(row["source"] or "") == "local"):
            from gateway.session_cron import _admit_local_delivery
            return await _admit_local_delivery(authority, session_id, event.text, metadata)
        key = str(row["session_key"] or "")
        entry = route_entry(authority, key) if key else None
        if entry is None or entry.origin is None:
            return False
        source = authority.runner._restored_source(entry.origin) if hasattr(authority.runner, "_restored_source") \
            else entry.origin
        adapter = authority.runner._adapter_for_source(source)
        if adapter is None:
            return False
        return await admit_route_wake(authority.runner, adapter, source, event.text, "delivery", metadata)


def deliver(db, row, target, event, *, wake_policy=None, request_id=None):
    from gateway.delivery_targets import DeliveryReceipt, KIND_SESSION
    owner = _owner(db)
    if owner is None:
        return DeliveryReceipt(KIND_SESSION, False, None, row["id"], reason="no_owner")
    authority, loop = owner
    try:
        admitted = asyncio.run_coroutine_threadsafe(_admit(authority, row, event, wake_policy), loop).result(timeout=60)
    except Exception as exc:  # health: allow BLE001 -- cross-loop boundary: unknown chat, refusal, stalled owner
        logger.warning("delivery to session %s not confirmed: %r", row["id"], exc, exc_info=True)
        return DeliveryReceipt(KIND_SESSION, False, SINK_NAME, row["id"], reason=f"refused: {exc!r}"[:200])
    if not admitted:
        return DeliveryReceipt(KIND_SESSION, False, SINK_NAME, row["id"], reason="no_committed_route")
    return DeliveryReceipt(KIND_SESSION, True, SINK_NAME, row["id"])


def wake(db, session_id: str) -> bool:
    """Schedule the owner's drain of *session_id* (an admission written beside the authority)."""
    owner = _owner(db)
    if owner is None:
        return False
    authority, loop = owner
    if session_id not in authority.sessions:
        return False
    from gateway.session_contract import SessionRef
    loop.call_soon_threadsafe(authority._schedule, SessionRef(authority.profile_id, session_id))
    return True


def register() -> None:
    from gateway.delivery_targets import register_sink, register_waker
    register_sink(SINK_NAME, accept, deliver)
    register_waker(wake)
