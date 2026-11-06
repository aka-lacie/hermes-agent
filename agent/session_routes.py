"""Stored session routes (unified-session plan §4.3): resolved once, kept credential-free, re-resolved anew.

``sessions.route`` is the JSON this module writes, for every producer (the one route module: delegate
spawn/resume, webhook re-delivery, cron re-fire and failover all read it here). It names how a session
reaches its model, never with what secret: a provider name, the model, the reasoning level the route chose
(a difficulty route's effort survives resume), the difficulty/backend labels, the endpoint as a SHA-256
digest when the route has no stable provider name, and the fallback chain as at most eight
``{provider, model[, key_env, endpoint_sha256]}`` entries (``key_env`` names a variable, never a value; an
ad-hoc entry endpoint is kept only as its digest).

Route kinds:

- ``named`` / ``default`` / ``parent``: a delegate child's route (:func:`capture_route` /
  :func:`capture_agent_route`), turned back into a live routing block by :func:`resolve_route`, which
  rehydrates credentials from the current provider configuration and refuses when an anonymous
  endpoint's digest no longer matches;
- ``declared``: what a webhook route or cron job declares for itself (:func:`declared_route`): optional
  ``provider`` / ``model`` / endpoint digest / ``reasoning_effort`` / ``fallback_providers``. The owner's
  runtime resolves the primary; :func:`route_fallback_chain` gives the chain it may walk.

``fallback_providers`` present means *declared* (``[]`` = fallback explicitly disabled); absent means the
owner declared nothing and the shared pin rule applies (``scoped_fallback_chain``: a pinned owner never
borrows the profile chain, an unpinned one inherits it). :func:`rehydrate_chain` is the one place a stored
chain becomes live again: endpoint digests are matched back to the owner's current configuration (dropped,
with a warning, when nothing matches) and credential references are re-attached from it. The chain rides the
stored route, so a failover chain survives resume (no more ``fallback_providers=[]`` forcing).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

ROUTE_VERSION = 1
FALLBACK_MAX = 8
MAX_FALLBACK_ENTRIES = FALLBACK_MAX
ROUTE_KINDS = frozenset({"named", "default", "parent", "declared"})
_ABSENT = object()
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


_CREDENTIAL_REFS = ("api_key", "key_env", "api_key_env")


def rehydrate_chain(chain: Optional[list], current: Iterable[Mapping[str, Any]] = ()) -> Optional[list]:
    """A stored chain as a live ``fallback_providers`` block, against the owner's *current* entries.

    An entry with an endpoint digest takes the base URL of the current entry of the same provider whose
    endpoint has that digest (none left: dropped, with a warning: never a guessed endpoint). Every entry
    re-attaches the credential reference (``api_key`` / ``key_env``) of the current entry naming the same
    provider, model and endpoint; an entry the config no longer names keeps its credential-free fields (the
    provider's own credential resolution applies). ``None`` / ``[]`` pass through."""
    if not chain:
        return chain
    from hermes_cli.fallback_config import _normalize_provider_id, get_fallback_chain
    candidates = get_fallback_chain({"fallback_providers": list(current or ())})

    def provider_of(entry):
        return _normalize_provider_id(str(entry.get("provider") or "").strip())
    live = []
    for entry in chain:
        item = {"provider": entry["provider"], "model": entry["model"]}
        if entry.get("key_env"):
            item["key_env"] = entry["key_env"]
        digest = entry.get("endpoint_sha256")
        same = [c for c in candidates if provider_of(c) == provider_of(entry)]
        if digest:
            at_endpoint = [c for c in same if c.get("base_url") and _same_endpoint(c["base_url"], digest)]
            if not at_endpoint:
                logger.warning("stored fallback %s/%s: its endpoint is no longer configured; skipped",
                               entry["provider"], entry["model"])
                continue
            item["base_url"] = at_endpoint[0]["base_url"]
            same = at_endpoint
        else:
            same = [c for c in same if not c.get("base_url")]
        match = next((c for c in same if str(c.get("model") or "").strip().lower()
                      == str(entry["model"]).strip().lower()), None)
        for key in _CREDENTIAL_REFS:
            if match is not None and match.get(key):
                item[key] = match[key]
        live.append(item)
    return live


def _rehydrate_chain(chain: Optional[list], config: Mapping[str, Any]) -> Optional[list]:
    """:func:`rehydrate_chain` for a delegate route: against the delegation block and the profile chain."""
    if not chain:
        return chain
    from hermes_cli.fallback_config import get_fallback_chain
    current = get_fallback_chain({"fallback_providers": config.get("fallback_providers")})
    try:
        from hermes_cli.config import load_config_readonly
        current += get_fallback_chain(load_config_readonly() or {})
    except Exception:
        logger.debug("profile fallback chain unreadable while resolving a stored route", exc_info=True)
    return rehydrate_chain(chain, current)


# ── owner-declared routes (webhook routes, cron jobs) ────────────────────────

def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def credential_free_chain(declared: Any) -> list[dict]:
    """*declared* normalized like ``fallback_providers`` (malformed entries dropped) in its stored form."""
    return stored_fallback_chain(declared) or []


def validate_declared_chain(declared: Any, owner: str) -> None:
    """Config-time check of a declared ``fallback_providers``: a list of ``{provider, model}`` mappings
    (``[]`` disables fallback). Raises ValueError naming *owner*."""
    if declared is None:
        return
    if not isinstance(declared, list) or not all(
            isinstance(e, dict) and _text(e.get("provider")) and _text(e.get("model")) for e in declared):
        raise ValueError(f"{owner} fallback_providers must be a list of {{provider, model}} entries")


def _owner_endpoint_digest(base_url: str) -> str:
    try:
        return endpoint_fingerprint(base_url)
    except RouteError:  # a scheme-less owner URL still pins *something*: its literal text
        return hashlib.sha256(base_url.rstrip("/").encode()).hexdigest()


def declared_route(*, provider: Any = None, model: Any = None, base_url: Any = None,
                   reasoning_effort: Any = None, fallback_providers: Any = _ABSENT) -> Optional[dict]:
    """The stored ``declared`` route an owner (webhook route, cron job) declares, or None when it declares
    nothing (the owner then follows the profile default and its chain, exactly as before)."""
    route: dict[str, Any] = {}
    for key, value in (("provider", provider), ("model", model), ("reasoning_effort", reasoning_effort)):
        if text := _text(value):
            route[key] = text
    if url := _text(base_url):
        route["endpoint_sha256"] = _owner_endpoint_digest(url)
    if fallback_providers is not _ABSENT and fallback_providers is not None:
        route["fallback_providers"] = [] if fallback_providers == [] else credential_free_chain(fallback_providers)
    if not route:
        return None
    return validate_route({"v": ROUTE_VERSION, "kind": "declared", **route})


def route_pinned(route: Optional[Mapping[str, Any]]) -> bool:
    """True when the route names its own provider, model or endpoint."""
    return bool(route) and any(route.get(k) for k in ("provider", "model", "endpoint_sha256"))


def route_fallback_chain(route: Optional[Mapping[str, Any]], inherited: Optional[list], *, owner: str,
                         current: Iterable[Mapping[str, Any]] = ()) -> Optional[list]:
    """The chain an owner's agent on *route* may walk: the route's declared chain (rehydrated against the
    owner's *current* entries), else the shared pin rule over *inherited* (the profile chain). ``None`` =
    no fallback."""
    from hermes_cli.fallback_config import scoped_fallback_chain
    declared = route.get("fallback_providers") if route else None
    live = rehydrate_chain(declared, current) if declared else declared
    if declared and not live:
        return None  # every declared entry lost its endpoint: never borrow another chain instead
    return scoped_fallback_chain(inherited, live, pinned=route_pinned(route), owner=owner)


def encode_route(route: Optional[Mapping[str, Any]]) -> Optional[str]:
    return None if not route else json.dumps(dict(route), sort_keys=True, separators=(",", ":"))


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
    if kind == "declared":  # an owner's own declaration: its runtime resolves the provider by name
        if provider:
            block["provider"] = provider
        return block
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
