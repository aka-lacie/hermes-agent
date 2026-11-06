"""Unified-session metadata a producer stamps on a session it owns (keyed webhook / cron sessions).

Set-once (``COALESCE``) writes of the §4.1 columns ``lineage_kind``, ``route`` and ``delivery_target``:
the first delivery that creates a session decides them, later deliveries never rewrite them (a keyed
session's next delivery keeps its stored route, plan §4.3).

The one stable logical key of a session is ``sessions.session_key`` (the gateway routing key every segment of
a conversation shares across compression and ``/new``); producers never add a parallel key column. A keyed
webhook session is addressed through its keyed chat id (``gateway.session_selectors``).

``attached_session_id`` is the attach primitive (:func:`bind_attached_session`): the live session an opted-in
session currently delivers into. It is rebound on creation and on every delivery (a selector target follows
``/new``), so it is not set-once, and it is never ``parent_session_id`` (no continuation walk can mistake an
attached session for a segment of its target).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_JSON_FIELDS = ("route", "delivery_target")
_TEXT_FIELDS = ("lineage_kind",)
META_FIELDS = _TEXT_FIELDS + _JSON_FIELDS


def _encoded(field: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if field in _JSON_FIELDS:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def stamp_session_meta(db: Any, session_id: str, **fields: Any) -> dict[str, bool]:
    """Fill the named columns of *session_id* where they are still NULL. Returns which fields this
    call wrote. Unknown field names raise ``TypeError``."""
    unknown = set(fields) - set(META_FIELDS)
    if unknown:
        raise TypeError(f"unknown session meta fields: {sorted(unknown)}")
    values = {name: _encoded(name, value) for name, value in fields.items() if value is not None}
    if not session_id or not values:
        return {}

    def write(conn):
        return {name: bool(conn.execute(
            f"UPDATE sessions SET {name} = ? WHERE id = ? AND {name} IS NULL",  # fixed column names
            (value, session_id)).rowcount) for name, value in values.items()}
    return db._execute_write(write) or {}


def bind_attached_session(db: Any, session_id: str, target_session_id: Optional[str]) -> bool:
    """Bind *session_id*'s attachment to *target_session_id* (its target's current live session). A no-op
    for a session bound there already, a self-binding or an empty target. True when the binding moved."""
    if not session_id or not target_session_id or session_id == target_session_id:
        return False
    return bool(db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET attached_session_id = ? WHERE id = ? AND attached_session_id IS NOT ?",
        (target_session_id, session_id, target_session_id)).rowcount))


def session_meta(db: Any, session_id: str) -> dict[str, Any]:
    """The §4.1 producer columns of *session_id* (JSON decoded; missing row -> {})."""
    if not session_id:
        return {}
    row = db._read_one("SELECT lineage_kind, route, delivery_target, attached_session_id, session_key "
                       "FROM sessions WHERE id = ?", (session_id,))
    if row is None:
        return {}
    meta = dict(row)
    for name in _JSON_FIELDS:
        raw = meta.get(name)
        try:
            meta[name] = json.loads(raw) if isinstance(raw, str) and raw else None
        except ValueError:
            logger.warning("Session %s has an unreadable %s column", session_id, name)
            meta[name] = None
    return meta
