"""The session primitives a plugin context exposes as ``ctx.sessions`` (plan §4.6; worker C wires it).

Every call names the calling session (``caller_session_id``); the primitives apply only the pluggable ACL
(``agent.session_directory.set_session_acl``, default: the caller's descendants plus sessions whose
``delivery_target`` names it) and the store's own safety rules (same profile database, target not closed,
FIFO cap). Policy (hop caps, sibling widening, presentation) belongs to the plugin.

- ``list(caller_session_id, scope="children"|"tree"|"siblings", query=None, limit=20)`` — Freya's rule.
- ``observe(caller_session_id, session_id, limit=20, max_chars=12000)`` — record, transcript tail, sub-children.
- ``admit(caller_session_id, target_session_id, text, *, sender_kind="peer", kind="peer_message", meta=None,
  intent="queue", wake_policy=None)`` — one typed system-event admission on the target's FIFO through the
  delivery sinks (a delegate FIFO directly, a gateway chat through its owner).
"""

from __future__ import annotations

from typing import Any, Optional


class SessionsAPI:
    def __init__(self, db):
        self.db = getattr(db, "_db", db)

    def list(self, caller_session_id: str, *, scope: str = "children", query: Optional[str] = None,
             limit: int = 20) -> list[dict]:
        from tools.delegate_session_store import DelegateSessionStore
        return DelegateSessionStore(self.db).list(caller_session_id, query=query, limit=limit, scope=scope)

    def observe(self, caller_session_id: str, session_id: str, *, limit: int = 20, max_chars: int = 12000) -> dict:
        from tools.delegate_session_store import DelegateSessionStore
        return DelegateSessionStore(self.db).observe(caller_session_id, session_id, limit=limit, max_chars=max_chars)

    def admit(self, caller_session_id: str, target_session_id: str, text: str, *, sender_kind: str = "peer",
              kind: str = "peer_message", meta: Optional[dict] = None, wake_policy: Optional[str] = None,
              request_id: Optional[str] = None) -> Any:
        """Deliver *text* from the caller into the target as a typed ``system_event`` (never human). Raises
        LookupError when the ACL does not let the caller reach the target."""
        from agent import session_directory as directory
        from gateway.delivery_targets import DeliveryEvent, dispatch, session_target
        with self.db._read_ctx() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (target_session_id,)).fetchone()
            if row is None or not directory.allowed(self.db, conn, caller_session_id, row):
                raise LookupError("No session with that session_id is reachable from this conversation.")
            if row["status"] == "closed":
                raise LookupError("That session is closed.")
        event = DeliveryEvent(kind, text, sender_kind, caller_session_id, meta=dict(meta or {}))
        return dispatch(self.db, None, event, request_id=request_id,
                        target=session_target(target_session_id, attach=False, wake_policy=wake_policy))
