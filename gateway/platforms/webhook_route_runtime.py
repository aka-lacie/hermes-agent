"""Turn-time route of a webhook session: the route's model pin and its declared fallback chain.

A webhook route may pin ``provider`` / ``model`` and declare ``fallback_providers``
(``[{provider, model}]``, ``[]`` = none). The credential-free route (agent/session_routes.py) is
stored in ``sessions.route`` of the session once (``webhook_session_keys.stamp_admitted_session``;
re-derived and stamped here when a turn starts before that stamp landed), and every turn of the
session reads it back, so a keyed session's next delivery keeps the same primary and chain:

- runtime (``GatewayRunner._resolve_session_agent_runtime`` -> :func:`pinned_runtime`): a pinned
  route resolves its own provider; when that fails and the route declared a chain, the chain is
  walked (with the same pre-agent notice the profile path uses), else the turn fails as a pinned
  turn always has;
- mid-run ladder (``AIAgent(fallback_model=...)`` and cached-agent refresh ->
  :func:`turn_fallback_chain`): the declared chain, else the shared pin rule (a pinned route never
  borrows the profile chain; an unpinned one inherits it).

A route that declares none of these stores no route and behaves exactly as before.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def declared_route_for(route_config: Any) -> Optional[dict]:
    """The credential-free route a webhook route config declares, else None."""
    from agent.session_routes import declared_route
    if not isinstance(route_config, dict):
        return None
    extra = {"fallback_providers": route_config["fallback_providers"]} if "fallback_providers" in route_config else {}
    return declared_route(provider=route_config.get("provider"), model=route_config.get("model"), **extra)


def _webhook_route_name(source: Any) -> Optional[str]:
    from gateway.config import Platform
    if source is None or getattr(source, "platform", None) != Platform.WEBHOOK:
        return None
    user_id = str(getattr(source, "user_id", "") or "")
    return user_id[len("webhook:"):] if user_id.startswith("webhook:") else None


def _route_config(runner: Any, source: Any, name: str) -> Optional[dict]:
    try:
        adapter = runner._adapter_for_source(source)
        adapter._reload_dynamic_routes()
        config = adapter._routes.get(name)
    except (AttributeError, KeyError, TypeError):
        logger.debug("webhook route %s config unavailable", name, exc_info=True)
        return None
    return config if isinstance(config, dict) else None


def _db(runner: Any):
    db = getattr(runner, "_session_db", None)
    return getattr(db, "_db", db)


def stored_route(runner: Any, source: Any, session_id: Optional[str]) -> tuple[Optional[dict], Optional[dict]]:
    """``(route, route_config)`` of a webhook session's turn; ``(None, None)`` off webhooks. The stored
    route of the session (or its compression root) wins; a session without one gets the route config's
    declaration, stamped set-once on the root so later deliveries reuse it."""
    name = _webhook_route_name(source)
    if name is None:
        return None, None
    config = _route_config(runner, source, name)
    db = _db(runner)
    if db is None or not session_id:
        return declared_route_for(config), config
    try:
        return _stored_or_stamped(db, session_id, config), config
    except Exception:  # health: allow BLE001 -- a store read must never fail the turn; the config route applies
        logger.warning("webhook session %s route unreadable; using the route config", session_id, exc_info=True)
        return declared_route_for(config), config


def _stored_or_stamped(db: Any, session_id: str, config: Optional[dict]) -> Optional[dict]:
    from hermes_state_session_meta import session_meta, stamp_session_meta
    lineage = db.get_compression_lineage(session_id) or [session_id]
    for sid in (session_id, lineage[0]):
        if isinstance(route := session_meta(db, sid).get("route"), dict):
            return route
    route = declared_route_for(config)
    if route is not None:
        stamp_session_meta(db, lineage[0], route=route)
    return route


def _session_id_for_key(runner: Any, session_key: Optional[str]) -> Optional[str]:
    store = getattr(runner, "session_store", None)
    entry = store.lookup_by_session_key(session_key) if store is not None and session_key else None
    return getattr(entry, "session_id", None)


def _walk_chain(route: dict, config: Optional[dict], exc: Exception) -> tuple[dict, str, dict]:
    """``(runtime kwargs, model, entry)`` of the first declared chain entry that resolves."""
    from gateway.run import _runtime_agent_kwargs
    from hermes_cli.fallback_config import effective_runtime_provider, resolve_entry_api_key
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from agent.session_routes import rehydrate_chain
    chain = rehydrate_chain(route.get("fallback_providers"), (config or {}).get("fallback_providers") or ())
    for entry in chain or ():
        kwargs = {"requested": entry["provider"], "target_model": entry["model"]}
        if entry.get("base_url"):
            kwargs["explicit_base_url"] = entry["base_url"]
        if api_key := resolve_entry_api_key(entry):
            kwargs["explicit_api_key"] = api_key
        try:
            runtime = resolve_runtime_provider(**kwargs)
        except Exception:
            logger.warning("webhook route fallback %s/%s unavailable", entry["provider"], entry["model"], exc_info=True)
            continue
        runtime["provider"] = effective_runtime_provider(entry, runtime)
        return _runtime_agent_kwargs(runtime), entry["model"], entry
    raise exc


def pinned_runtime(runner: Any, source: Any, session_key: Optional[str], model: str,
                   runtime_kwargs: dict) -> tuple[str, dict]:
    """``(model, runtime_kwargs)`` for this turn: unchanged unless *source* is a webhook session whose
    stored route pins a provider and/or model."""
    from agent.session_routes import route_pinned
    if _webhook_route_name(source) is None:
        return model, runtime_kwargs
    route, config = stored_route(runner, source, _session_id_for_key(runner, session_key))
    if not route_pinned(route):
        return model, runtime_kwargs
    pinned_model = route.get("model")
    if not route.get("provider"):
        return pinned_model or model, runtime_kwargs
    from gateway.run import _resolve_runtime_agent_kwargs_for_provider
    try:
        resolved = _resolve_runtime_agent_kwargs_for_provider(route["provider"], target_model=pinned_model or None)
    except Exception as exc:  # health: allow BLE001 -- re-raised by _walk_chain when no declared entry resolves
        resolved, fb_model, entry = _walk_chain(route, config, exc)
        from hermes_cli.fallback_config import pre_agent_fallback_notice
        runner._pre_agent_fallback_notice = pre_agent_fallback_notice(
            route["provider"], pinned_model, resolved.get("provider") or entry["provider"], fb_model)
        return fb_model, resolved
    runtime_model = resolved.pop("model", None)
    return pinned_model or runtime_model or model, resolved


def turn_fallback_chain(runner: Any, source: Any, session_id: Optional[str],
                        inherited: Callable[[], Optional[list]]) -> Optional[list]:
    """The chain this turn's agent may walk: the profile chain (*inherited*) unless *source* is a
    webhook session with a stored route, which applies its declared chain / the shared pin rule."""
    route, config = stored_route(runner, source, session_id)
    if route is None:
        return inherited()
    from agent.session_routes import route_fallback_chain
    return route_fallback_chain(route, inherited(), owner=f"webhook route {_webhook_route_name(source)}",
                                current=(config or {}).get("fallback_providers") or ())
