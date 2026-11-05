"""Stored session routes (unified-session plan §4.3): resolved once, kept credential-free, re-resolved anew.

``sessions.route`` is the JSON this module writes. It names how a session reaches its model, never with
what secret: a provider name, the model, the reasoning level the route chose (a difficulty route's
effort survives resume), the difficulty/backend labels, the endpoint as a SHA-256 digest when the route
has no stable provider name, and the fallback chain as at most eight ``{provider, model}`` pairs. Spawn,
resume, webhook re-delivery and cron re-fire all turn it back into a live routing block through one
:func:`resolve_route`, which rehydrates credentials from the current provider configuration and refuses
when an anonymous endpoint's digest no longer matches. The chain rides the stored route, so a failover
chain survives resume (no more ``fallback_providers=[]`` forcing).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

ROUTE_VERSION = 1
FALLBACK_MAX = 8
ROUTE_KINDS = frozenset({"named", "default", "parent"})
_FALLBACK_KEYS = frozenset({"provider", "model", "key_env", "endpoint_sha256"})
_ROUTE_KEYS = frozenset({
    "v", "kind", "provider", "model", "reasoning_effort", "fallback_providers", "difficulty", "backend",
    "label", "endpoint_sha256", "on_fallback", "service_tier",
})
# Anything that could carry a secret or a live connection bundle is refused outright, never filtered.
_CREDENTIAL_KEYS = frozenset({"api_key", "base_url", "command", "args", "request_overrides", "credential_pool"})
ON_FALLBACK = frozenset({"revert", "stick"})


class RouteError(ValueError):
    """A stored route that cannot be persisted or re-resolved as recorded."""


def endpoint_fingerprint(base_url: Any) -> str:
    """SHA-256 of a normalized endpoint URL; userinfo (embedded credentials) is never part of it."""
    parsed = urlsplit(str(base_url or "").strip())
    if not parsed.scheme or not parsed.hostname:
        raise RouteError("This unnamed endpoint has no stable URL; configure a named provider before retaining it.")
    scheme, host = parsed.scheme.lower(), parsed.hostname.lower()
    port = parsed.port
    host = f"[{host}]" if ":" in host else host
    if port is not None and (scheme, port) not in {("https", 443), ("http", 80)}:
        host += f":{port}"
    return hashlib.sha256(urlunsplit((scheme, host, parsed.path.rstrip("/"), parsed.query, "")).encode()).hexdigest()


def _digest_ok(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def stored_fallback_chain(raw: Any) -> Optional[list[dict[str, str]]]:
    """The credential-free form of a fallback chain: None (inherit), [] (disabled) or up to eight pairs.
    An entry's ad-hoc ``base_url`` is kept only as its digest; inline keys are dropped (``key_env`` names a
    variable, never a value)."""
    if raw is None:
        return None
    if raw == []:
        return []
    from hermes_cli.fallback_config import get_fallback_chain
    chain = []
    for entry in get_fallback_chain({"fallback_providers": raw})[:FALLBACK_MAX]:
        item = {"provider": entry["provider"], "model": entry["model"]}
        if entry.get("key_env") or entry.get("api_key_env"):
            item["key_env"] = str(entry.get("key_env") or entry.get("api_key_env"))
        if entry.get("base_url"):
            try:
                item["endpoint_sha256"] = endpoint_fingerprint(entry["base_url"])
            except RouteError:
                continue
        chain.append(item)
    return chain


def validate_route(route: Any) -> dict:
    """A route as stored: credential-free and of known shape (raises :class:`RouteError`)."""
    if not isinstance(route, Mapping):
        raise RouteError("A stored route must be an object.")
    keys = set(route)
    if keys & _CREDENTIAL_KEYS:
        raise RouteError("A stored route cannot carry credentials or a connection bundle.")
    if keys - _ROUTE_KEYS:
        raise RouteError(f"Unsupported stored route fields: {', '.join(sorted(keys - _ROUTE_KEYS))}.")
    if route.get("kind") not in ROUTE_KINDS:
        raise RouteError("Unknown stored route kind.")
    if route.get("endpoint_sha256") is not None and not _digest_ok(route["endpoint_sha256"]):
        raise RouteError("A stored endpoint identity must be a SHA-256 digest.")
    if route.get("on_fallback") not in (None, *ON_FALLBACK):
        raise RouteError("on_fallback must be revert or stick.")
    chain = route.get("fallback_providers")
    if chain is not None:
        if not isinstance(chain, list) or len(chain) > FALLBACK_MAX:
            raise RouteError(f"A stored fallback chain holds at most {FALLBACK_MAX} entries.")
        for entry in chain:
            if (not isinstance(entry, Mapping) or set(entry) - _FALLBACK_KEYS
                    or not all(isinstance(entry.get(k), str) and entry[k] for k in ("provider", "model"))):
                raise RouteError("Stored fallback entries are {provider, model} pairs.")
    return dict(route)


def capture_route(*, provider: Any, model: Any, base_url: Any = None, routing_cfg: Optional[Mapping] = None,
                  reasoning_effort: Any = None, difficulty: Any = None, backend: Any = None, label: Any = None,
                  service_tier: Any = None) -> dict:
    """The stored route of a session built on ``provider``/``model`` from the routing block ``routing_cfg``
    (the block that owns its fallback policy). Named providers are re-resolved by name later; an anonymous
    endpoint additionally pins its digest."""
    from tools.delegate_tool_config import _NATIVE_SDK_PROVIDERS
    cfg = dict(routing_cfg or {})
    name = str(provider or "").strip().lower()
    if cfg.get("base_url") and str(cfg.get("provider") or "").lower() not in _NATIVE_SDK_PROVIDERS:
        route: dict[str, Any] = {"kind": "default", "endpoint_sha256": endpoint_fingerprint(base_url)}
    elif name not in {"", "auto", "custom"}:
        route = {"kind": "named", "provider": name}
    elif cfg.get("provider"):
        route = {"kind": "named", "provider": name, "endpoint_sha256": endpoint_fingerprint(base_url)}
    else:
        route = {"kind": "parent", "endpoint_sha256": endpoint_fingerprint(base_url)}
    route.setdefault("provider", name or None)
    route.update(v=ROUTE_VERSION, model=str(model or "").strip() or None)
    chain = stored_fallback_chain(cfg.get("fallback_providers")) if "fallback_providers" in cfg else None
    if chain is not None:
        route["fallback_providers"] = chain
    for key, value in (("reasoning_effort", reasoning_effort), ("difficulty", difficulty),
                       ("backend", backend), ("label", label), ("service_tier", service_tier)):
        if value not in (None, ""):
            route[key] = value if isinstance(value, str) else str(value)
    if cfg.get("on_fallback") in ON_FALLBACK:
        route["on_fallback"] = cfg["on_fallback"]
    return validate_route({k: v for k, v in route.items() if v is not None})


def _effort_text(value: Any) -> Optional[str]:
    """A child's reasoning config (``{"effort": ...}`` / ``{"enabled": False}``) as its stored level."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        if value.get("enabled") is False:
            return "none"
        effort = value.get("effort")
        return str(effort) if effort else None
    return None


def capture_agent_route(agent: Any, *, routing_cfg: Optional[Mapping] = None, report: Optional[Mapping] = None) -> dict:
    """:func:`capture_route` for a built agent (a delegate child): its live provider/model/endpoint and the
    reasoning level it actually runs with."""
    report = report or {}
    return capture_route(
        provider=getattr(agent, "requested_provider", None) or getattr(agent, "provider", None),
        model=getattr(agent, "model", None), base_url=getattr(agent, "base_url", None), routing_cfg=routing_cfg,
        reasoning_effort=_effort_text(getattr(agent, "reasoning_config", None)),
        difficulty=report.get("difficulty"), backend=report.get("backend"), label=report.get("label"),
        service_tier=getattr(agent, "service_tier", None))


def _rehydrate_chain(chain: Optional[list], config: Mapping[str, Any]) -> Optional[list]:
    """A stored chain as a live ``fallback_providers`` block: ad-hoc endpoints are matched back to the current
    configuration by digest (dropped, with a warning, when no configured entry matches any more)."""
    if chain is None or chain == []:
        return chain
    from hermes_cli.fallback_config import get_fallback_chain
    candidates = get_fallback_chain({"fallback_providers": config.get("fallback_providers")})
    try:
        from hermes_cli.config import load_config_readonly
        candidates += get_fallback_chain(load_config_readonly() or {})
    except Exception:
        logger.debug("profile fallback chain unreadable while resolving a stored route", exc_info=True)
    live = []
    for entry in chain:
        item = {"provider": entry["provider"], "model": entry["model"]}
        if entry.get("key_env"):
            item["key_env"] = entry["key_env"]
        digest = entry.get("endpoint_sha256")
        if digest:
            match = next((c for c in candidates if c.get("base_url") and c["provider"] == entry["provider"]
                          and _same_endpoint(c["base_url"], digest)), None)
            if match is None:
                logger.warning("stored fallback %s/%s: its endpoint is no longer configured; skipped",
                               entry["provider"], entry["model"])
                continue
            item["base_url"] = match["base_url"]
        live.append(item)
    return live


def _same_endpoint(base_url: Any, digest: str) -> bool:
    try:
        return endpoint_fingerprint(base_url) == digest
    except RouteError:
        return False


def _require_endpoint(route: Mapping[str, Any], base_url: Any) -> None:
    expected = route.get("endpoint_sha256")
    if not expected or not _same_endpoint(base_url, expected):
        raise RouteError(
            "The stored route's unnamed provider endpoint no longer matches its original endpoint. Restore that "
            "route or start a new session with a named provider; nothing was run.")


def resolve_route(route: Mapping[str, Any], parent_agent: Any, *, model: Optional[str] = None,
                  config: Optional[Mapping[str, Any]] = None) -> dict:
    """The live routing block (``credentials_cfg`` shape, consumed by ``route_for_config``) a stored route
    names now. Credentials come from the current provider configuration; the stored fallback chain and
    reasoning level are kept; a ``model`` override stays within the stored provider."""
    from tools.delegate_tool_config import _inherit_parent_endpoint, _load_config, _resolve_delegation_credentials
    route = validate_route(route)
    selected = route.get("model") if model is None else model
    if not isinstance(selected, str) or not selected.strip():
        raise RouteError("A resumed model must be a non-empty string.")
    cfg = dict(_load_config() if config is None else config)
    block: dict[str, Any] = {"model": selected.strip()}
    effort = route.get("reasoning_effort", cfg.get("reasoning_effort"))
    if effort not in (None, ""):
        block["reasoning_effort"] = effort
    chain = _rehydrate_chain(route.get("fallback_providers"), cfg)
    if chain is not None:
        block["fallback_providers"] = chain
    elif cfg.get("fallback_providers") is not None:
        block["fallback_providers"] = cfg["fallback_providers"]
    if route.get("service_tier"):
        block["service_tier"] = route["service_tier"]
    kind, provider = route["kind"], str(route.get("provider") or "").strip().lower()
    if kind == "parent":
        current = str(getattr(parent_agent, "requested_provider", None) or getattr(parent_agent, "provider", "")
                      ).strip().lower()
        if provider and current != provider:
            raise RouteError("The stored route's inherited provider has changed; restore the original "
                             "provider or start a new session.")
        endpoint, _key = _inherit_parent_endpoint(parent_agent, getattr(parent_agent, "base_url", None),
                                                  getattr(parent_agent, "api_key", None))
        _require_endpoint(route, endpoint)
        return block
    if kind == "named":
        block["provider"] = provider
    else:  # default: the current delegation default, verified to be the same endpoint
        block = {**{k: v for k, v in cfg.items() if k not in ("model", "reasoning_effort", "fallback_providers")},
                 **block}
    if kind != "named" or route.get("endpoint_sha256"):
        credentials = _resolve_delegation_credentials(block, parent_agent)
        _require_endpoint(route, credentials.get("base_url"))
        if provider and str(credentials.get("provider") or "").strip().lower() != provider:
            raise RouteError("The stored route's provider has changed on its saved endpoint; restore the original "
                             "route or start a new session.")
    return block
