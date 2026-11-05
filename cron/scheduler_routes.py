"""Stored route of a cron fire's session (unified-session plan §4.3).

A job that pins its own ``provider`` / ``model`` / ``base_url`` or declares ``fallback_providers``
gets that route recorded, credential-free (hermes_cli/session_route.py), in ``sessions.route`` of the
fire's session, next to ``lineage_kind = "cron"``. A job that declares nothing follows the main model
and stores no route, as before. Best-effort: a failed stamp never fails the fire.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def cron_job_route(job: dict) -> Optional[dict]:
    """The credential-free route *job* declares, else None."""
    from hermes_cli.session_route import declared_route
    extra = {"fallback_providers": job["fallback_providers"]} if "fallback_providers" in job else {}
    return declared_route(provider=job.get("provider"), model=job.get("model"), base_url=job.get("base_url"), **extra)


def stamp_cron_session_route(session_db: Any, session_id: Optional[str], job: dict, setup: Any = None) -> None:
    """Set-once ``lineage_kind`` / ``route`` of a cron fire's session row (when the row exists)."""
    if session_db is None or not session_id:
        return
    try:
        from hermes_state_session_meta import stamp_session_meta
        stamp_session_meta(session_db, session_id, lineage_kind="cron", route=cron_job_route(job))
    except Exception:  # health: allow BLE001 -- bookkeeping only; the fire's own outcome stands
        logger.warning("Job '%s': could not record the session route", job.get("id"), exc_info=True)
