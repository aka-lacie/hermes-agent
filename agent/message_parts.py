"""Typed semantic message parts: the in-memory transport for message provenance.

A user-role message dict may carry ``msg[MESSAGE_PARTS]``: an ordered list of JSON-shaped part dicts,
``{"type": ..., "v": 1, ...}``. JSON-shaped (never objects) because messages are deep-copied,
structurally cloned for the send path, JSON-spooled at shutdown, validated across forks and passed
through hook payload sanitizers; plain dicts survive all of them unchanged. The underscore keeps the
key off every provider wire even on a transport that forgets it (``PERSISTENCE_ONLY_MESSAGE_FIELDS``
pops it from each outgoing copy as well).

Part shapes (every key is always present; ``v`` is :data:`PARTS_VERSION`):

``human``         ``{text, delivery: "turn" | "steer", origin: {platform?, message_id?, channel_id?, sent_at?}}``
``system_event``  ``{kind, text, meta}``  meta: ids, status, pointers (JSON object); never instructions
``injection``     ``{kind, text}``        harness-owned per-turn context (a ``pre_llm_call`` result)
``media``         ``{sha256, mime, path}``

``origin.sent_at`` and the ``sent_at`` row field are epoch seconds (float), the same clock as
``timestamp``; renderers format them.

**Invariants.** The parts are the truth and ``content`` is their derived plain view
(:func:`plain_text`). Only the helpers here edit parts (:func:`set_parts`, :func:`append_parts`,
:func:`merge`); each regenerates ``content`` and drops the stale ``api_content`` sidecar. Parts are
built from structured fields only (:func:`build_parts`), never by parsing text: a legacy row without
typing stays untyped until the one-shot lineage migration types it.

Row-level provenance travels beside the parts as plain keys named like their ``messages`` columns
(:data:`PROVENANCE_FIELDS`); they are persistence-only and never reach a provider.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from typing import Any, Iterable, Mapping, MutableMapping, Optional

logger = logging.getLogger(__name__)

MESSAGE_PARTS = "_parts"
PARTS_VERSION = 1

HUMAN = "human"
SYSTEM_EVENT = "system_event"
INJECTION = "injection"
MEDIA = "media"
PART_TYPES = frozenset({HUMAN, SYSTEM_EVENT, INJECTION, MEDIA})
# Parts whose ``text`` makes up the plain view, in message order. Media contributes nothing: its bytes
# (or a one-line reference outside the replay window) are the renderer's business.
_TEXT_PART_TYPES = frozenset({HUMAN, SYSTEM_EVENT, INJECTION})
_PART_KEYS = {
    HUMAN: frozenset({"type", "v", "text", "delivery", "origin"}),
    SYSTEM_EVENT: frozenset({"type", "v", "kind", "text", "meta"}),
    INJECTION: frozenset({"type", "v", "kind", "text"}),
    MEDIA: frozenset({"type", "v", "sha256", "mime", "path"}),
}
DELIVERIES = ("turn", "steer")
ORIGIN_KEYS = ("platform", "message_id", "channel_id", "sent_at")
# The joint between consecutive text parts, and so between merged turns: the same "\n\n" the
# consecutive-user merge has always used, so a typed merge produces the legacy merged text.
PLAIN_TEXT_SEPARATOR = "\n\n"

SENDER_KINDS = frozenset({
    "human", "parent", "child", "peer", "webhook", "cron", "system", "automation", "tool_result"})
TURN_OUTCOMES = frozenset({"completed", "failed", "partial", "interrupted"})
# Message-dict keys mirroring the ``messages`` provenance columns (``_parts`` <-> ``parts_json``).
PROVENANCE_FIELDS = ("sender_kind", "sender_id", "sent_at", "source_event_id", "intent", "part_kind", "turn_outcome")

# Rule 1 (assistant purity): who produced an assistant dict. SessionDB's append gate admits a new
# assistant row only with one of these (``hermes_state_provenance``).
ASSISTANT_ORIGIN = "_assistant_origin"
MODEL_ORIGIN = "model"  # this session's model generated the tokens in one of this session's turns
COPY_ORIGIN = "copy"  # re-issue of rows already durable elsewhere (branch seeds, spool replay)
# TODO(2e): harness rows that exist only to keep strict user/assistant alternation (failed-turn
# notices, interrupt placeholder, guardrail halt, preflight errors, compaction summary role flip,
# micro-compaction marker, codex event projection, pre_llm_call veto reply). The wire renderer owns
# alternation after 2c; 2e deletes every producer of this origin and then the origin itself.
HARNESS_LEGACY_ORIGIN = "harness_legacy"
ASSISTANT_ORIGINS = frozenset({MODEL_ORIGIN, COPY_ORIGIN, HARNESS_LEGACY_ORIGIN})


def harness_legacy_origin(role: Any) -> dict:
    """``{ASSISTANT_ORIGIN: HARNESS_LEGACY_ORIGIN}`` for a harness row built with a computed *role* that
    lands on ``assistant`` only to keep alternation, else ``{}``. TODO(2e): delete with its callers."""
    return {ASSISTANT_ORIGIN: HARNESS_LEGACY_ORIGIN} if role == "assistant" else {}

_DB_PERSISTED_MARKER = "_db_persisted"  # agent.context_compressor._DB_PERSISTED_MARKER (import-light here)


# ── constructors ──────────────────────────────────────────────────────────────


def human_message(text: str, *, delivery: str = "turn", origin: Optional[Mapping[str, Any]] = None) -> dict:
    """A human's own words. ``origin`` keeps only the known keys that carry a value."""
    part = {"type": HUMAN, "v": PARTS_VERSION, "text": text, "delivery": delivery,
            "origin": {k: origin[k] for k in ORIGIN_KEYS if origin and origin.get(k) is not None}}
    _validate_part(part)
    return part


def system_event(kind: str, text: str, *, meta: Optional[Mapping[str, Any]] = None) -> dict:
    """Something that happened (a cron result, a child completion, a mirrored send, a notice)."""
    part = {"type": SYSTEM_EVENT, "v": PARTS_VERSION, "kind": kind, "text": text, "meta": dict(meta or {})}
    _validate_part(part)
    return copy.deepcopy(part)


def turn_injection(kind: str, text: str) -> dict:
    """Harness-owned context that rides with the turn it was produced for."""
    part = {"type": INJECTION, "v": PARTS_VERSION, "kind": kind, "text": text}
    _validate_part(part)
    return part


def media_ref(sha256: str, mime: str, path: str) -> dict:
    """A stored attachment; the renderer attaches its bytes inside the replay window."""
    part = {"type": MEDIA, "v": PARTS_VERSION, "sha256": sha256, "mime": mime, "path": path}
    _validate_part(part)
    return part


def system_event_message(kind: str, text: str, *, sender_id: str, sender_kind: str = "system",
                         meta: Optional[Mapping[str, Any]] = None, sent_at: Optional[float] = None,
                         source_event_id: Optional[str] = None) -> dict:
    """A complete ``role="user"`` message dict holding one :func:`system_event` part: the row shape for
    any notice that is not this session's model speaking (§4.7 classes 2/3)."""
    if sender_kind not in SENDER_KINDS or sender_kind == "human":
        raise ValueError(f"system events need a non-human sender_kind, got {sender_kind!r}")
    if not isinstance(sender_id, str) or not sender_id:
        raise ValueError("system events need a sender_id")
    msg: dict = {"role": "user", "sender_kind": sender_kind, "sender_id": sender_id, "part_kind": kind}
    if sent_at is not None:
        msg["sent_at"] = float(sent_at)
    if source_event_id is not None:
        msg["source_event_id"] = str(source_event_id)
    set_parts(msg, [system_event(kind, text, meta=meta)])
    return msg


# ── validation and the plain view ─────────────────────────────────────────────


def _is_json_value(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _require_str(part: Mapping[str, Any], key: str, *, non_empty: bool = False) -> None:
    value = part.get(key)
    if not isinstance(value, str) or (non_empty and not value):
        raise ValueError(f"{part.get('type')} part needs a{' non-empty' if non_empty else ''} string {key!r}")


def _validate_origin(origin: Any) -> None:
    if not isinstance(origin, dict) or set(origin) - set(ORIGIN_KEYS):
        raise ValueError(f"human origin must be an object with keys from {ORIGIN_KEYS}")
    for key in ("platform", "message_id", "channel_id"):
        if key in origin and not isinstance(origin[key], str):
            raise ValueError(f"human origin {key!r} must be a string")
    sent_at = origin.get("sent_at")
    if "sent_at" in origin and (isinstance(sent_at, bool) or not isinstance(sent_at, (int, float))
                                or not math.isfinite(sent_at)):
        raise ValueError("human origin sent_at must be finite epoch seconds")


def _validate_part(part: Any) -> None:
    if not isinstance(part, dict):
        raise ValueError(f"a part must be a dict, got {type(part).__name__}")
    ptype = part.get("type")
    if ptype not in PART_TYPES:
        raise ValueError(f"unknown part type {ptype!r}")
    if part.get("v") != PARTS_VERSION:
        raise ValueError(f"unsupported {ptype} part version {part.get('v')!r}")
    if set(part) != _PART_KEYS[ptype]:
        raise ValueError(f"{ptype} part keys must be exactly {sorted(_PART_KEYS[ptype])}, got {sorted(part)}")
    if ptype == MEDIA:
        sha = part["sha256"]
        if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
            raise ValueError("media sha256 must be 64 lowercase hex characters")
        _require_str(part, "path", non_empty=True)
        if not (isinstance(part["mime"], str) and "/" in part["mime"]):
            raise ValueError("media mime must be a type/subtype string")
        return
    _require_str(part, "text")
    if ptype == HUMAN:
        if part["delivery"] not in DELIVERIES:
            raise ValueError(f"human delivery must be one of {DELIVERIES}")
        _validate_origin(part["origin"])
        return
    _require_str(part, "kind", non_empty=True)
    if ptype == SYSTEM_EVENT and not (isinstance(part["meta"], dict) and _is_json_value(part["meta"])):
        raise ValueError("system_event meta must be a JSON object")


def validate_parts(parts: Any) -> list:
    """*parts* itself when it is a well-formed part list; raises ``ValueError`` otherwise."""
    if not isinstance(parts, list):
        raise ValueError(f"parts must be a list, got {type(parts).__name__}")
    for part in parts:
        _validate_part(part)
    return parts


def plain_text(parts: Iterable[Mapping[str, Any]]) -> str:
    """The derived ``content`` view: the non-empty ``text`` of every human / system_event / injection
    part, in order, joined by :data:`PLAIN_TEXT_SEPARATOR`. Media parts contribute nothing."""
    return PLAIN_TEXT_SEPARATOR.join(
        p["text"] for p in parts if p.get("type") in _TEXT_PART_TYPES and p.get("text"))


def has_parts(msg: Any) -> bool:
    return isinstance(msg, Mapping) and isinstance(msg.get(MESSAGE_PARTS), list)


def parts_consistent(msg: Mapping[str, Any]) -> bool:
    """``content`` is still the plain view of the parts (multimodal list content is never compared)."""
    content = msg.get("content")
    return not isinstance(content, str) or content == plain_text(msg[MESSAGE_PARTS])


# ── mutation helpers (the only code that edits parts) ─────────────────────────


def _assign(msg: MutableMapping[str, Any], parts: list) -> bool:
    """Store a validated copy of *parts*, regenerate content, drop the stale sidecar; True if changed."""
    parts = copy.deepcopy(validate_parts(parts))
    content = plain_text(parts)
    changed = msg.get(MESSAGE_PARTS) != parts or msg.get("content") != content
    msg[MESSAGE_PARTS] = parts
    msg["content"] = content
    msg.pop("api_content", None)
    return changed


def set_parts(msg: MutableMapping[str, Any], parts: list) -> MutableMapping[str, Any]:
    """Replace *msg*'s parts. A durable dict whose parts changed loses its persisted stamp, so the
    next flush rewrites its row."""
    if msg.get("role") not in (None, "user"):
        raise ValueError("only user-role messages carry typed parts")
    if _assign(msg, parts):
        msg.pop(_DB_PERSISTED_MARKER, None)
    return msg


def _parts_or_legacy(msg: Mapping[str, Any]) -> Optional[list]:
    if has_parts(msg):
        return list(msg[MESSAGE_PARTS])
    return build_parts(msg, type_untyped=True)


def append_parts(msg: MutableMapping[str, Any], parts: list) -> MutableMapping[str, Any]:
    """Append *parts* after *msg*'s own. An untyped plain user message is typed as the human turn it
    has always been treated as; untyped multimodal content cannot be typed here and raises."""
    base = _parts_or_legacy(msg)
    if base is None:
        raise ValueError("cannot append typed parts to an untyped non-text message")
    return set_parts(msg, base + list(parts))


def merge(prev: MutableMapping[str, Any], msg: Mapping[str, Any]) -> bool:
    """Fold user message *msg* into *prev* by concatenating parts; True when it did.

    Returns False (nothing touched) when neither side is typed, so the caller's legacy text merge runs
    unchanged, or when an untyped side holds non-text content. An untyped plain side is typed as the
    human turn it was rendered as. *prev* keeps its row-level provenance; each part keeps its own type.
    The persisted stamp is the caller's to manage (the merge keeps its own durable-row bookkeeping).
    """
    if not (has_parts(prev) or has_parts(msg)):
        return False
    left, right = _parts_or_legacy(prev), _parts_or_legacy(msg)
    if left is None or right is None:
        return False
    _assign(prev, left + right)
    return True


# ── construction from structured fields, (de)serialisation, copies ───────────


def build_parts(source: Mapping[str, Any], *, type_untyped: bool = False) -> Optional[list]:
    """Parts for a live input or a stored row, from structured fields ONLY (never by parsing text).

    *source* is a message dict or a decoded ``messages`` row. Existing ``_parts`` / ``parts_json`` win.
    Otherwise a ``role="user"`` text row is typed from its provenance columns: ``sender_kind="human"``
    -> one human part (``delivery="steer"`` for a steer intent or display kind, origin from ``sent_at`` /
    ``source_event_id``); any other non-tool sender -> one system_event part of kind ``part_kind`` (else
    the sender kind). A legacy row without ``sender_kind`` stays untyped (None): role alone does not say
    who wrote it (cron and webhook mirrors are user rows too). ``type_untyped`` types such a row as what
    it has always been rendered as: a human turn (a steer for the steer display kind), or a system_event
    of its display kind (timeline markers, notifications). Only the helpers that must join it to typed
    parts use it; the lineage migration owns typing stored history.
    """
    if has_parts(source):
        return copy.deepcopy(validate_parts(source[MESSAGE_PARTS]))
    if source.get("parts_json") is not None:
        return decode_parts_json(source.get("parts_json"))
    if source.get("role") != "user" or not isinstance(source.get("content"), str):
        return None
    from agent.prompt_builder import STEER_DISPLAY_KIND
    text = source["content"]
    sender_kind = source.get("sender_kind")
    display_kind = source.get("display_kind")
    if sender_kind is None:
        if not type_untyped:
            return None
        if display_kind and display_kind != STEER_DISPLAY_KIND:
            return [system_event(display_kind, text)]
        return [human_message(text, delivery="steer" if display_kind else "turn")]
    if sender_kind == "human":
        steer = source.get("intent") == "steer" or display_kind == STEER_DISPLAY_KIND
        origin = {"sent_at": source.get("sent_at"), "message_id": source.get("source_event_id")}
        return [human_message(text, delivery="steer" if steer else "turn", origin=origin)]
    if sender_kind in SENDER_KINDS and sender_kind != "tool_result":
        return [system_event(source.get("part_kind") or sender_kind, text)]
    return None


def encode_parts_json(parts: Optional[list]) -> Optional[str]:
    """Canonical ``parts_json`` text (sorted keys, compact) or None."""
    if parts is None:
        return None
    return json.dumps(validate_parts(parts), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_parts_json(raw: Any) -> Optional[list]:
    """Stored ``parts_json`` -> part list; a malformed or future-version value reads as untyped."""
    if raw is None:
        return None
    try:
        return validate_parts(json.loads(raw))
    except (TypeError, ValueError) as exc:
        logger.warning("Ignoring unreadable parts_json on message row: %s", exc)
        return None


def typed_fields(msg: Mapping[str, Any]) -> dict:
    """The typing a copy of *msg* must carry: its parts (deep copy) and every provenance field that holds
    a value. Every allowlist copy that keeps ``message_uid`` keeps these too (key-preservation rule 1)."""
    fields = {key: msg[key] for key in PROVENANCE_FIELDS if msg.get(key) is not None}
    if has_parts(msg):
        fields[MESSAGE_PARTS] = copy.deepcopy(msg[MESSAGE_PARTS])
    return fields


def copy_row_fields(msg: Mapping[str, Any]) -> dict:
    """What a fresh row copied from *msg* into another session (a branch or seed copy) carries beyond its
    payload: its typing, and for an assistant row the Rule 1 ``COPY_ORIGIN`` (an earlier turn's model
    wrote it, not a new one)."""
    fields = typed_fields(msg)
    if msg.get("role") == "assistant":
        fields[ASSISTANT_ORIGIN] = COPY_ORIGIN
    return fields


def current_turn_inputs(messages: Any, index: Optional[int]) -> list:
    """The typed parts of the current turn's user message (``messages[index]``), copied: the read-only
    ``pre_llm_call(inputs=...)`` payload. Empty when the turn's message is untyped."""
    if not isinstance(messages, list) or not isinstance(index, int) or not 0 <= index < len(messages):
        return []
    msg = messages[index]
    return copy.deepcopy(msg[MESSAGE_PARTS]) if has_parts(msg) else []
