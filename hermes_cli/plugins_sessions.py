"""``ctx.sessions``: the unified-session primitives on the plugin context (plan §4.6).

A thin facade over :class:`agent.sessions_api.SessionsAPI` for the current profile's session database
(acquired from the shared registry per call, never held). Every method names the calling session with the
keyword ``caller_session_id`` (a plugin tool handler receives it as its ``session_id`` dispatch kwarg).

- ``list(scope="children"|"siblings"|"tree", *, caller_session_id, query=None, limit=20)``
- ``observe(session_id, *, caller_session_id, limit=None)``: newest N (default 6, max 20), compact
- ``admit(target_session_id, *, caller_session_id, sender_kind="peer", intent="queue", parts=None, text=None,
  kind=None, meta=None, wake_policy=None, request_id=None)``: refusals raise ``SessionAdmissionError``
  (``.code``, ``.to_dict()``), e.g. ``peer_hop_limit`` past ``PEER_HOP_MAX``
- ``resolve(selector, *, profile=None)``
- ``set_acl(policy, *, action="observe"|"peer")``: replace an ACL (``agent.session_directory``); the
  previous one is restored when the plugin unloads.

Policy stays pluggable: nothing here hardcodes ``caller == parent``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Optional


class PluginSessions:
    def __init__(self, ctx: Any = None, *, db_path: Any = None):
        from agent.sessions_api import PEER_HOP_MAX, SessionAdmissionError
        self._ctx, self._db_path = ctx, db_path
        self.PEER_HOP_MAX = PEER_HOP_MAX
        self.SessionAdmissionError = SessionAdmissionError

    @contextmanager
    def _api(self):
        from agent.sessions_api import SessionsAPI
        from hermes_state_registry import acquire, release_or_close
        db = acquire(self._db_path)
        try:
            yield SessionsAPI(db)
        finally:
            release_or_close(db)

    @staticmethod
    def _caller(caller_session_id: Optional[str]) -> str:
        if not isinstance(caller_session_id, str) or not caller_session_id.strip():
            raise ValueError("caller_session_id is required (the calling session's id).")
        return caller_session_id.strip()

    def list(self, scope: str = "children", *, caller_session_id: str, query: Optional[str] = None,
             limit: int = 20) -> list[dict]:
        with self._api() as api:
            return api.list(self._caller(caller_session_id), scope=scope, query=query, limit=limit)

    def observe(self, session_id: str, *, caller_session_id: str, limit: Optional[int] = None) -> dict:
        with self._api() as api:
            return api.observe(self._caller(caller_session_id), session_id, limit=limit)

    def admit(self, target_session_id: str, *, caller_session_id: str, sender_kind: str = "peer",
              intent: str = "queue", parts: Optional[list] = None, text: Optional[str] = None,
              kind: Optional[str] = None, meta: Optional[dict] = None, wake_policy: Optional[str] = None,
              request_id: Optional[str] = None) -> dict:
        with self._api() as api:
            return api.admit(self._caller(caller_session_id), target_session_id, text, sender_kind=sender_kind,
                             intent=intent, parts=parts, kind=kind, meta=meta, wake_policy=wake_policy,
                             request_id=request_id)

    def resolve(self, selector: str, *, profile: Optional[str] = None) -> Optional[str]:
        with self._api() as api:
            return api.resolve(selector, profile=profile)

    def set_acl(self, policy: Optional[Callable[..., bool]], *, action: str = "observe") -> Callable[..., bool]:
        from agent import session_directory as directory
        previous = directory.set_session_acl(policy, action=action)
        if self._ctx is not None and hasattr(self._ctx, "on_unload"):
            def restore(installed=directory.get_session_acl(action)):
                if directory.get_session_acl(action) is installed:
                    directory.set_session_acl(previous, action=action)
            self._ctx.on_unload(restore)
        return previous
