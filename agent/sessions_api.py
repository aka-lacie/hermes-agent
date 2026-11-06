"""The session primitives a plugin context exposes as ``ctx.sessions`` (plan §4.6).

Every call names the calling session (``caller_session_id``); the primitives apply only the pluggable ACLs
(``agent.session_directory.set_session_acl``: observe/control default = the caller's descendants plus
sessions whose ``delivery_target`` names it; ``action="peer"`` default = siblings) and the store's own
global safety rules (same profile database, target not closed, FIFO cap, the peer hop cap). Presentation is
compact by construction (``agent.session_views``); policy beyond that belongs to the plugin.

- ``list(caller_session_id, scope="children"|"tree"|"siblings", query=None, limit=20)``: Freya's rule, flat
  compact rows (grandchildren only as ``running_children`` / ``children_total``).
- ``observe(caller_session_id, session_id, limit=6)``: the newest *limit* messages (max 20), chronological,
  each capped, plus a compact ``children`` roll-up (< 1.5 KB). Any descendant, read-only.
- ``admit(caller_session_id, target_session_id, text=None, *, sender_kind="peer", intent="queue", parts=None,
  kind=None, meta=None, wake_policy=None, request_id=None)``: one typed ``system_event`` admission on the
  target's FIFO (never a human row). ``sender_kind="peer"``: kind ``peer_message`` with meta
  ``{sender_id, sender_title, relation}``, the sibling ACL, and ``peer_hop`` = the caller's triggering hop
  + 1 (1 when the caller's current turn was not peer-triggered); above :data:`PEER_HOP_MAX` it is refused
  with :class:`SessionAdmissionError` ``peer_hop_limit``. Returns a small receipt dict.
  ``request_id`` makes the call idempotent: an exact retry (same caller, target, text, kind, meta,
  intent) returns the original admission's receipt, also when it overlaps the first call (the store
  reconciles both inside the admission transaction); the same ``request_id`` with a different request is
  refused ``request_conflict``. Store refusals surface as :class:`SessionAdmissionError` too, never raw.
- ``resolve(selector, profile=None)``: the session a logical selector names right now, or None.

``observe`` and ``admit`` also take a selector wherever they take a session id (resolved first; the ACL
applies to the session it resolves to).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

PEER_HOP_MAX = 8
PEER_KIND = "peer_message"
_INTENTS = ("queue", "steer")


class SessionAdmissionError(LookupError):
    """A refused admission, with a stable machine-readable ``code`` (``unavailable``, ``closed``,
    ``not_resumable``, ``invalid_message``, ``invalid_intent``, ``peer_hop_limit``, ``request_conflict``,
    ``queue_full``, ``invalid_request``, ``refused``)."""

    def __init__(self, code: str, message: str, **details: Any):
        self.code, self.details = code, details
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"error": self.code, "message": str(self), **self.details}


def _store_db(db):
    return getattr(db, "_db", db)


# The state authority's refusal reasons, as the facade's codes (anything else: ``refused``).
_STORE_REFUSALS = {
    "closed": ("closed", "That session is closed."),
    "admission_conflict": ("request_conflict", "This request_id was already used for a different message."),
    "queue_full": ("queue_full", "That session already has the maximum queued inputs."),
    "not_found": ("unavailable", "No session with that session_id is reachable from this conversation."),
    "invalid_params": ("invalid_request", "The admission request is malformed."),
}


def _store_refusal(exc) -> SessionAdmissionError:
    reason = str(getattr(exc, "reason", "") or "")
    code, message = _STORE_REFUSALS.get(reason, ("refused", "The session store refused this admission."))
    details = {"reason": reason}
    if getattr(exc, "field", None):
        details["field"] = exc.field
    return SessionAdmissionError(code, message, **details)


def triggering_peer_hop(db, conn, session_id: str) -> int:
    """The peer hop of the turn *session_id* is running now: the highest ``peer_hop`` of its started claim's
    events when every input of that claim is a peer message, else 0 (a human, parent or system input resets
    the chain)."""
    import json
    from agent import session_directory as directory
    from gateway.delivery_targets import event_events
    hop = 0
    for sid in directory.lineage(db, conn, session_id) or [session_id]:
        for (raw,) in conn.execute("SELECT payload_json FROM session_admissions WHERE target_session_id = ? "
                                   "AND status = 'started'", (sid,)).fetchall():
            try:
                payload = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                payload = {}
            events = event_events(payload if isinstance(payload, dict) else {})
            if not events or any(e.get("sender_kind") != "peer" for e in events):
                return 0
            hop = max(hop, *(int(e.get("peer_hop") or 1) for e in events))
    return hop


def _part_text(parts: Any) -> str:
    """The plain text of caller-supplied parts; a peer never speaks as the user (no ``human`` parts)."""
    if not isinstance(parts, list) or not parts:
        raise SessionAdmissionError("invalid_message", "parts must be a non-empty list of part objects.")
    texts = []
    for part in parts:
        if not isinstance(part, Mapping) or part.get("type") == "human":
            raise SessionAdmissionError("invalid_message", "Admitted parts are typed events; a human part is refused.")
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "\n\n".join(t for t in texts if t.strip())


class SessionsAPI:
    def __init__(self, db):
        self.db = _store_db(db)

    def list(self, caller_session_id: str, *, scope: str = "children", query: Optional[str] = None,
             limit: int = 20) -> list[dict]:
        from agent.session_views import list_row
        from tools.delegate_session_store import DelegateSessionStore
        return [list_row(r) for r in DelegateSessionStore(self.db).list(caller_session_id, query=query, limit=limit,
                                                                         scope=scope)]

    def resolve(self, selector: str, *, profile: Optional[str] = None) -> Optional[str]:
        from gateway.session_selectors import resolve_session_selector
        return resolve_session_selector(self.db, selector, profile=profile)

    def observe(self, caller_session_id: str, session_id: str, *, limit: Optional[int] = None,
                max_chars: Optional[int] = None) -> dict:
        from tools.delegate_session_store import DelegateSessionStore
        return DelegateSessionStore(self.db).observe(caller_session_id, session_id, limit=limit, max_chars=max_chars)

    def admit(self, caller_session_id: str, target_session_id: str, text: Optional[str] = None, *,
              sender_kind: str = "peer", intent: str = "queue", parts: Optional[list] = None,
              kind: Optional[str] = None, meta: Optional[dict] = None, wake_policy: Optional[str] = None,
              request_id: Optional[str] = None) -> dict:
        """Deliver one typed event from the caller into the target's FIFO. Raises
        :class:`SessionAdmissionError` (a ``LookupError``) when refused; returns ``{delivered, session_id,
        admission_id, status, sink[, peer_hop][, reason]}``."""
        from agent import session_directory as directory
        from gateway.delivery_targets import DeliveryEvent, dispatch, session_target
        from hermes_state_runtime import RuntimeStoreError
        from tools.delegate_session_store import DelegateSessionStore
        if intent not in _INTENTS:
            raise SessionAdmissionError("invalid_intent", "intent must be 'queue' or 'steer'.")
        if parts is not None:
            text = _part_text(parts) if text is None else text
        if not isinstance(text, str) or not text.strip():
            raise SessionAdmissionError("invalid_message", "A non-empty message is required.")
        peer = sender_kind == "peer"
        target_session_id = DelegateSessionStore(self.db).resolve_ref(target_session_id)
        with self.db._read_ctx() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (target_session_id,)).fetchone()
            reachable = row is not None and directory.allowed(self.db, conn, caller_session_id, row,
                                                              action="peer" if peer else "observe")
            if not reachable:
                raise SessionAdmissionError("unavailable", "No session with that session_id is reachable from "
                                            "this conversation.")
            info = directory.record(conn, row)
            if info["status"] == "closed":
                raise SessionAdmissionError("closed", "That session is closed.")
            event_meta = dict(meta or {})
            hop = None
            if peer:
                if info["lineage_kind"] == "delegate" and not info["retained"]:
                    raise SessionAdmissionError("not_resumable", "That session is a one-off child; it cannot "
                                                "receive messages.")
                own = conn.execute("SELECT * FROM sessions WHERE id = ?",
                                   (directory.logical_root(self.db, conn, caller_session_id),)).fetchone()
                title = directory.record(conn, own)["title"] if own is not None else None
                relation = "sibling" if directory.are_siblings(self.db, conn, caller_session_id, row) else "peer"
                event_meta.update(sender_id=caller_session_id, sender_title=title, relation=relation)
                hop = triggering_peer_hop(self.db, conn, caller_session_id) + 1
        if hop is not None and hop > PEER_HOP_MAX:
            logger.warning("peer admission %s -> %s refused: peer_hop %d exceeds PEER_HOP_MAX %d",
                           caller_session_id, target_session_id, hop, PEER_HOP_MAX)
            raise SessionAdmissionError(
                "peer_hop_limit", f"This peer conversation reached {PEER_HOP_MAX} hops; it stops here. Report to "
                "your parent instead.", peer_hop=hop, max=PEER_HOP_MAX)
        event = DeliveryEvent(kind or (PEER_KIND if peer else "session_message"), text, sender_kind, caller_session_id,
                              meta=event_meta, peer_hop=hop, intent=intent)
        target = session_target(target_session_id, attach=False, wake_policy=wake_policy)
        try:
            receipt = dispatch(self.db, None, event, request_id=request_id, target=target)
        except RuntimeStoreError as exc:
            raise _store_refusal(exc) from exc
        out = {"delivered": receipt.delivered, "session_id": target_session_id, "admission_id": receipt.admission_id,
               "sink": receipt.sink, "status": "queued" if receipt.delivered else "not_delivered"}
        if hop is not None:
            out["peer_hop"] = hop
        if receipt.reason:
            out["reason"] = receipt.reason
        if receipt.delivered and receipt.sink == "session_fifo":
            from tools.delegate_sessions import wake_delegate
            busy = info["status"] == "running"
            woke = wake_delegate(self.db, target_session_id)
            out["status"] = "queued_next_turn" if busy else ("delivered" if woke else "queued")
        return out
