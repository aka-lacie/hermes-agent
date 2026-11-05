"""Session mirroring for cross-platform message delivery.

When a message is sent to a platform (send_message or cron delivery), append a
"delivery-mirror" record to the target session's transcript so the receiving-side
agent knows what was sent.  Standalone: works from CLI, cron and gateway contexts.

A mirror is never the target session's model speaking, so it is always a ``role="user"``
row holding one ``system_event`` part (Rule 1: assistant rows are model output only), with
the producer named in ``sender_id`` (``cron:<job>``, ``webhook:<route>``, ``session:<id>``).
"""

import json
import logging
from pathlib import Path
from typing import Optional

from agent.message_parts import MESSAGE_PARTS, PROVENANCE_FIELDS, system_event_message
from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)

MIRROR_EVENT_KIND = "delivery_mirror"
_SESSIONS_DIR = get_hermes_home() / "sessions"
_SESSIONS_INDEX = _SESSIONS_DIR / "sessions.json"
_SESSIONS_INDEX_AT_IMPORT = _SESSIONS_INDEX


def _resolve_sessions_index() -> Path:
    """Active profile's ``sessions.json`` at call time: the patched ``_SESSIONS_INDEX`` when a test
    changed it, else live profile-scoped HERMES_HOME — under the multiplexed gateway one process
    serves every profile, so the import-time constant would resolve every profile's pre-migration
    session lookup against the launch profile's index."""
    return (_SESSIONS_INDEX if _SESSIONS_INDEX != _SESSIONS_INDEX_AT_IMPORT
            else get_hermes_home() / "sessions" / "sessions.json")


def _origin_user_id(entry: dict) -> str:
    return str((entry.get("origin") or {}).get("user_id") or "")


def mirror_to_session(
    platform: str, chat_id: str, message_text: str, source_label: str = "cli", thread_id: Optional[str] = None,
    user_id: Optional[str] = None, session_id: Optional[str] = None, *, sender_id: str,
    exclude_session_id: Optional[str] = None, event_kind: str = MIRROR_EVENT_KIND, sender_kind: str = "system",
    meta: Optional[dict] = None,
) -> bool:
    """Append a delivery-mirror notice to the target session's SQLite transcript.

    Pass ``session_id`` when the caller already holds the exact session (e.g. the
    cron in_channel seed) to skip the origin scan, which refuses to guess on a
    populated chat (flat + N thread sessions per chat_id) and would drop the mirror.
    The row is ``role="user"``, ``sender_kind="system"``, ``sender_id=sender_id`` with one
    ``system_event`` part: it collapses safely into the next user turn via the
    consecutive-user merge and never teaches the model a reply it did not write.
    ``exclude_session_id``: the sending session itself; a send into its own chat is already
    on record as its tool call, so nothing is mirrored there. ``event_kind`` / ``sender_kind`` /
    ``meta`` type a producer's own delivery (``cron`` with the job, ``webhook`` with the route):
    provenance is structure, never a bracket label prefixed to ``message_text`` (Rule 2).
    Returns True if mirrored, False if no matching session or error. Never raises.
    """
    try:
        if not session_id:
            session_id = _find_session_id(platform, str(chat_id), thread_id=thread_id, user_id=user_id)
        if not session_id:
            logger.warning(
                "Mirror: no session found for %s:%s thread=%s user=%s (explicit_id=none, origin-scan bailed)",
                platform, chat_id, thread_id, user_id,
            )
            return False
        if exclude_session_id and session_id == exclude_session_id:
            logger.debug("Mirror: skipped, %s sent into its own session", session_id)
            return False
        _append_to_sqlite(session_id, system_event_message(
            event_kind, message_text, sender_id=sender_id, sender_kind=sender_kind,
            meta={"source": source_label, "platform": platform, **(meta or {})}))
        logger.debug("Mirror: wrote to session %s (from %s)", session_id, source_label)
        return True
    except Exception as e:
        # WARNING, not debug: a silent mirror drop is the cron continuation-amnesia bug.
        logger.warning("Mirror failed for %s:%s thread=%s user=%s session=%s: %s", platform, chat_id, thread_id, user_id, session_id, e)
        return False


def _find_session_id(platform: str, chat_id: str, thread_id: Optional[str] = None, user_id: Optional[str] = None) -> Optional[str]:
    """Active session_id for a platform + chat_id pair.

    state.db is primary; sessions.json is the pre-migration fallback.  DM keys
    don't embed the chat_id ("agent:main:telegram:dm"), so match on the persisted
    origin.  With *user_id*, exact sender matches win; several same-chat candidates
    with no user match → None rather than contaminate another participant's session.

    Queries state.db gateway session rows (primary source since #9006); falls back to scanning sessions.json
    for pre-migration databases.
    """
    try:
        from hermes_state_registry import acquire, release_or_close
        db = acquire()
        try:
            finder = getattr(db, "find_session_by_origin", None)
            session_id = finder(platform=platform, chat_id=chat_id, thread_id=thread_id, user_id=user_id) if callable(finder) else None
            if session_id:
                return str(session_id)
        finally:
            release_or_close(db)
    except Exception as e:
        logger.debug("Mirror state.db session lookup failed: %s", e)

    sessions_index = _resolve_sessions_index()
    if not sessions_index.exists():
        return None
    try:
        data = json.loads(sessions_index.read_text(encoding="utf-8-sig"))
    except Exception:
        return None

    def _matches(entry: dict) -> bool:
        origin = entry.get("origin") or {}
        return ((origin.get("platform") or entry.get("platform", "")).lower() == platform.lower()
                and str(origin.get("chat_id", "")) == str(chat_id)
                and (thread_id is None or str(origin.get("thread_id") or "") == str(thread_id)))

    # Keys starting with "_" (e.g. the gateway's "_README") are metadata sentinels.
    candidates = [e for k, e in data.items() if not str(k).startswith("_") and isinstance(e, dict) and _matches(e)]
    if not candidates:
        return None
    if user_id:
        exact_user_matches = [e for e in candidates if _origin_user_id(e) == str(user_id)]
        if exact_user_matches:
            candidates = exact_user_matches
        elif len(candidates) > 1:
            return None
    elif len(candidates) > 1 and len({u.strip() for u in map(_origin_user_id, candidates) if u.strip()}) > 1:
        return None
    return max(candidates, key=lambda entry: entry.get("updated_at", "")).get("session_id")


def _append_to_sqlite(session_id: str, message: dict) -> None:
    """Append a message to the SQLite session database.

    Raises on failure: ``mirror_to_session`` reports ``False`` (and warns) only when the
    exception reaches it — swallowing it here made every failed write look mirrored (#10130).
    """
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        db.append_message(
            session_id=session_id, role="user", parts=message[MESSAGE_PARTS],
            **{key: message[key] for key in PROVENANCE_FIELDS if message.get(key) is not None})
    finally:
        release_or_close(db)
