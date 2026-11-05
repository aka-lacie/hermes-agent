"""Message provenance for SessionDB: bind/restore of the ``messages`` provenance columns.

Bind/restore pair with ``agent.message_parts``: the in-memory keys are the column names, except
``_parts`` <-> ``parts_json``. ``parts_json`` is written only while ``content`` is still the plain view
of the parts; a row whose content was rewritten around its parts is stored untyped (content wins), so
stored parts can never disagree with what FTS, the UI and legacy readers see.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping, MutableMapping, Optional

from agent.message_parts import MESSAGE_PARTS, PROVENANCE_FIELDS, decode_parts_json, encode_parts_json, has_parts, \
    parts_consistent

logger = logging.getLogger("hermes_state")

# ── column bind / restore ─────────────────────────────────────────────────────


def _text_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _epoch_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def stored_parts_json(msg: Mapping[str, Any]) -> Optional[str]:
    """``parts_json`` for *msg*: its parts while ``content`` is their plain view, else NULL (logged)."""
    if not has_parts(msg):
        return None
    if not parts_consistent(msg):
        logger.warning("Message content was rewritten around its typed parts; storing the row untyped")
        return None
    try:
        return encode_parts_json(msg[MESSAGE_PARTS])
    except ValueError as exc:
        logger.warning("Dropping invalid typed parts on write: %s", exc)
        return None


def provenance_row_params(msg: Mapping[str, Any]) -> tuple:
    """Bind values for the provenance columns, in ``_INSERT_MESSAGE_SQL`` order: sender_kind, sender_id,
    sent_at, source_event_id, intent, part_kind, parts_json, turn_outcome."""
    return (_text_or_none(msg.get("sender_kind")), _text_or_none(msg.get("sender_id")),
            _epoch_or_none(msg.get("sent_at")),
            None if msg.get("source_event_id") is None else str(msg.get("source_event_id")),
            _text_or_none(msg.get("intent")), _text_or_none(msg.get("part_kind")),
            stored_parts_json(msg), _text_or_none(msg.get("turn_outcome")))


def restore_provenance_columns(row: Any, msg: MutableMapping[str, Any]) -> None:
    """Copy a fetched row's provenance onto its message dict (every projection, like ``message_uid``).
    A row fetched without the columns (an identity-only probe) leaves *msg* untouched."""
    keys = row.keys()
    for column in PROVENANCE_FIELDS:
        if column in keys and row[column] is not None:
            msg[column] = row[column]
    if "parts_json" in keys and row["parts_json"] is not None:
        parts = decode_parts_json(row["parts_json"])
        if parts is not None:
            msg[MESSAGE_PARTS] = parts
