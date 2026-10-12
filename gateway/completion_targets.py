"""Read-only admission rules for gateway completion targets."""

import json
import logging

logger = logging.getLogger("gateway.run")


def is_delegate_target(row: dict) -> bool:
    """A delegate (including compression continuations) has no gateway wake lane.

    Transport metadata can overwrite source with discord/telegram. The creation
    source and inherited delegation marker still identify the owner.
    """
    if row.get("source") == "subagent" or row.get("created_source") == "subagent":
        return True
    config = row.get("model_config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return False
    return isinstance(config, dict) and bool(config.get("_delegate_from"))


async def classify_completion_target(session_db, parent_session_id: str) -> str:
    """deliver, terminal (closed/missing/delegate), or retry on uncertain storage.

    Children poll their processes or hand them to the parent. Their notifications
    must not create foreground agents on child transcripts. A delegation's final
    result targets the parent and still uses normal gateway delivery.
    """
    from gateway.run import _USER_BOUNDARY_END_REASONS
    if session_db is None:
        return "retry"
    try:
        parent = await session_db.get_session(parent_session_id)
    except Exception:
        logger.debug("Async-completion pre-flight parent lookup failed for %s", parent_session_id, exc_info=True)
        return "retry"
    if parent is None or is_delegate_target(parent):
        return "terminal"
    if not parent.get("ended_at"):
        return "deliver"
    end_reason = str(parent.get("end_reason") or "")
    if end_reason != "compression":
        # Only a USER-closed session (/new, user_exit, session_switch) is unreachable; idle/timeout
        # ends stay routable and the resolver retargets. Boundary set shared with the resolver.
        return "terminal" if end_reason in _USER_BOUNDARY_END_REASONS else "deliver"
    try:
        tip_session_id = await session_db.get_compression_tip(parent_session_id)
        if not tip_session_id or tip_session_id == parent_session_id:
            # Rotation mid-flight: continuation not visible yet. Retry, don't drop.
            return "retry"
        tip = await session_db.get_session(tip_session_id)
    except Exception:
        logger.debug("Async-completion pre-flight tip lookup failed for %s", parent_session_id, exc_info=True)
        return "retry"
    if tip is None or tip.get("ended_at"):
        return "retry"
    return "deliver"
