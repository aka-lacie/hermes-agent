"""Unified-session metadata a producer stamps on a session it owns (keyed webhook / cron sessions).

Set-once (``COALESCE``) writes of the §4.1 columns ``lineage_kind``, ``route``, ``delivery_target``
and ``conversation_key``: the first delivery that creates a session decides them, later deliveries
never rewrite them (a keyed session's next delivery keeps its stored route, plan §4.3).
``conversation_key`` is unique per ``source`` (``idx_sessions_conversation_key``): a stamp that
would collide is skipped and reported, never raised into the delivery path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

_JSON_FIELDS = ("route", "delivery_target")
_TEXT_FIELDS = ("lineage_kind", "conversation_key")
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
        written = {}
        for name, value in values.items():
            try:
                written[name] = bool(conn.execute(
                    f"UPDATE sessions SET {name} = ? WHERE id = ? AND {name} IS NULL",  # fixed column names
                    (value, session_id)).rowcount)
            except sqlite3.IntegrityError:
                logger.warning("Session %s: %s %r is already held by another session; not stamped",
                               session_id, name, value)
                written[name] = False
        return written
    return db._execute_write(write) or {}


def session_meta(db: Any, session_id: str) -> dict[str, Any]:
    """The §4.1 producer columns of *session_id* (JSON decoded; missing row -> {})."""
    if not session_id:
        return {}
    row = db._read_one("SELECT lineage_kind, conversation_key, route, delivery_target FROM sessions WHERE id = ?",
                       (session_id,))
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


def find_session_by_conversation_key(db: Any, source: str, conversation_key: str) -> Optional[str]:
    """The session id holding *conversation_key* for *source*, else None."""
    if not source or not conversation_key:
        return None
    row = db._read_one("SELECT id FROM sessions WHERE source = ? AND conversation_key = ?",
                       (source, conversation_key))
    return str(row["id"]) if row is not None else None
