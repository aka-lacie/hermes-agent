"""Logical session selectors: name a conversation, not one of its session ids (unified-session plan §4.1, §4.4).

``/new``, a session rollover and compression give the same Discord DM or Bot Chat a new ``session_id``; an
opt-in ``delivery_target`` or a ``delegate_task`` lookup that must keep finding "that chat" names it with a
selector instead. :func:`resolve_session_selector` turns one into the session currently holding it, at the
moment of use (delivery time, list/observe/resume time), never at configuration time.

Grammar (first match wins):

- ``session:<id>``: a literal session id (its compression tip, when it was compressed);
- ``title:<title>``: the session with that exact title (``title:Bot Chat`` is the Bot Mode canonical chat,
  resolved the same way every Bot Chat transport resolves it: exact title, then its compression tip);
- ``home:<platform>``: the conversation in the profile's configured home channel for that platform
  (``gateway.platforms.<platform>.home_channel``), found by ``source`` + ``chat_id`` (+ ``thread_id``);
- ``webhook:<route>:<key>``: the keyed webhook conversation for that route key (its keyed chat id,
  ``webhook_session_keys.keyed_chat_id``);
- ``agent:<namespace>:...``: a full gateway session key (``sessions.session_key``);
- ``<platform>:<rest>`` (anything else with a colon): a gateway session key in this profile's namespace,
  e.g. ``discord:dm:1443211608653430826`` -> ``agent:<profile>:discord:dm:1443211608653430826``;
- a bare alias (no colon): looked up in the alias table, then resolved once more (aliases never chain).

The alias table is ``sessions.selector_aliases`` in the profile config (alias -> selector) over the defaults:
``bot-chat`` -> ``title:Bot Chat``; any bare platform name (``discord``, ``telegram``, ...) ->
``home:<platform>``. A bare word that is neither resolves to nothing.

A key resolves through the gateway's own machinery: the ``gateway_routing`` entry for that key (the session
the gateway will run the chat's next message in), else the newest row carrying that ``session_key``; either
way the compression tip of that row (a compressed conversation delivers into its continuation).

Nothing matches -> ``None``: callers never guess a destination (the dispatcher reports
``selector_unresolved`` and the producing session keeps its output).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

ALIASES_CONFIG_SECTION = "sessions"
ALIASES_CONFIG_KEY = "selector_aliases"
DEFAULT_ALIASES = {"bot-chat": "title:Bot Chat"}
SCHEMES = ("session", "title", "home", "webhook", "agent")
MAX_SELECTOR_CHARS = 512
_ALIAS = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_PLATFORM = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class SelectorError(ValueError):
    """A selector of unknown shape (refused when a target is configured, never at delivery time)."""


def validate_selector(selector: Any) -> str:
    """The selector as stored (stripped); raises :class:`SelectorError` for a malformed one."""
    if not isinstance(selector, str) or not selector.strip() or len(selector) > MAX_SELECTOR_CHARS:
        raise SelectorError("a session selector is a non-empty string")
    value = selector.strip()
    if ":" not in value:
        if not _ALIAS.fullmatch(value.lower()):
            raise SelectorError(f"unknown session selector alias {value!r}")
        return value.lower()
    scheme, rest = value.split(":", 1)
    if not scheme or not rest.strip():
        raise SelectorError(f"session selector {value!r} names nothing after its scheme")
    if scheme == "webhook" and ":" not in rest:
        raise SelectorError("a webhook selector is webhook:<route>:<key>")
    return value


def is_selector(value: Any) -> bool:
    try:
        validate_selector(value)
    except SelectorError:
        return False
    return True


# ── configuration ────────────────────────────────────────────────────────────

def _profile_config() -> Mapping[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly() or {}
    except Exception:  # health: allow BLE001 -- an unreadable config only loses the alias overrides
        logger.debug("profile config unreadable while resolving a session selector", exc_info=True)
        return {}


def selector_aliases(config: Optional[Mapping[str, Any]] = None) -> dict[str, str]:
    """The alias table: the defaults with ``sessions.selector_aliases`` over them (malformed entries ignored)."""
    table = dict(DEFAULT_ALIASES)
    section = (config if config is not None else _profile_config()).get(ALIASES_CONFIG_SECTION) or {}
    configured = section.get(ALIASES_CONFIG_KEY) if isinstance(section, Mapping) else None
    for alias, target in (configured.items() if isinstance(configured, Mapping) else ()):
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias.strip().lower()):
            continue
        try:
            table[alias.strip().lower()] = validate_selector(target)
        except SelectorError:
            logger.warning("sessions.selector_aliases.%s is not a selector; ignored", alias)
    return table


def _home_channel(platform: str, config: Optional[Mapping[str, Any]]) -> Optional[tuple[str, Optional[str]]]:
    """``(chat_id, thread_id)`` of the profile's home channel on *platform*, or None."""
    homes = (config or {}).get("home_channels") if config is not None else None
    if isinstance(homes, Mapping):  # an explicit table (tests, embedders)
        home = homes.get(platform)
        if isinstance(home, Mapping) and home.get("chat_id"):
            return str(home["chat_id"]), (str(home["thread_id"]) if home.get("thread_id") else None)
        return None
    try:
        from gateway.config import Platform, load_gateway_config
        home = load_gateway_config().get_home_channel(Platform(platform))
    except Exception:  # health: allow BLE001 -- no gateway config: the alias names nothing
        logger.debug("home channel for %s unavailable", platform, exc_info=True)
        return None
    return (str(home.chat_id), home.thread_id) if home is not None and home.chat_id else None


# ── resolution ───────────────────────────────────────────────────────────────

def _namespace(db: Any, profile: Optional[str]) -> str:
    from gateway.session import _session_key_namespace
    if profile is None:
        try:
            profile = db._own_profile_name()
        except Exception:  # health: allow BLE001 -- a store outside the profile tree: the default namespace
            profile = None
    return _session_key_namespace(profile)


def _tip(db: Any, session_id: Optional[str]) -> Optional[str]:
    if not session_id or db._read_one("SELECT 1 FROM sessions WHERE id = ?", (session_id,)) is None:
        return None
    return db.get_compression_tip(session_id) or session_id


def current_session_for_key(db: Any, session_key: str) -> Optional[str]:
    """The session the conversation keyed *session_key* runs in now: the gateway routing entry's session
    (when its row exists), else the newest row carrying the key (open rows first); its compression tip."""
    if not session_key:
        return None
    routed = db._read_all("SELECT entry_json FROM gateway_routing WHERE session_key = ? ORDER BY updated_at DESC",
                          (session_key,))
    for row in routed:
        try:
            entry = json.loads(row["entry_json"] or "{}")
        except (TypeError, ValueError, RecursionError):
            continue
        if isinstance(entry, dict) and (tip := _tip(db, entry.get("session_id"))):
            return tip
    row = db._read_one(
        "SELECT id FROM sessions WHERE session_key = ? ORDER BY (ended_at IS NULL) DESC, "
        "COALESCE(last_activity_at, started_at) DESC, started_at DESC, id DESC LIMIT 1", (session_key,))
    return _tip(db, row["id"]) if row is not None else None


def session_key_for_selector(selector: str, *, namespace: str) -> Optional[str]:
    """The gateway session key a key-shaped selector names (None for the other schemes)."""
    scheme = selector.split(":", 1)[0]
    if ":" not in selector or scheme in SCHEMES[:-1]:
        return None
    return selector if scheme == "agent" else f"{namespace}:{selector}"


def _webhook_session(db: Any, rest: str, profile: Optional[str]) -> Optional[str]:
    from gateway.platforms.webhook_session_keys import keyed_chat_id
    route, key = rest.split(":", 1)
    # The keyed chat id carries the delivery's ``/p/<profile>/`` prefix profile (``None`` without one): try
    # the named profile, the unprefixed spelling and the store's own profile.
    try:
        own = db._own_profile_name()
    except Exception:  # health: allow BLE001 -- a store outside the profile tree
        own = None
    chats = list(dict.fromkeys(keyed_chat_id(p, route, key) for p in (profile, None, own)))
    row = db._read_one(
        f"SELECT id, session_key FROM sessions WHERE source = 'webhook' AND chat_id IN ({','.join('?' * len(chats))}) "
        "ORDER BY started_at DESC, id DESC LIMIT 1", chats)
    if row is None:
        return None
    return (current_session_for_key(db, row["session_key"]) if row["session_key"] else None) or _tip(db, row["id"])


def _home_session(db: Any, platform: str, config: Optional[Mapping[str, Any]]) -> Optional[str]:
    home = _home_channel(platform, config)
    if home is None:
        return None
    chat_id, thread_id = home
    row = db._read_one(
        "SELECT id, session_key FROM sessions WHERE source = ? AND chat_id = ? AND COALESCE(thread_id, '') = ? "
        "ORDER BY (ended_at IS NULL) DESC, COALESCE(last_activity_at, started_at) DESC, id DESC LIMIT 1",
        (platform, chat_id, thread_id or ""))
    if row is None:
        return None
    return (current_session_for_key(db, row["session_key"]) if row["session_key"] else None) or _tip(db, row["id"])


def resolve_session_selector(db: Any, selector: str, *, profile: Optional[str] = None,
                             config: Optional[Mapping[str, Any]] = None, _alias_depth: int = 0) -> Optional[str]:
    """The session id *selector* names right now, or None. *profile* picks the session-key namespace (default:
    the store's own profile); *config* overrides the profile config (``sessions.selector_aliases``, and an
    optional ``home_channels: {platform: {chat_id, thread_id}}`` table instead of the gateway config)."""
    try:
        value = validate_selector(selector)
    except SelectorError:
        logger.warning("unresolvable session selector %r", selector)
        return None
    if ":" not in value:
        if _alias_depth:
            return None  # aliases never chain
        target = selector_aliases(config).get(value)
        if target is None and _PLATFORM.fullmatch(value):
            target = f"home:{value}"
        return None if target is None else resolve_session_selector(
            db, target, profile=profile, config=config, _alias_depth=1)
    scheme, rest = value.split(":", 1)
    if scheme == "session":
        return _tip(db, rest.strip())
    if scheme == "title":
        sid = db.resolve_session_by_title(rest.strip())
        return _tip(db, sid)
    if scheme == "home":
        return _home_session(db, rest.strip().lower(), config)
    if scheme == "webhook":
        return _webhook_session(db, rest, profile)
    key = session_key_for_selector(value, namespace=_namespace(db, profile))
    return current_session_for_key(db, key) if key else None
