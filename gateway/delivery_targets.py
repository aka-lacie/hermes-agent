"""``sessions.delivery_target``: where a session's results go (unified-session plan §4.4, BRIEF3 contract).

The column holds one of

- ``{"kind": "session", "session_id": "<target>", "attach": true|false}`` (optional ``wake_policy``):
  results/completions are admitted into the target session as ``role="user"`` system-event rows
  (``sender_kind`` child / cron / webhook, ``sender_id`` this session). ``attach: true`` also lists this
  session in the target's default delegate list. Delegate children: ``session_id=parent, attach=true``.
- ``{"kind": "session", "selector": "<selector>", "attach": true|false}``: the same, but the target is a
  conversation, not one session id: ``discord:dm:<chat_id>``, ``discord`` (the profile's Discord home
  channel), ``bot-chat`` (the Bot Mode canonical chat), ``webhook:<route>:<key>``, ... (grammar:
  ``gateway.session_selectors``). It is resolved to the session currently holding it at every delivery, so
  ``/new``, a rollover or a compression of the target never strands the opt-in. Exactly one of
  ``session_id`` / ``selector``.
- ``{"kind": "platform", "platform": "...", "chat_id": "...", "thread_id": "..."}``: final output goes to a
  platform channel only; nothing is written into any other session's transcript.
- ``{"kind": "none"}`` or NULL: nothing is delivered beyond the session itself.

:func:`resolve_delivery_target` reads a row; :func:`dispatch` delivers one typed event through the first
sink that accepts the target. Sinks are pluggable (``register_sink``): the gateway registers the one that
admits into a messaging/local chat it owns; core ships

- the session-FIFO sink: a direct ``session_admissions`` write (a delegate parent's FIFO, drained by the
  delegate runner; batch siblings fold into one queued turn, upstream's completion folding), and
- the completion-queue sink: the in-process completion consumer (gateway notification drain or the CLI)
  for a child's completion to a session this process does not drain directly.

A ``platform`` target goes to the registered platform sender only (cron's scheduler delivery registers one).

The attach primitive: an opted-in (``attach: true``) non-delegate session is bound to its target's current
session through ``sessions.attached_session_id`` (:func:`bind_target`), on creation and again on every
delivery, never through ``parent_session_id`` (so no continuation walk can absorb it into its target). A
selector that resolves to nothing delivers nothing (``reason="selector_unresolved"``): the producing session
keeps its output, and the target's list finds the session again once the conversation exists.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

KIND_SESSION, KIND_PLATFORM, KIND_NONE = "session", "platform", "none"
_SESSION_KEYS = frozenset({"kind", "session_id", "selector", "attach", "wake_policy"})
_PLATFORM_KEYS = frozenset({"kind", "platform", "chat_id", "thread_id"})
EVENT_SENDER_KINDS = frozenset({"child", "cron", "webhook", "peer", "parent", "system", "automation"})
# The admission payload key a delivered event rides in a session FIFO (never a person's input).
EVENT_PAYLOAD_KEY = "system_event_v1"
NONE_TARGET = {"kind": KIND_NONE}


class DeliveryTargetError(ValueError):
    """A ``delivery_target`` value of unknown shape."""


def parse_delivery_target(value: Any) -> dict:
    """The validated target a column value names (``{"kind": "none"}`` for NULL / empty)."""
    if value in (None, ""):
        return dict(NONE_TARGET)
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise DeliveryTargetError("delivery_target is not JSON") from exc
    if not isinstance(value, Mapping):
        raise DeliveryTargetError("delivery_target must be an object")
    kind = value.get("kind")
    if kind == KIND_NONE and set(value) == {"kind"}:
        return dict(NONE_TARGET)
    if kind == KIND_SESSION:
        sid, selector = value.get("session_id"), value.get("selector")
        if set(value) - _SESSION_KEYS or (sid is None) == (selector is None) or (
                sid is not None and (not isinstance(sid, str) or not sid)):
            raise DeliveryTargetError("a session target is {kind, session_id | selector, attach[, wake_policy]}")
        attach = value.get("attach", False)
        if type(attach) is not bool:
            raise DeliveryTargetError("attach must be a boolean")
        if selector is not None:
            from gateway.session_selectors import SelectorError, validate_selector
            try:
                target = {"kind": KIND_SESSION, "selector": validate_selector(selector), "attach": attach}
            except SelectorError as exc:
                raise DeliveryTargetError(str(exc)) from exc
        else:
            target = {"kind": KIND_SESSION, "session_id": sid, "attach": attach}
        if value.get("wake_policy") is not None:
            from hermes_state_wake import WAKE_POLICIES
            if value["wake_policy"] not in WAKE_POLICIES:
                raise DeliveryTargetError("unknown wake_policy")
            target["wake_policy"] = value["wake_policy"]
        return target
    if kind == KIND_PLATFORM:
        if set(value) - _PLATFORM_KEYS or not isinstance(value.get("platform"), str) or not value["platform"]:
            raise DeliveryTargetError("a platform target is {kind, platform, chat_id, thread_id}")
        return {"kind": KIND_PLATFORM, "platform": value["platform"],
                "chat_id": None if value.get("chat_id") in (None, "") else str(value["chat_id"]),
                "thread_id": None if value.get("thread_id") in (None, "") else str(value["thread_id"])}
    raise DeliveryTargetError(f"unknown delivery_target kind {kind!r}")


def encode_delivery_target(target: Mapping[str, Any]) -> str:
    return json.dumps(parse_delivery_target(dict(target)), sort_keys=True, separators=(",", ":"))


def session_target(session_id: Optional[str] = None, *, attach: bool, wake_policy: Optional[str] = None,
                   selector: Optional[str] = None) -> dict:
    """A ``kind=session`` target naming one session id, or (``selector=``) a conversation."""
    target = {"kind": KIND_SESSION, "attach": bool(attach)}
    target.update({"selector": selector} if selector is not None else {"session_id": session_id})
    if wake_policy is not None:
        target["wake_policy"] = wake_policy
    return parse_delivery_target(target)


def target_session_id(db, target: Mapping[str, Any], *, profile: Optional[str] = None) -> Optional[str]:
    """The session a ``kind=session`` target names right now: its literal id, or what its selector resolves
    to at this moment (``gateway.session_selectors.resolve_session_selector``); None when nothing holds it."""
    if target.get("kind") != KIND_SESSION:
        return None
    if target.get("session_id"):
        return str(target["session_id"])
    from gateway.session_selectors import resolve_session_selector
    return resolve_session_selector(db, str(target.get("selector") or ""), profile=profile)


def bind_target(db, session_id: str, target: Any = None, *, profile: Optional[str] = None,
                resolved: Optional[str] = None) -> Optional[str]:
    """The attach primitive: bind opted-in *session_id* (``attach: true``, not a delegate child, whose link is
    its parent) to the session its target holds now. Returns the bound session id (None: nothing to bind)."""
    if target is None:
        row = db._read_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None or _is_delegate_row(row):
            return None
        target = resolve_delivery_target(row)
    elif not isinstance(target, Mapping) or "kind" not in target:
        return None
    else:
        target = parse_delivery_target(dict(target))
    if target["kind"] != KIND_SESSION or not target.get("attach"):
        return None
    current = resolved or target_session_id(db, target, profile=profile)
    if current is None or current == session_id:
        return None
    from hermes_state_session_meta import bind_attached_session
    bind_attached_session(db, session_id, current)
    return current


def delegate_target(parent_session_id: str) -> dict:
    """A delegate child's target: its parent, attached (it is in the parent's default list)."""
    return session_target(parent_session_id, attach=True)


def resolve_delivery_target(row: Any) -> dict:
    """The target a sessions row (mapping / ``sqlite3.Row``) names; a malformed value delivers nothing
    (logged), never a guessed destination."""
    try:
        raw = row["delivery_target"] if row is not None else None
    except (KeyError, IndexError, TypeError):
        raw = None
    try:
        return parse_delivery_target(raw)
    except DeliveryTargetError:
        logger.warning("session %s has a malformed delivery_target; nothing is delivered",
                       _get(row, "id"), exc_info=True)
        return dict(NONE_TARGET)


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


# ── events ───────────────────────────────────────────────────────────────────

@dataclass
class DeliveryEvent:
    """One typed result to deliver: a ``system_event`` part plus row provenance. ``fold_key`` groups batch
    siblings (a delegation id) whose queued completions merge into one turn."""

    kind: str
    text: str
    sender_kind: str
    sender_id: str
    meta: dict = field(default_factory=dict)
    sent_at: Optional[float] = None
    source_event_id: Optional[str] = None
    fold_key: Optional[str] = None
    legacy_event: Optional[dict] = None  # the completion-queue shape, for in-process consumers
    # A peer admission's hop in its chain (plan §4.4): stored on the admission, never rendered.
    peer_hop: Optional[int] = None
    # The admission intent a session FIFO records (``queue`` / ``steer``); never part of the payload.
    intent: str = "queue"

    def __post_init__(self):
        if self.sender_kind not in EVENT_SENDER_KINDS:
            raise ValueError(f"unknown event sender_kind {self.sender_kind!r}")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("an event needs a kind")
        self.sent_at = time.time() if self.sent_at is None else float(self.sent_at)

    def part(self) -> dict:
        from agent.message_parts import system_event
        return system_event(self.kind, self.text or "", meta=dict(self.meta))

    def payload(self) -> dict:
        """The admission payload a session FIFO stores (no secrets, no text envelopes)."""
        return {"text": self.text or "", EVENT_PAYLOAD_KEY: {
            "kind": self.kind, "text": self.text or "", "meta": dict(self.meta), "sender_kind": self.sender_kind,
            "sender_id": self.sender_id, "sent_at": self.sent_at, "source_event_id": self.source_event_id,
            **({"fold_key": self.fold_key} if self.fold_key else {}),
            **({"peer_hop": int(self.peer_hop)} if self.peer_hop is not None else {})}}


def event_from_payload(payload: Mapping[str, Any]) -> Optional[dict]:
    """The typed event an admission payload carries, or None for any other input."""
    event = payload.get(EVENT_PAYLOAD_KEY) if isinstance(payload, Mapping) else None
    return dict(event) if isinstance(event, Mapping) else None


def event_events(payload: Mapping[str, Any]) -> list[dict]:
    """Every event of a (possibly folded) admission payload, in arrival order."""
    event = event_from_payload(payload)
    if event is None:
        return []
    folded = event.get("folded")
    return [dict(item) for item in folded] if isinstance(folded, list) else [event]


def event_message(event: Mapping[str, Any]) -> dict:
    """The ``role="user"`` transcript row of one delivered event (Rule 1: never assistant)."""
    from agent.message_parts import system_event_message
    return system_event_message(
        str(event["kind"]), str(event.get("text") or ""), sender_id=str(event["sender_id"]),
        sender_kind=str(event["sender_kind"]), meta=dict(event.get("meta") or {}),
        sent_at=event.get("sent_at"), source_event_id=event.get("source_event_id"))


def held_events(payload: Mapping[str, Any]) -> list[dict]:
    """The typed events of one held admission: a delivered event, or a producer wake (its descriptor's typed
    sender when it has one, else a plain system notification). Structure only, never text."""
    events = event_events(payload)
    if events:
        return events
    descriptor = (payload.get("local_automation_v1") or (payload.get("native_text_v1") or {}).get("automation")
                  or {}) if isinstance(payload, Mapping) else {}
    sender = descriptor.get("sender") if isinstance(descriptor, Mapping) else None
    text = str(payload.get("text") or "") if isinstance(payload, Mapping) else ""
    if isinstance(sender, Mapping):
        return [{"kind": sender["kind"], "text": text, "meta": dict(sender.get("meta") or {}),
                 "sender_kind": sender["sender_kind"], "sender_id": sender["sender_id"]}]
    return [{"kind": "notification", "text": text, "meta": {}, "sender_kind": "system",
             "sender_id": "system:notification"}] if text else []


def write_held_events(db, session_id: str, claim: Mapping[str, Any]) -> list[str]:
    """Held notices a user-turn claim took (``hermes_state_wake``) go into *session_id*'s transcript as their
    own typed ``role="user"`` rows right before the turn's input (the renderer merges them into that one wire
    turn); then they are acknowledged. One a crash leaves unacknowledged is requeued when the claim settles."""
    held = claim.get("held") or []
    if not held:
        return []
    from hermes_state_wake import ack_held_inputs
    written = []
    for row in held:
        for event in held_events(row.get("payload") or {}):
            message = event_message(event)
            db.append_message(session_id, "user", message["content"], parts=message["_parts"],
                              sender_kind=message["sender_kind"], sender_id=message["sender_id"],
                              sent_at=message.get("sent_at"), source_event_id=message.get("source_event_id"),
                              part_kind=message.get("part_kind"))
        written.append(row["admission_id"])
    ack_held_inputs(db, epoch=claim["owner_epoch"], generation=claim["generation"], admission_ids=written,
                    session_id=claim.get("target_session_id"))
    return written


def event_typed_input(events: list[Mapping[str, Any]]) -> Optional[dict]:
    """The ``typed_input`` of a turn whose input is *events* (one row, one part per event)."""
    if not events:
        return None
    from agent.message_parts import system_event, typed_input
    first = events[0]
    parts = [system_event(str(e["kind"]), str(e.get("text") or ""), meta=dict(e.get("meta") or {})) for e in events]
    return typed_input(parts, sender_kind=str(first["sender_kind"]), sender_id=str(first["sender_id"]),
                       sent_at=first.get("sent_at"), source_event_id=first.get("source_event_id"),
                       part_kind=str(first["kind"]))


# ── receipts and sinks ───────────────────────────────────────────────────────

@dataclass
class DeliveryReceipt:
    kind: str
    delivered: bool
    sink: Optional[str] = None
    target_session_id: Optional[str] = None
    admission_id: Optional[str] = None
    folded: bool = False
    reason: Optional[str] = None


Accept = Callable[[Any, Mapping[str, Any], Mapping[str, Any]], bool]
Deliver = Callable[..., DeliveryReceipt]
_sinks: list[tuple[str, Accept, Deliver]] = []
_platform_senders: list[tuple[str, Callable[..., bool]]] = []
_wakers: list[Callable[[Any, str], bool]] = []
_lock = threading.Lock()


def register_sink(name: str, accept: Accept, deliver: Deliver) -> None:
    """Add (or replace) a session sink, tried before the core ones. ``accept(db, target_row, target)``
    picks it; ``deliver(db, target_row, target, event, *, wake_policy, request_id)`` delivers."""
    with _lock:
        _sinks[:] = [s for s in _sinks if s[0] != name]
        _sinks.insert(0, (name, accept, deliver))


def unregister_sink(name: str) -> None:
    with _lock:
        _sinks[:] = [s for s in _sinks if s[0] != name]


def register_platform_sender(name: str, send: Callable[..., bool]) -> None:
    """``send(target, text, *, event) -> bool`` delivers final output to a platform channel only."""
    with _lock:
        _platform_senders[:] = [s for s in _platform_senders if s[0] != name]
        _platform_senders.insert(0, (name, send))


def unregister_platform_sender(name: str) -> None:
    with _lock:
        _platform_senders[:] = [s for s in _platform_senders if s[0] != name]


def register_waker(waker: Callable[[Any, str], bool]) -> None:
    """``waker(db, session_id) -> bool``: schedule the drain of a session that just got input."""
    with _lock:
        if waker not in _wakers:
            _wakers.append(waker)


def unregister_waker(waker: Callable[[Any, str], bool]) -> None:
    with _lock:
        if waker in _wakers:
            _wakers.remove(waker)


def wake(db, session_id: str) -> bool:
    with _lock:
        wakers = list(_wakers)
    for waker in wakers:
        try:
            if waker(db, session_id):
                return True
        except Exception:
            logger.warning("session waker %r failed for %s", waker, session_id, exc_info=True)
    return False


# ── core sinks ───────────────────────────────────────────────────────────────

def _is_delegate_row(row) -> bool:
    from agent.session_directory import _effective_kind
    try:
        return row is not None and _effective_kind(row) == "delegate"
    except (KeyError, IndexError, TypeError):  # a partial row (a producer's projection) names no lineage
        return False


def _fifo_accept(db, row, target) -> bool:
    return _is_delegate_row(row)


# Event fields the producer generates (not the caller's request): an exact retry keeps the original's.
_GENERATED_EVENT_FIELDS = ("sent_at",)


def _same_request(stored: Mapping[str, Any], fresh: Mapping[str, Any]) -> bool:
    """*stored* and *fresh* are the same event request apart from producer-generated fields."""
    def strip(event):
        # JSON identity must distinguish true/1 and false/0, including nested caller metadata.
        return json.dumps({k: v for k, v in dict(event).items() if k not in _GENERATED_EVENT_FIELDS},
                          sort_keys=True, separators=(",", ":"))
    return strip(stored) == strip(fresh)


def _same_payload(stored_payload: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """*payload* is the caller's same request as the admitted *stored_payload*: every caller-supplied
    field (text, kind, meta, sender identity, source event, hop) is equal; only the producer-generated
    event fields may differ. The store asks this inside the admission transaction (``_same_request``)."""
    if not isinstance(stored_payload, Mapping) or not isinstance(payload, Mapping):
        return False
    stored, fresh = event_from_payload(stored_payload), payload.get(EVENT_PAYLOAD_KEY)
    if stored is None or not isinstance(fresh, Mapping) or not _same_request(stored, fresh):
        return False
    return (json.dumps({k: v for k, v in stored_payload.items() if k != EVENT_PAYLOAD_KEY},
                       sort_keys=True, separators=(",", ":"))
            == json.dumps({k: v for k, v in payload.items() if k != EVENT_PAYLOAD_KEY},
                          sort_keys=True, separators=(",", ":")))


def _exact_retry(db, principal: str, session_id: str, request: str, payload: dict, intent: str) -> Optional[dict]:
    """The payload of an exact semantic retry of an already-admitted *request*: the stored one (its
    original ``sent_at``). None when the request is new, or differs in substance (that conflict is
    the store's to refuse). A read before the write: it only routes a known retry past folding; the
    store's in-transaction ``_same_request`` check is what makes overlapping first requests agree."""
    from hermes_state_runtime import find_session_admission
    existing = find_session_admission(db, principal_id=principal, session_id=session_id, request_id=request)
    if existing is None or existing.get("intent") != intent or not _same_payload(existing["payload"], payload):
        return None
    return existing["payload"]


def admit_event(db, session_id: str, event: DeliveryEvent, *, wake_policy: Optional[str] = None,
                request_id: Optional[str] = None, intent: Optional[str] = None) -> DeliveryReceipt:
    """Admit *event* into *session_id*'s FIFO as one queued turn. A batch sibling (same ``fold_key`` and
    sender kind) still queued at the FIFO tail is folded: the waiting row is retired in the same
    transaction that admits the merged one, so the parent runs ONE turn for the whole batch. ``intent``
    is the admission's (``queue`` / ``steer``); a steer-intent event is still a queued turn here: a typed
    event is never handed into a running turn as steer text.

    An exact retry (same principal, target and ``request_id``, same event apart from its generated
    ``sent_at``) returns the original admission instead of conflicting with its own timestamp."""
    from hermes_state_runtime import (
        RuntimeStoreError, admit_session_input, list_session_admissions, replacement_binding, retire_replaced_input,
    )
    from tools.delegate_session_store import runtime_epoch
    epoch = runtime_epoch(db)
    intent = intent or event.intent
    principal = f"{event.sender_kind}:{event.sender_id}"
    request = request_id or event.source_event_id or uuid.uuid4().hex
    payload = event.payload()
    intent = intent if intent in ("queue", "steer") else "queue"
    replaced = None
    caller_request = bool(request_id or event.source_event_id)
    retry = _exact_retry(db, principal, session_id, request, payload, intent) if caller_request else None
    if retry is not None:
        payload = retry  # the original bytes: the store replays the existing admission
    elif event.fold_key:
        queued = [r for r in list_session_admissions(db, session_id=session_id) if r["status"] == "queued"]
        tail = queued[-1] if queued else None
        prior = event_from_payload(tail["payload"]) if tail is not None else None
        if (prior is not None and prior.get("fold_key") == event.fold_key
                and prior.get("sender_kind") == event.sender_kind and tail.get("wake_policy") == wake_policy_value(wake_policy)):
            replaced = tail
            events = event_events(tail["payload"]) + [payload[EVENT_PAYLOAD_KEY]]
            merged = dict(payload[EVENT_PAYLOAD_KEY], folded=events,
                          text="\n\n".join(str(e.get("text") or "") for e in events))
            payload = {"text": merged["text"], EVENT_PAYLOAD_KEY: merged}
            principal = f"{event.sender_kind}:batch:{event.fold_key}"
            request = f"fold:{event.fold_key}:{len(events)}:{request}"
    guard = None
    if replaced is not None:
        binding = replacement_binding(principal, session_id, request)
        guard = lambda conn: retire_replaced_input(conn, replaced["admission_id"], binding)
    # A caller-identified request that overlaps its own first admission (both read no admission
    # above) is reconciled in the admission transaction: same caller request -> the original receipt.
    same_request = _same_payload if caller_request and replaced is None else None
    try:
        row = admit_session_input(db, epoch=epoch, principal_id=principal, session_id=session_id, request_id=request,
                                  payload=payload, _authorize_write=guard, wake_policy=wake_policy,
                                  intent=intent, _same_request=same_request)
    except RuntimeStoreError as exc:
        if replaced is None or exc.reason != "admission_started":
            raise
        # The waiting sibling was claimed first: this completion queues alone.
        return admit_event(db, session_id, DeliveryEvent(**{**event.__dict__, "fold_key": None}),
                           wake_policy=wake_policy, request_id=request_id, intent=intent)
    wake(db, session_id)
    return DeliveryReceipt(KIND_SESSION, True, "session_fifo", session_id, row["admission_id"], replaced is not None)


def wake_policy_value(wake_policy: Optional[str]) -> Optional[str]:
    from hermes_state_wake import stored_wake_policy
    return stored_wake_policy(wake_policy)


def _fifo_deliver(db, row, target, event, *, wake_policy=None, request_id=None) -> DeliveryReceipt:
    return admit_event(db, target["session_id"], event, wake_policy=wake_policy, request_id=request_id)


def _queue_accept(db, row, target) -> bool:
    return row is not None


def _queue_deliver(db, row, target, event, *, wake_policy=None, request_id=None) -> DeliveryReceipt:
    """Hand a child's completion to this process's completion consumer (gateway notification drain or
    CLI), carrying the typed provenance and target. Other producers (cron, webhook) need a registered
    owner sink: their target is a chat this process must admit into, not a delegation completion."""
    if event.legacy_event is None:
        return DeliveryReceipt(KIND_SESSION, False, None, target["session_id"], reason="no_owner")
    from tools.process_registry import process_registry
    evt = dict(event.legacy_event)
    evt.setdefault("parent_session_id", target["session_id"])
    evt["delivery"] = {"target_session_id": target["session_id"], "sender_kind": event.sender_kind,
                       "sender_id": event.sender_id, "kind": event.kind, "meta": dict(event.meta),
                       **({"wake_policy": wake_policy} if wake_policy else {})}
    process_registry.completion_queue.put(evt)
    return DeliveryReceipt(KIND_SESSION, True, "completion_queue", target["session_id"])


_CORE_SINKS: tuple[tuple[str, Accept, Deliver], ...] = (
    ("session_fifo", _fifo_accept, _fifo_deliver),
    ("completion_queue", _queue_accept, _queue_deliver),
)


def dispatch(db, source_row: Any, event: DeliveryEvent, *, request_id: Optional[str] = None,
             target: Optional[Mapping[str, Any]] = None) -> DeliveryReceipt:
    """Deliver *event* (a result of ``source_row``'s session) to ``resolve_delivery_target(source_row)``
    (or an explicit *target*). Never guesses a destination: no target, no delivery."""
    target = parse_delivery_target(dict(target)) if target is not None else resolve_delivery_target(source_row)
    kind = target["kind"]
    if kind == KIND_NONE:
        return DeliveryReceipt(KIND_NONE, False, reason="no_target")
    if kind == KIND_PLATFORM:
        with _lock:
            senders = list(_platform_senders)
        for name, send in senders:
            try:
                if send(target, event.text, event=event):
                    return DeliveryReceipt(KIND_PLATFORM, True, name)
            except Exception:
                logger.warning("platform sender %s failed", name, exc_info=True)
        return DeliveryReceipt(KIND_PLATFORM, False, reason="no_platform_sender")
    wake_policy = target.get("wake_policy")
    resolved = target_session_id(db, target)
    if resolved is None:
        logger.info("delivery_target selector %r holds no session now; nothing delivered", target.get("selector"))
        return DeliveryReceipt(KIND_SESSION, False, None, None, reason="selector_unresolved")
    declared, target = target, {**target, "session_id": resolved}  # sinks address the session held now
    target_row = db._read_one("SELECT * FROM sessions WHERE id = ?", (resolved,))
    if target_row is None:
        return DeliveryReceipt(KIND_SESSION, False, None, resolved, reason="target_missing")
    with _lock:
        sinks = list(_sinks)
    for name, accept, deliver in (*sinks, *_CORE_SINKS):
        try:
            accepted = accept(db, target_row, target)
        except Exception:
            logger.warning("delivery sink %s accept() failed", name, exc_info=True)
            continue
        if not accepted:
            continue
        receipt = deliver(db, target_row, target, event, wake_policy=wake_policy, request_id=request_id)
        if receipt.delivered or receipt.reason != "no_owner":
            receipt.sink = receipt.sink or name
            if receipt.delivered and _get(source_row, "id") and not _is_delegate_row(source_row):
                try:
                    bind_target(db, str(_get(source_row, "id")), declared, resolved=resolved)
                except Exception:  # health: allow BLE001 -- the delivery stands; the binding is bookkeeping
                    logger.warning("could not rebind %s to %s", _get(source_row, "id"), resolved, exc_info=True)
            return receipt
    return DeliveryReceipt(KIND_SESSION, False, None, target["session_id"], reason="no_owner")


def child_completion_event(child_row: Any, entry: Mapping[str, Any], *, fold_key: Optional[str] = None,
                           legacy_event: Optional[dict] = None) -> DeliveryEvent:
    """The typed ``subagent_complete`` event of one child turn's result entry (task-notification shape:
    ids, status, a summary and a pointer; never the child's brief)."""
    sid = str(_get(child_row, "id") or entry.get("session_id") or "")
    status = str(entry.get("status") or "completed")
    meta = {"session_id": sid, "status": status}
    for key in ("exit_reason", "api_calls", "duration_seconds", "delegation_id", "task_index", "admission_id"):
        if entry.get(key) is not None:
            meta[key] = entry[key]
    text = entry.get("summary") or entry.get("error") or ""
    return DeliveryEvent("subagent_complete", str(text), "child", sid, meta=meta, fold_key=fold_key,
                         source_event_id=str(entry.get("admission_id") or "") or None, legacy_event=legacy_event)
