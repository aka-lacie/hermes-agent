"""A producer outside the gateway (dashboard Run-now, ``hermes cron run``, a CLI session's cron tool, a
headless tick) admits its typed event into a messaging conversation's own FIFO; the live owner picks it up.

The owner sink (``gateway/delivery_owner_sink.py``) is process-local, so such a producer's delivery to a
human chat answered ``no_owner`` and the fire was consumed undelivered. :func:`admit_for_owner` commits
instead, into ``session_admissions`` of the conversation the route's retained routing names now
(``gateway_routing``), the same ``native_text_v1`` automation row ``snapshot_automation`` builds there: it
borrows the route's last committed human envelope (its source and connector provenance) and carries the
producer's typed sender, so the turn it runs is a typed system event, never a person's input. Nothing is
trusted from this side: the owner's preclaim (``check_automation_route``) and startup recovery
(``recover_native_sessions``) re-verify the provenance against its live connector before the row runs.

Then the owner is told: :func:`signal` drops one marker per conversation under ``<home>/cron/
session_handoff/``, which the consumer the owner binds (:func:`start_consumer`) collects about once a
second, mounting the conversation if no inbound has since the restart and scheduling its drain. With no
gateway running the row stays queued; startup recovery binds and drains it. A lost marker is only a
lost wake: the row runs with the conversation's next drain.

Out of scope here, refused so the caller reports it (never a guessed row): a relay-fronted route (its
seal must be re-sealed with the adapter's transport secret), webhook routes (their reply leg binds to a
signed delivery), local app conversations (their wakes ride the live owner's ``local_automation_v1``
check), a conversation the route has moved past, and a route with no committed human turn.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SINK_NAME = "gateway_owner_handoff"
PICKUP_SECONDS = 1.0
_UNHANDED_PLATFORMS = frozenset({"local", "api_server", "webhook"})


def signal_dir(home) -> Path:
    return Path(home).resolve() / "cron" / "session_handoff"


def _store_home(db) -> Path:
    return Path(db.db_path).resolve().parent


def signal(home, session_id: str) -> None:
    """Tell the owner of *home* that *session_id*'s FIFO holds a handed-off row."""
    root = signal_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    tmp = root / f".{name}.{os.getpid()}"
    tmp.write_text(session_id, encoding="utf-8")
    os.replace(tmp, root / name)


def _committed_envelope(db, owner: str):
    """The route's last committed human envelope with connector provenance (``committed_route_envelope``)."""
    from hermes_state_runtime import list_session_admissions
    prior = list_session_admissions(db, session_id=owner, pending_only=False)
    envelope = next((r["payload"]["native_text_v1"] for r in reversed(prior)
                     if "native_text_v1" in r["payload"] and "automation" not in r["payload"]["native_text_v1"]), None)
    return envelope if envelope is not None and "provenance" in envelope else None


def handoff_payload(db, session_id: str, event, request_id: str):
    """``(owner, principal, identity, payload)`` of the automation row for *event* on *session_id*'s route,
    else ``(None, reason)``. Reads only."""
    from gateway.session_automation import automation_sender_metadata
    from gateway.session_selectors import current_session_for_key
    row = db.get_session(session_id)
    key = str((row or {}).get("session_key") or "")
    if not key or str(row.get("source") or "") in _UNHANDED_PLATFORMS:
        return None, "no_owner"  # local app / API / webhook conversations: only their live owner admits
    lineage = db.get_compression_lineage(session_id)
    owner = lineage[0] if lineage else session_id
    if current_session_for_key(db, key) != (db.get_compression_tip(owner) or owner):
        return None, "no_owner: the route moved past this conversation"
    envelope = _committed_envelope(db, owner)
    if envelope is None or envelope.get("route") != key:
        return None, "no_committed_route"
    provenance = deepcopy(envelope["provenance"])
    if (envelope.get("source") or {}).get("platform") in _UNHANDED_PLATFORMS:
        return None, "no_owner"
    if "relay" in provenance:
        return None, "no_owner: a relay-fronted route is re-sealed only by its live owner"
    source = deepcopy(envelope["source"])
    source.pop("message_id", None)  # never anchored to the stale human message
    identity = json.dumps([event.sender_kind, key, request_id], separators=(",", ":"))
    descriptor = {"identity": identity, "owner": owner}
    descriptor.update(automation_sender_metadata({"typed_sender": {
        "sender_kind": event.sender_kind, "sender_id": event.sender_id, "kind": event.kind,
        "meta": dict(event.meta)}}))
    payload = {"text": event.text or "", "native_text_v1": {
        "source": source, "route": key, "timestamp": datetime.fromtimestamp(0, UTC).isoformat(),
        "event": {"message_id": identity}, "provenance": provenance, "automation": descriptor}}
    return (owner, "automation:" + key, identity, payload), None


def admit_for_owner(db, session_id: str, event, *, wake_policy=None, request_id: str):
    """Commit *event* for the gateway owner of *session_id*'s conversation (module docstring). An exact
    retry of the same request replays its admission. Returns a ``DeliveryReceipt``."""
    from gateway.delivery_targets import KIND_SESSION, DeliveryReceipt
    from hermes_state_runtime import RuntimeStoreError, admit_session_input
    from tools.delegate_session_store import runtime_epoch
    planned, reason = handoff_payload(db, session_id, event, request_id)
    if planned is None:
        return DeliveryReceipt(KIND_SESSION, False, None, session_id, reason=reason)
    owner, principal, identity, payload = planned
    try:
        row = admit_session_input(db, epoch=runtime_epoch(db), principal_id=principal, session_id=owner,
                                  request_id=identity, payload=payload, wake_policy=wake_policy)
    except RuntimeStoreError as exc:
        return DeliveryReceipt(KIND_SESSION, False, SINK_NAME, owner, reason=f"refused: {exc.reason}")
    try:
        signal(_store_home(db), owner)
    except OSError:  # the row is committed; only the prompt wake is lost
        logger.warning("handed-off admission %s committed without its owner wake", row["admission_id"], exc_info=True)
    return DeliveryReceipt(KIND_SESSION, True, SINK_NAME, owner, row["admission_id"])


# ── owner side ───────────────────────────────────────────────────────────────

def _collect(home) -> list[str]:
    """The signalled session ids, each marker removed before its wake (a later signal re-arms it)."""
    sessions = []
    with contextlib.suppress(FileNotFoundError), os.scandir(signal_dir(home)) as entries:
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_file():
                continue
            with contextlib.suppress(OSError):
                session_id = Path(entry.path).read_text(encoding="utf-8").strip()
                os.unlink(entry.path)
                if session_id:
                    sessions.append(session_id)
    return sessions


def pick_up(authority, session_ids) -> list[str]:
    """Schedule the drain of each handed-off conversation this owner serves (mounting a conversation no
    inbound has published since the restart); returns those scheduled. One it cannot mount stays queued
    for its next drain or startup recovery."""
    from gateway.session_authorities import owner_scope
    from gateway.session_contract import SessionRef
    from gateway.session_route_recovery import restore_routed_session
    from hermes_state_runtime import RuntimeStoreError
    scheduled = []
    with owner_scope(authority):
        for session_id in dict.fromkeys(session_ids):
            try:
                mounted = session_id in authority.sessions or restore_routed_session(authority, session_id) is not None
            except RuntimeStoreError as exc:  # admission closed (drain, retirement): startup recovery runs it
                logger.info("handed-off admission for %s left queued: %s", session_id, exc.reason)
                continue
            if not mounted:
                logger.info("handed-off admission for %s left queued: no route mounts it here", session_id)
                continue
            authority._schedule(SessionRef(authority.profile_id, session_id))
            scheduled.append(session_id)
    return scheduled


async def _consume(authority, home):
    while True:
        try:
            session_ids = await asyncio.to_thread(_collect, home)
            if session_ids:
                pick_up(authority, session_ids)
        except asyncio.CancelledError:
            raise
        except Exception:  # health: allow BLE001 -- one bad pass must not end the owner's pickup
            logger.warning("handed-off admission pickup failed for %s", home, exc_info=True)
        await asyncio.sleep(PICKUP_SECONDS)


def start_consumer(authority, home):
    """The owner's pickup task (``gateway.session_cron.bind_owner``); cancel it to stop."""
    return asyncio.get_running_loop().create_task(_consume(authority, Path(home)))
