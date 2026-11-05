"""Credential-free stored routes (unified-session plan §4.3) for owners that declare their own route.

A webhook route or a cron job may pin ``provider`` / ``model`` (/ ``base_url``) and declare its own
``fallback_providers`` chain. The route is resolved once into a credential-free dict and stored in
``sessions.route`` (JSON) so the session's next turn (a keyed webhook re-delivery, a resumed
session) uses the same primary and the same chain:

    {"provider": str?, "model": str?, "endpoint_sha256": str?, "reasoning_effort": str?,
     "fallback_providers": [{"provider", "model", "base_url"?}, ...]?}

``fallback_providers`` present means *declared* (``[]`` = fallback explicitly disabled); absent
means the owner declared nothing and the shared pin rule applies (``scoped_fallback_chain``: a
pinned owner never borrows the profile chain, an unpinned one inherits it). Credentials never enter
the stored route: a chain entry's ``api_key`` / ``key_env`` is re-attached at turn time from the
owner's current config (:func:`rehydrate_chain`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable, Optional

from hermes_cli.fallback_config import _entry_identity, get_fallback_chain, scoped_fallback_chain

logger = logging.getLogger(__name__)

MAX_FALLBACK_ENTRIES = 8  # plan §4.3: a named chain of at most 8 {provider, model} pairs
_ENTRY_FIELDS = ("provider", "model", "base_url")
_ABSENT = object()


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def credential_free_chain(declared: Any) -> list[dict]:
    """*declared* normalized like ``fallback_providers`` (malformed entries dropped), stripped to
    ``provider`` / ``model`` / ``base_url`` and capped at :data:`MAX_FALLBACK_ENTRIES`."""
    chain = get_fallback_chain({"fallback_providers": declared})
    return [{k: entry[k] for k in _ENTRY_FIELDS if entry.get(k)} for entry in chain[:MAX_FALLBACK_ENTRIES]]


def validate_declared_chain(declared: Any, owner: str) -> None:
    """Config-time check of a declared ``fallback_providers``: a list of ``{provider, model}``
    mappings (``[]`` disables fallback). Raises ValueError naming *owner*."""
    if declared is None:
        return
    if not isinstance(declared, list) or not all(
            isinstance(e, dict) and _text(e.get("provider")) and _text(e.get("model")) for e in declared):
        raise ValueError(f"{owner} fallback_providers must be a list of {{provider, model}} entries")


def declared_route(*, provider: Any = None, model: Any = None, base_url: Any = None,
                   reasoning_effort: Any = None, fallback_providers: Any = _ABSENT) -> Optional[dict]:
    """The credential-free route an owner declares, or None when it declares nothing (the owner then
    follows the profile default and its chain, exactly as before)."""
    route: dict[str, Any] = {}
    for key, value in (("provider", provider), ("model", model), ("reasoning_effort", reasoning_effort)):
        if text := _text(value):
            route[key] = text
    if url := _text(base_url):
        route["endpoint_sha256"] = hashlib.sha256(url.rstrip("/").encode()).hexdigest()
    if fallback_providers is not _ABSENT and fallback_providers is not None:
        route["fallback_providers"] = [] if fallback_providers == [] else credential_free_chain(fallback_providers)
    return route or None


def route_pinned(route: Optional[dict]) -> bool:
    """True when the route names its own provider, model or endpoint."""
    return bool(route) and any(route.get(k) for k in ("provider", "model", "endpoint_sha256"))


def rehydrate_chain(chain: Optional[list], current: Iterable[dict] = ()) -> Optional[list]:
    """*chain* with each entry's credential reference (``api_key`` / ``key_env``) taken from the
    owner's *current* declared entries of the same identity; entries the config no longer names
    keep only their credential-free fields (the provider's own credential resolution applies)."""
    if not chain:
        return chain
    by_identity = {_entry_identity(e): e for e in get_fallback_chain({"fallback_providers": list(current)})}
    return [{**entry, **by_identity.get(_entry_identity(entry), {})} for entry in chain]


def route_fallback_chain(route: Optional[dict], inherited: Optional[list], *, owner: str,
                         current: Iterable[dict] = ()) -> Optional[list]:
    """The chain an agent on *route* may walk: the route's declared chain, else the shared pin rule
    over *inherited* (the profile chain). ``None`` = no fallback."""
    declared = route.get("fallback_providers") if route else None
    chain = scoped_fallback_chain(inherited, declared, pinned=route_pinned(route), owner=owner)
    return rehydrate_chain(chain, current) if declared else chain


def encode_route(route: Optional[dict]) -> Optional[str]:
    return None if not route else json.dumps(route, sort_keys=True, separators=(",", ":"))


def decode_route(raw: Any) -> Optional[dict]:
    """A stored ``sessions.route`` value as a dict, or None (missing or unreadable)."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        logger.warning("Ignoring unreadable stored session route")
        return None
    return value if isinstance(value, dict) else None
