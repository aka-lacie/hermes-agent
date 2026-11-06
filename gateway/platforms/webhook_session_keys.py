"""Keyed webhook sessions: follow-up deliveries resume one conversation (plan §4.5, §6 Q6).

A delivery's session key comes from, in order (first that answers wins):

1. the route ``script`` (the gate that already parses and filters the payload): the object it
   prints carries the reserved top-level field ``__hermes_session_key__``. A string resumes that key;
   ``null`` / ``""`` explicitly asks for a per-delivery session. The field is stripped from every
   inbound payload BEFORE the script runs, so only the gate can set it, and removed from what the
   prompt renders;
2. the route's declarative ``session_key``: a ``{dot.path}`` template, or a list tried in order
   (``"{repository.full_name}#{pull_request.number}"``). A template with an unresolved/empty
   placeholder is skipped;
3. plugins: the ``resolve_session_key`` hook (``ctx.register_hook``), or the route-scoped
   ``ctx.register_webhook_session_key(route_name, fn)`` (hermes_cli/plugins_webhooks.py).

Nothing answers -> ``None``: a per-delivery session, finalized after its turn exactly as before.

Hook contract (``resolve_session_key``), fired once per agent-run admission (immediate or coalesced;
never for ``deliver_only`` / ``cron_job`` routes, which start no webhook session):

    kwargs: route_name, route (copy of the route config), payload (after the route script, reserved
            field removed), headers (dict copy), event_type, delivery_id, profile
    return: None -> no opinion (next resolver / per-delivery session)
            "key" or {"key": "key"} -> resume (or start and bind) the session for this key

A raising, slow (``plugins.hook_callback_timeout``) or malformed answer is skipped: keying improves
context, it never gates acceptance. "Start fresh" is the resolver's call: return a new key (e.g. a
generation suffix).

Every delivery sharing ``(profile, route, key)`` shares one chat id (:func:`keyed_chat_id`), hence one
SessionStore routing entry, one ``sessions`` row and one admission FIFO: concurrent deliveries for one
key serialize on that session's runner, and the conversation compacts like any other. The chat id
spelling is dev2's (``feat/webhook-session-keys``), so a migrated keyed routing entry keeps resolving.
The session row records ``lineage_kind = "webhook"``; the keyed conversation is addressable by the logical
selector ``webhook:<route>:<key>`` (:func:`session_selector`, resolved through the keyed chat id by
``gateway.session_selectors``: no parallel key column). Keyed sessions are never closed by the one-shot
finalizer, the retry finalizer or startup recovery.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from copy import deepcopy
from typing import Any, Optional

logger = logging.getLogger(__name__)

HOOK = "resolve_session_key"
# Reserved script-output field (same spelling family as ``__hermes_ignore__``).
SCRIPT_FIELD = "__hermes_session_key__"
NO_KEY = object()  # the script said nothing about the key
# The 4th tuple member marks a keyed chat id; per-delivery ids are 3-tuples (``_WebhookDeliveryIdentity``).
_KEYED_TAG = "session"
_MAX_KEY_CHARS = 1024
_PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_.]+)\}")
_CHAT_PREFIX = "webhook:v2:"


def keyed_chat_id(profile: Optional[str], route: str, key: str) -> str:
    """The chat id every delivery with this ``(profile, route, key)`` shares. Same ``webhook:v2:``
    spelling as a per-delivery id (``route_from_chat_id`` reads it); the key is hashed so provider
    identifiers stay out of routing keys and logs."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    payload = json.dumps((profile or "default", route, digest, _KEYED_TAG), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return _CHAT_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def is_keyed_chat_id(chat_id: Any) -> bool:
    """True for a chat id minted by :func:`keyed_chat_id` (never a per-delivery or legacy id)."""
    if not isinstance(chat_id, str) or not chat_id.startswith(_CHAT_PREFIX):
        return False
    token = chat_id[len(_CHAT_PREFIX):]
    try:
        parts = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, TypeError):
        return False
    return isinstance(parts, list) and len(parts) == 4 and parts[3] == _KEYED_TAG


def session_selector(route: str, key: str) -> str:
    """The logical selector of a keyed conversation: ``webhook:<route>:<key>`` (gateway.session_selectors)."""
    return f"webhook:{route}:{key}"


def coerce_key(answer: Any) -> Optional[str]:
    if isinstance(answer, dict):
        answer = answer.get("key")
    if not isinstance(answer, str):
        return None
    key = answer.strip()
    return key if key and len(key) <= _MAX_KEY_CHARS else None


def strip_reserved(payload: Any) -> Any:
    """Inbound payload without the reserved field (only the route script may set it)."""
    if isinstance(payload, dict) and SCRIPT_FIELD in payload:
        return {k: v for k, v in payload.items() if k != SCRIPT_FIELD}
    return payload


def split_script_key(payload: Any) -> tuple[Any, Any]:
    """``(payload without the reserved field, the script's answer or NO_KEY)``."""
    if not isinstance(payload, dict) or SCRIPT_FIELD not in payload:
        return payload, NO_KEY
    return strip_reserved(payload), payload[SCRIPT_FIELD]


def _templates(route: dict) -> list:
    value = route.get("session_key")
    return [value] if isinstance(value, str) else value if isinstance(value, list) else []


def validate_route_session_options(name: str, route: dict) -> None:
    """Startup / dynamic-route validation of ``session_key``, ``model`` / ``provider``,
    ``fallback_providers`` and ``delivery_target``. Raises ValueError."""
    from agent.session_routes import validate_declared_chain
    value = route.get("session_key")
    if value is not None and (not _templates(route) or any(
            not isinstance(t, str) or not _PLACEHOLDER.search(t) for t in _templates(route))):
        raise ValueError(f"[webhook] Route '{name}' session_key must be a template string with at least one "
                         "{placeholder}, or a non-empty list of them")
    agentless = route.get("deliver_only") or route.get("cron_job")
    declared = [k for k in ("session_key", "model", "provider", "fallback_providers", "delivery_target")
                if route.get(k) is not None]
    if agentless and declared:
        raise ValueError(f"[webhook] Route '{name}' sets {', '.join(declared)}, which need an agent session; "
                         "not deliver_only or cron_job")
    for key in ("model", "provider"):
        if route.get(key) is not None and not (isinstance(route[key], str) and route[key].strip()):
            raise ValueError(f"[webhook] Route '{name}' {key} must be a non-empty string")
    validate_declared_chain(route.get("fallback_providers"), f"[webhook] Route '{name}'")
    from gateway.platforms.webhook_session_targets import validate_delivery_target, route_delivery_target
    validate_delivery_target(name, route.get("delivery_target"))
    # Template addresses are rendered and validated again before admission. At startup only
    # the platform/mode contract can be checked without a provider payload.
    route_delivery_target({**route, "deliver_extra": {}})


def _resolve_path(payload: Any, path: str) -> Optional[str]:
    value = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    if isinstance(value, (bool, dict, list)) or value is None:
        return None
    return str(value).strip() or None


def route_template_key(route_config: dict, payload: Any) -> Optional[str]:
    """The first ``session_key`` template whose placeholders all resolve to scalar payload values."""
    for template in _templates(route_config):
        if not isinstance(template, str):
            continue
        values = [_resolve_path(payload, m.group(1)) for m in _PLACEHOLDER.finditer(template)]
        if values and all(v is not None for v in values):
            filled = iter([str(v) for v in values])
            return coerce_key(_PLACEHOLDER.sub(lambda _m: next(filled), template))
    return None


async def plugin_session_key(*, route_name: str, route_config: dict, payload: Any, headers: Any,
                             event_type: str, delivery_id: str, profile: Optional[str]) -> Optional[str]:
    """The first usable ``resolve_session_key`` answer, else None."""
    try:
        from hermes_cli.lifecycle import ainvoke_hook, has_hook
        if not has_hook(HOOK):
            return None
        results = await ainvoke_hook(HOOK, route_name=route_name, route=deepcopy(route_config),
                                     payload=deepcopy(payload), headers=dict(headers or {}),
                                     event_type=event_type, delivery_id=delivery_id, profile=profile)
    except Exception:
        logger.warning("[webhook] %s hook failed for route %s; using a per-delivery session", HOOK, route_name,
                       exc_info=True)
        return None
    for answer in results or ():
        if (key := coerce_key(answer)) is not None:
            return key
        logger.warning("[webhook] Ignoring malformed %s answer for route %s: %s", HOOK, route_name,
                       type(answer).__name__)
    return None


async def resolve_delivery_key(*, route_name: str, route_config: dict, payload: Any, event_type: str,
                               delivery_id: str, profile: Optional[str], headers: Any = None,
                               script_answer: Any = NO_KEY) -> Optional[str]:
    """Script directive, then the route's template, then plugins (see the module docstring)."""
    if script_answer is not NO_KEY:
        return coerce_key(script_answer)
    try:
        key = route_template_key(route_config, payload)
    except Exception:
        logger.warning("[webhook] session_key template failed for route %s", route_name, exc_info=True)
        key = None
    if key is not None:
        return key
    return await plugin_session_key(route_name=route_name, route_config=route_config, payload=payload,
                                    headers=headers, event_type=event_type, delivery_id=delivery_id,
                                    profile=profile)


def stamp_admitted_session(authority: Any, receipt: Any, *, route_name: str, route_config: dict,
                           key: Optional[str]) -> None:
    """Set-once unified-session metadata of the session a delivery was admitted into (the receipt's
    logical session): lineage, the route's credential-free declared route and its delivery_target; an
    opted-in target (``attach: true``) is bound to the session it holds now (``delivery_targets.bind_target``).
    Best-effort: a failed stamp is logged, never fails the delivery."""
    from gateway.platforms.webhook_route_runtime import declared_route_for
    from gateway.platforms.webhook_session_targets import route_delivery_target
    from gateway.delivery_targets import bind_target
    from hermes_state_session_meta import stamp_session_meta
    db, session_id = getattr(authority, "db", None), getattr(getattr(receipt, "ref", None), "session_id", None)
    if db is None or not isinstance(session_id, str):
        return
    try:
        admission = db._read_one("SELECT payload_json FROM session_admissions WHERE admission_id=?", (receipt.admission_id,))
        import json
        native = json.loads(admission['payload_json']).get('native_text_v1', {}) if admission else {}
        selected = (native.get('webhook_delivery') or {}).get('worker_route')
        # Legacy admissions need the caller's explicit migration/backfill selection; new
        # admissions already froze it before the commit, under their owning profile scope.
        if selected is None:
            from gateway.run import _profile_runtime_scope
            with _profile_runtime_scope(db.db_path.parent):
                selected = declared_route_for(route_config)
        stamp_session_meta(db, session_id, lineage_kind="webhook", route=selected,
                           delivery_target=route_delivery_target(route_config))
        bind_target(db, session_id)
    except Exception:
        logger.warning("[webhook] Could not stamp session %s for route %s", session_id, route_name, exc_info=True)
