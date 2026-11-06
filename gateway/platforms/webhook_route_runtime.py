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


def configured_route(route_config: dict, platform: dict) -> dict:
    """Owner platform defaults apply only where the route has no supplied pin/chain."""
    result = dict(route_config)
    if result.get("deliver_only") or result.get("cron_job"):
        return result
    for key in ("model", "provider", "fallback_providers"):
        if result.get(key) is None and platform.get(key) is not None:
            result[key] = platform[key]
    return result


def _complete_pin(config: dict) -> dict:
    """Freeze missing halves of an explicitly pinned worker selection, not credentials."""
    if not (config.get("model") or config.get("provider")):
        return config
    if config.get("model") and config.get("provider") and str(config["provider"]).strip().lower() != "auto":
        return config
    from gateway.run import _load_gateway_config, _resolve_gateway_model
    profile = _load_gateway_config()
    model = profile.get("model") or {}
    model = model if isinstance(model, dict) else {}
    provider = config.get("provider") or model.get("provider")
    if not provider:
        from hermes_cli.runtime_provider import resolve_requested_provider
        provider = resolve_requested_provider()
    if str(provider).strip().lower() == "auto":
        from hermes_cli.runtime_provider import _LOCAL_BYPASS_CLOUD_HOSTS, base_url_host_matches
        base = str(model.get("base_url") or "").strip()
        if (base and str(model.get("provider") or "auto").strip().lower() == "auto"
                and not any(base_url_host_matches(base, host) for host in _LOCAL_BYPASS_CLOUD_HOSTS)):
            provider = "custom"
        else:
            from hermes_cli.auth import resolve_provider
            provider = resolve_provider("auto")
    selected_model = config.get("model") or _resolve_gateway_model(profile)
    if not selected_model and provider:
        from hermes_cli.models import get_default_model_for_provider
        selected_model = get_default_model_for_provider(provider)
    return {**config, **({"provider": provider} if provider else {}),
            **({"model": selected_model} if selected_model else {})}


def declared_route_for(route_config: Any) -> Optional[dict]:
    """The credential-free route a webhook route config declares, else None."""
    from agent.session_routes import declared_route
    if not isinstance(route_config, dict):
        return None
    route_config = _complete_pin(route_config)
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
    return configured_route(config, adapter.config.extra) if isinstance(config, dict) else None


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
    # A broken store cannot authorize a different provider or a fresh mutable route.
    return _stored_or_stamped(db, session_id, config), config


def _stored_or_stamped(db: Any, session_id: str, config: Optional[dict]) -> Optional[dict]:
    import json
    from agent.session_routes import validate_route
    from hermes_state_session_meta import stamp_session_meta
    lineage = db.get_compression_lineage(session_id) or [session_id]
    for sid in (session_id, lineage[0]):
        row = db._read_one("SELECT route FROM sessions WHERE id=?", (sid,))
        if row and row['route'] is not None:
            return validate_route(json.loads(row['route']))
    admitted = db._read_one("""SELECT payload_json FROM session_admissions
        WHERE target_session_id IN (?, ?)
          AND json_extract(payload_json, '$.native_text_v1.source.platform')='webhook'
          AND json_extract(payload_json, '$.native_text_v1.automation') IS NULL
        ORDER BY seq LIMIT 1""", (session_id, lineage[0]))
    native = json.loads(admitted['payload_json']).get('native_text_v1', {}) if admitted else {}
    route = (native.get('webhook_delivery') or {}).get('worker_route')
    if route is not None:
        route = validate_route(route)
    elif native.get('webhook_runtime') is not None:
        route = declared_route_for(native['webhook_runtime'])  # pre-unified signed selection
    else:
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


def early_pinned_runtime(runner: Any, source: Any, session_key: Optional[str], model: str):
    """A worker pin resolves before any default-provider authentication or chat override."""
    if _webhook_route_name(source) is None:
        return None
    route, _config = stored_route(runner, source, _session_id_for_key(runner, session_key))
    from agent.session_routes import route_pinned
    if not route_pinned(route):
        return None
    selected_model, runtime = pinned_runtime(runner, source, session_key, model, {})
    runtime['_webhook_runtime_pinned'] = True
    return selected_model, runtime


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
