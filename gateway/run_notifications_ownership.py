"""Completion ownership for GatewayRunner: the session a pinned completion belongs to.

Split out of ``gateway/run_notifications.py`` (itself split out of ``gateway/run.py``); bound onto
``GatewayRunner`` through ``GatewayNotificationsMixin``. A completion may be pinned to a delegate
child's execution transcript: ownership follows only ``model_config._delegate_from`` to the
human-facing session, shared by delivery preflight and route resolution, and no route moves until
that owner is proven.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
from typing import Any, Optional, Sequence, cast

from gateway.session import SessionEntry
from hermes_state_errors import StateDbReplacedError

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

# Upper bound on the ``model_config._delegate_from`` chain walked by _resolve_async_delegation_session
# when canonicalizing a delegate child to its human-facing gateway parent (#92611). Real nesting is
# bounded far lower by delegation.max_spawn_depth (default 2); this only stops a corrupt chain from
# turning one completion event into an unbounded run of sequential session reads. Exceeding it is
# fail-closed, like every other unresolved provenance.
_MAX_DELEGATE_PROVENANCE_HOPS = 16


async def _notification_store_read(store, method, *args):
    """Notification preflight also serves inline SessionDB/SessionStore callers."""
    result = getattr(store, method)(*args)
    return await result if inspect.isawaitable(result) else result


def _is_delegate_notification_target(row):
    if row.get("lineage_kind") == "delegate" or "subagent" in (row.get("source"), row.get("created_source")):
        return True
    config = row.get("model_config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return False
    return isinstance(config, dict) and bool(config.get("_delegate_from"))


async def _same_notification_lineage(db, target, current):
    if target == current:
        return True
    target_tip = await _notification_store_read(db, "get_compression_tip", target)
    current_tip = await _notification_store_read(db, "get_compression_tip", current)
    return bool(target_tip and target_tip == current_tip)


def legacy_notification_route_mismatch(adapter, event):
    """The adapter has already canonicalized this rejected event; an unequal key cannot heal on retry."""
    expected = str((event.metadata or {}).get("gateway_session_key") or "").strip()
    derive = getattr(adapter, "_event_session_key", None)
    actual = derive(event) if expected and callable(derive) else None
    return isinstance(actual, str) and actual != expected


class GatewayNotificationOwnershipMixin:
    """Ownership proof and route resolution for pinned async and process completions."""

    @staticmethod
    def _build_process_completion_event(watcher: dict, session, session_id: str) -> dict:
        """Build a redacted watcher completion, preferring its current handoff owner over stale registration."""
        from gateway.run import _redact_gateway_user_facing_secrets
        from gateway.run_notifications import _WATCHER_ROUTE_FIELDS
        from agent.redact import redact_terminal_output
        from tools.ansi_strip import strip_ansi
        from tools.process_registry import transform_process_output
        _command = getattr(session, "command", "") or ""
        _raw = strip_ansi(session.output_buffer) if session.output_buffer else ""
        _raw = transform_process_output(_raw, command=_command, returncode=session.exit_code,
                                        task_id=getattr(session, "task_id", "") or "") if _raw else _raw
        _raw = redact_terminal_output(_raw, _command)
        _LIMIT = 2000
        # Snap the bounded output tail to a line boundary (#23284).
        if len(_raw) > _LIMIT:
            _tail = _raw[-_LIMIT:]
            _nl = _tail.find("\n")
            _tail = _tail[_nl + 1:] if _nl != -1 else _tail
            _out = f"[… output truncated — showing last {len(_tail)} chars]\n{_tail}"
        else:
            _out = _raw
        return {
            "type": "completion", "session_id": session_id,
            **{k: watcher.get(k, "") for k in _WATCHER_ROUTE_FIELDS},
            "owner_task_id": getattr(session, "owner_task_id", "") or "",
            "task_id": getattr(session, "task_id", "") or "",
            "message_id": str(watcher.get("message_id") or "").strip() or None,
            "started_at": getattr(session, "started_at", None),
            "command": _redact_gateway_user_facing_secrets(_command), "exit_code": session.exit_code,
            "completion_reason": getattr(session, "completion_reason", "exited"),
            "termination_source": getattr(session, "termination_source", ""),
            "output": _redact_gateway_user_facing_secrets(_out),
            "parent_session_id": getattr(session, "parent_session_id", "") or watcher.get("parent_session_id") or "",
        }

    async def _process_completion_target_verdict(self, evt: dict) -> str:
        """Only delegate-owned processes need pre-adapter refusal; ordinary sessions keep authority admission.

        A child's processes are polled or explicitly handed off, never inferred as parent work. A chat
        explicitly resumed on that child keeps its own notices, including compression continuations.
        """
        from gateway.run import _USER_BOUNDARY_END_REASONS
        db = getattr(self, "_session_db", None)
        target = str(evt.get("parent_session_id") or "")
        if db is None or not target:
            return "deliver"
        try:
            row = await _notification_store_read(db, "get_session", target)
            if row is None or not _is_delegate_notification_target(row):
                return "deliver"
            if row.get("ended_at") and row.get("end_reason") in _USER_BOUNDARY_END_REASONS:
                return "terminal"
            key = str(evt.get("session_key") or "")
            store = getattr(self, "session_store", None)
            if store is None or not key:
                return "terminal"
            entry = await _notification_store_read(store, "lookup_by_session_key", key)
            if entry is None or not await _same_notification_lineage(db, target, entry.session_id):
                return "terminal"
            if row.get("end_reason") == "compression":
                tip = await _notification_store_read(db, "get_compression_tip", target)
                if not tip or tip == target:
                    return "retry"
            return "deliver"
        except Exception:
            logger.debug("Delegate process notification ownership unavailable", exc_info=True)
            return "retry"

    async def _resolve_compression_lineage_target(
        self, session_db: Any, session_entry: Optional[SessionEntry], pinned_session_id: str,
    ) -> tuple[str, Optional[str]]:
        """Distinguish uncertain compression ownership from a proven foreign route."""
        try:
            target_session_id = await session_db.get_compression_tip(pinned_session_id)
            if not target_session_id or target_session_id == pinned_session_id:
                return "retry", None  # Rotation may not have published its continuation yet.
            tip_row = await session_db.get_session(target_session_id)
            if tip_row is None or tip_row.get("ended_at"):
                return "retry", None
            if session_entry is None or session_entry.session_id in {pinned_session_id, target_session_id}:
                return "deliver", target_session_id
            # Across several rotations, accept a stale route only when its own tip is the same live target.
            route_row = await session_db.get_session(session_entry.session_id)
            if route_row is None:
                return "retry", None
            if route_row.get("ended_at") and route_row.get("end_reason") == "compression":
                route_tip = await session_db.get_compression_tip(session_entry.session_id)
                if not route_tip or route_tip == session_entry.session_id:
                    return "retry", None
                if route_tip == target_session_id:
                    return "deliver", target_session_id
        except Exception:
            logger.debug("Async-delegation compression ownership lookup failed for %s", pinned_session_id, exc_info=True)
            return "retry", None
        logger.warning(
            "Async-delegation completion for compression lineage %s -> %s "
            "does not own current route %s; dropping injection.",
            pinned_session_id, target_session_id, session_entry.session_id,
        )
        return "terminal", None

    async def _closed_local_target(
        self, session_db: Any, entry: Optional[SessionEntry], pinned_session_id: str, pinned_row: Optional[dict],
    ) -> str:
        """The closed (/new, exit) physical session of a local conversation a completion is noted on
        (#134889 LC-7, ``session_completion_notes``), else ``""``. The pinned session, or the compression
        tip it rotated to, must be the one the conversation's route still names: a local /new opens a new
        route, so the replacement never matches, and messaging routes never qualify. Only a close ends a
        local tip (``session.detach {end_reason}`` stamps the client's own reason: ``cli_close``,
        ``tui_close``, ``tui_shutdown``...), so any end but compression is one."""
        from gateway.config import Platform
        if entry is None or entry.platform != Platform.LOCAL or pinned_row is None:
            return ""
        target, row = pinned_session_id, pinned_row
        if row.get("end_reason") == "compression":
            try:
                target = await session_db.get_compression_tip(pinned_session_id)
                row = await session_db.get_session(target) if target == entry.session_id else None
            except (sqlite3.Error, StateDbReplacedError):
                logger.debug("Closed local tip lookup failed for %s", pinned_session_id, exc_info=True)
                return ""  # the compression path below defers it
        if (target != entry.session_id or row is None or not row.get("ended_at")
                or row.get("end_reason") == "compression"):
            return ""
        return target

    async def _lookup_completion_owner(self, session_id: str, current_session_id: str = ""):
        """Nonmutating delegation proof shared by preflight and route resolution.

        A current route is an explicit ownership boundary, not a route to repair.
        Generic parent pointers also describe compression and branches; only
        ``_delegate_from`` authorizes walking out of an execution transcript.
        """
        from gateway.run import _USER_BOUNDARY_END_REASONS
        session_db = cast(Any, self)._session_db
        if session_db is None:
            return "retry", session_id, None
        seen = set()
        for hops in range(_MAX_DELEGATE_PROVENANCE_HOPS + 1):
            if session_id in seen:
                return "terminal", session_id, None
            seen.add(session_id)
            try:
                row = await session_db.get_session(session_id)
            except Exception:
                logger.debug("Completion ownership lookup failed for %s", session_id, exc_info=True)
                return "retry", session_id, None
            if row is None:
                return "terminal", session_id, None
            # User closure is a boundary at EVERY hop, including execution rows.
            # Ordinary delegate completion may still follow provenance to its owner.
            if row.get("ended_at") and row.get("end_reason") in _USER_BOUNDARY_END_REASONS:
                return "terminal", session_id, row
            if session_id == current_session_id:
                return "deliver", session_id, row
            if current_session_id and _is_delegate_notification_target(row):
                try:
                    if await _same_notification_lineage(session_db, session_id, current_session_id):
                        return "deliver", session_id, row
                except Exception:
                    logger.debug("Resumed delegate notification lineage unavailable", exc_info=True)
                    return "retry", session_id, None
            # A hop compressed into the current route is its conversation (a resumed delegate run), as
            # admission rules: climbing past it would reach the parent the run was spawned from.
            if current_session_id and row.get("end_reason") == "compression":
                try:
                    if await session_db.get_compression_tip(session_id) == current_session_id:
                        return "deliver", session_id, row
                except Exception:
                    logger.debug("Completion compression lookup failed for %s", session_id, exc_info=True)
                    return "retry", session_id, None
            config = row.get("model_config")
            try:
                if isinstance(config, str):
                    config = json.loads(config)
            except (TypeError, ValueError):
                return "terminal", session_id, None
            if config is None:
                config = {}
            if not isinstance(config, dict):
                return "terminal", session_id, None
            if "_delegate_from" not in config:
                verdict = "terminal" if row.get("source") == "subagent" else "deliver"
                return verdict, session_id, row
            parent = config["_delegate_from"]
            if not isinstance(parent, str) or not parent.strip() or hops == _MAX_DELEGATE_PROVENANCE_HOPS:
                return "terminal", session_id, None
            session_id = parent
        return "terminal", session_id, None

    async def _watch_pin_gone(self, evt: dict, refusal: str, *, log: bool = True) -> bool:
        """True (logged once with ``log``) when a refused notice is pinned to a permanently-gone session.

        The session authority admits a notice only into its pinned session or that session's
        compression continuation, so after /new it refuses the notice on every retry and the drain
        would spin on it (#141562). This is the completion pre-flight's verdict, applied after the
        refusal. A dropped notice is gone for good; a /resume reopens the pinned session, so the
        process's later notices are admitted again.
        """
        pinned = str(evt.get("parent_session_id") or "").strip()
        if not pinned:
            return False
        session_key = str(evt.get("session_key") or "")
        verdict, _owner, _target = await cast(Any, self)._classify_completion_owner(pinned, session_key)
        if verdict != "terminal":
            return False
        if not log:
            return True
        logger.warning(
            "Dropping %s notification of process %s: pinned session %s is permanently gone (closed "
            "at a user boundary such as /new, or no longer an owner); the route now runs %s and "
            "refuses it (%s). Output remains available via process(action='log').",
            evt.get("type") or "watch", evt.get("session_id") or "<unknown>", pinned,
            cast(Any, self).session_store.peek_session_id(session_key) or "<none>", refusal,
        )
        return True

    async def _resolve_async_delegation_session(
        self, session_entry: SessionEntry, pinned_session_id: str, proven_session_id: str = "",
        completion_ids: Sequence[str] = (),
    ) -> Optional[SessionEntry]:
        """Resolve an async completion to its verified owning gateway session.

        Follow compression-rotation lineage (parent row ended, child continues), but never let a
        late completion override an unrelated /new or restored route. Unknown ownership fails
        closed; the result stays in the delegation records. An owner that cannot be confirmed right
        now (``retry``) sends the completions admitted with the event back to the delivery queue.

        A pinned *delegate* row is likewise canonicalized to its human-facing parent through
        ``model_config._delegate_from`` before any route check or mutation (#92611). Unresolvable
        provenance — a cycle, a missing parent, a malformed config, or a chain longer than
        ``_MAX_DELEGATE_PROVENANCE_HOPS`` — drops the injection rather than falling back to the
        current route entry. That is deliberate: routing an internal child's output into a session
        whose ownership was never verified is the same class of defect this canonicalization fixes,
        and the completion remains readable in the delegation records either way.
        """
        from gateway.run import _USER_BOUNDARY_END_REASONS
        session_db = cast(Any, self._session_db)
        if session_db is None:
            logger.warning(
                "Async-delegation completion has no session database; "
                "dropping injection (#55578 fail-closed)."
            )
            return None
        if proven_session_id and proven_session_id == session_entry.session_id:
            # Admission already settled the durable claim on this proof; another read could only lose it.
            return session_entry
        # Snapshot before ownership awaits: a concurrent /stop or /new wins.
        run_generation = self._current_session_run_generation(session_entry.session_key)
        verdict, pinned_session_id, pinned_row = await self._lookup_completion_owner(
            pinned_session_id, session_entry.session_id,
        )
        if verdict != "deliver" or pinned_row is None:
            return await self._requeue_uncertain_completion(verdict, completion_ids)
        target_session_id = pinned_session_id
        follows_compression = False
        if pinned_row.get("ended_at"):
            _end_reason = str(pinned_row.get("end_reason") or "")
            if _end_reason in _USER_BOUNDARY_END_REASONS:
                logger.warning(
                    "Async-delegation completion pinned to user-closed session %s "
                    "(end_reason=%r); dropping injection instead of resurrecting it "
                    "(#55578 fail-closed).", pinned_session_id, _end_reason,
                )
                return None
            if _end_reason != "compression":
                # Idle/timeout end (scale-to-zero norm): the chat route is still valid, so deliver to its
                # current session rather than drop (the row would be acked then silently lost).
                logger.info(
                    "Async-delegation completion pinned to %s-ended session %s; "
                    "retargeting to the chat's current session %s.",
                    _end_reason or "idle", pinned_session_id, session_entry.session_id,
                )
                return session_entry
            follows_compression = True
            verdict, target_session_id = await self._resolve_compression_lineage_target(
                session_db, session_entry, pinned_session_id,
            )
            if verdict != "deliver" or target_session_id is None:
                return await self._requeue_uncertain_completion(verdict, completion_ids)
        if target_session_id == session_entry.session_id:
            return session_entry
        prior_session_id = session_entry.session_id
        if not self._is_session_run_current(session_entry.session_key, run_generation):
            logger.warning(
                "Async-delegation completion for routing key %s was invalidated while resolving pinned "
                "session %s; leaving the route on %s and dropping injection.",
                session_entry.session_key, pinned_session_id, prior_session_id,
            )
            return None
        if follows_compression:
            switched = await self.async_session_store.advance_compression_session(
                session_entry.session_key, prior_session_id, target_session_id,
            )
        else:
            # CAS on the session this completion resolved against: a route replaced meanwhile
            # (/new, /resume) wins over the stale completion.
            switched = await self.async_session_store.switch_session(
                session_entry.session_key, target_session_id, expected_session_id=prior_session_id,
            )
        if switched is None:
            logger.warning(
                "Async-delegation completion could not bind routing key %s to "
                "owning session %s (route moved or unknown); dropping injection.",
                session_entry.session_key, target_session_id,
            )
            return None
        logger.info(
            "Pinned async-delegation completion to owning session %s (was %s) for routing key %s (#57498)",
            target_session_id, prior_session_id, session_entry.session_key,
        )
        return switched

    async def _requeue_uncertain_completion(self, verdict: str, completion_ids: Sequence[str]) -> None:
        """Offer admitted completions again when the turn could not confirm their owner (``retry``),
        so the next delivery re-runs the pre-flight; boundary verdicts stay dropped."""
        if verdict != "retry" or not completion_ids:
            return
        from tools.async_delegation import requeue_admitted_completion
        from tools.process_registry import process_registry
        runner = cast(Any, self)
        with runner._completion_delivery_lock:
            for delegation_id in completion_ids:
                runner._completion_deliveries_delivered.pop(("async_delegation", delegation_id, ""), None)
        for delegation_id in completion_ids:
            try:
                requeued = await asyncio.to_thread(
                    requeue_admitted_completion, delegation_id, process_registry.completion_queue)
            except Exception:
                logger.debug("Could not requeue admitted completion %s", delegation_id, exc_info=True)
                requeued = False
            logger.warning(
                "Async-delegation completion %s: owner not confirmed after admission; %s.", delegation_id,
                "offered again" if requeued else "not offered again (result remains queryable)",
            )
