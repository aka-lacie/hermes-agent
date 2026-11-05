"""Inbound received by an adapter the runner has since retired, held for its replacement.

A reconnect unregisters the failing adapter first (``_handle_adapter_fatal_error_impl``) and
publishes a rebuilt one later. An event the old instance already received and still dispatches in
that window (a debounced text batch flushing after the disconnect, a busy burst, a late socket
frame) names the old instance as its transport, so session admission cannot bind it
(``capture_provenance`` -> ``not_found``) and it used to be dropped with only a log line.

Such an event is held on the retired instance instead. The runner's hand-over at publish time
(``hand_over_held_inbound``: retired instance -> replacement, failed candidate -> retained
predecessor) moves it on; once a registered replacement holding the same credential exists, the
event's transport ref moves to it and it is dispatched through that adapter's own
``handle_message``, where admission re-runs every binding check against the live adapter. Nothing
here grants provenance: only the runner's own lineage links decide where an event may go, and a
replacement with a different credential (another bot) never receives it.
"""

import asyncio
import logging
import weakref

logger = logging.getLogger(__name__)

HELD_MAX = 64
#: A held event still waiting this long is reported at ERROR (and kept for the next hand-over).
ALERT_SECONDS = 30.0
_HELD = "_retired_inbound_events"
_SUCCESSOR = "_retired_inbound_successor"


def receiving_adapter(source):
    ref = getattr(source, "_transport_adapter_ref", None)
    return ref() if callable(ref) else None


def _registered(runner, adapter) -> bool:
    owning = getattr(runner, "_owning_profile", None)
    platform = getattr(adapter, "platform", None)
    if not callable(owning) or platform is None:
        return False
    try:
        registered, _profile = owning(adapter, platform)
    except (TypeError, ValueError):  # a bare rig's runner double: nothing is registered there
        return False
    return bool(registered)


def retired_receiver(runner, source):
    """The adapter that received *source* when *runner* no longer has it registered, else None.

    Only an instance this runner built and later unregistered qualifies: the source must carry a live
    transport ref to an adapter of the source's own platform that is bound to *runner*. Relay
    deliveries (sealed to the process-level connector), restored or hand-built sources (no ref) and
    events of a still-registered adapter keep their existing admission outcome."""
    if source is None or runner is None or getattr(source, "delivered_via_upstream_relay", False) is True:
        return None
    adapter = receiving_adapter(source)
    platform = getattr(source, "platform", None)
    if adapter is None or platform is None or getattr(adapter, "platform", None) != platform:
        return None
    if getattr(adapter, "gateway_runner", None) is not runner or not callable(getattr(runner, "_owning_profile", None)):
        return None
    return None if _registered(runner, adapter) else adapter


def _describe(event) -> str:
    source = getattr(event, "source", None)
    return "message_id=%s chat=%s (%d chars)" % (
        getattr(event, "message_id", None) or "-", getattr(source, "chat_id", None) or "-",
        len(getattr(event, "text", None) or ""))


def _end_of_lineage(adapter):
    """Follow the runner's hand-over links from *adapter* to the newest instance (cycle-safe)."""
    seen = {id(adapter)}
    while True:
        link = adapter.__dict__.get(_SUCCESSOR)
        nxt = link() if link is not None else None
        if nxt is None or id(nxt) in seen:
            return adapter
        seen.add(id(nxt))
        adapter = nxt


def _fingerprint(runner, adapter):
    fingerprint = getattr(runner, "_adapter_credential_fingerprint", None)
    return fingerprint(adapter) if callable(fingerprint) else None


def hold(runner, adapter, event) -> None:
    """Hold *event*, received by retired *adapter*, for the instance that replaces it; dispatch it at
    once when a registered replacement already exists (the batch flushed after the hand-over)."""
    target = _end_of_lineage(adapter)
    if target is not adapter and _registered(runner, target):
        _dispatch(runner, target, [event])
        return
    held = target.__dict__.setdefault(_HELD, [])
    if any(existing is event for existing in held):
        return
    while len(held) >= HELD_MAX:
        dropped = held.pop(0)
        logger.error("[%s] Held-inbound queue full (%d); dropping oldest inbound %s",
                     getattr(target, "name", "adapter"), HELD_MAX, _describe(dropped))
    held.append(event)
    logger.warning(
        "[%s] Holding inbound %s: the adapter that received it was replaced (reconnect); it is "
        "admitted through the reconnected adapter", getattr(target, "name", "adapter"), _describe(event))
    _arm_alert(target, event)


def adopt(predecessor, adapter) -> None:
    """Runner hand-over *predecessor* -> *adapter*: what *predecessor* holds moves on now, and later
    holds on it follow the link. *adapter* owns its own queue again (no reverse link, no cycle)."""
    if predecessor is None or adapter is None or predecessor is adapter:
        return
    if not hasattr(predecessor, "__dict__") or not hasattr(adapter, "__dict__"):
        return
    try:
        link = weakref.ref(adapter)
    except TypeError:
        return
    adapter.__dict__.pop(_SUCCESSOR, None)
    predecessor.__dict__[_SUCCESSOR] = link
    events = predecessor.__dict__.pop(_HELD, None) or []
    if not events and not adapter.__dict__.get(_HELD):
        return
    runner = getattr(adapter, "gateway_runner", None) or getattr(predecessor, "gateway_runner", None)
    if _registered(runner, adapter):
        # Published now: what it held itself before publish (received during its own connect) goes too.
        events += adapter.__dict__.pop(_HELD, None) or []
        if events:
            _dispatch(runner, adapter, events)
        return
    for event in events:
        hold(runner, adapter, event)


def _dispatch(runner, target, events) -> None:
    """Rebind each event's transport to registered *target* and dispatch it there, in order."""
    accepted = []
    for event in events:
        receiver = receiving_adapter(getattr(event, "source", None))
        if receiver is not None and _fingerprint(runner, receiver) != _fingerprint(runner, target):
            # Another credential is another bot: it must not answer what this one received.
            logger.error("[%s] Refusing held inbound %s: the reconnected adapter holds a different credential",
                         getattr(target, "name", "adapter"), _describe(event))
            continue
        accepted.append(event)
    if not accepted:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        target.__dict__.setdefault(_HELD, []).extend(accepted)  # no loop to dispatch on: next hand-over
        return
    for event in accepted:
        event.source._transport_adapter_ref = weakref.ref(target)
    logger.info("[%s] Dispatching %d held inbound message(s) through the reconnected adapter: %s",
                getattr(target, "name", "adapter"), len(accepted), ", ".join(_describe(e) for e in accepted))

    async def _run():
        for event in accepted:
            try:
                await target.handle_message(event)
            except Exception:
                logger.exception("[%s] Held inbound %s failed to dispatch", getattr(target, "name", "adapter"),
                                 _describe(event))

    task = loop.create_task(_run())
    tasks = getattr(target, "_background_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)


def _arm_alert(adapter, event) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # The holder stays alive for the alert (a retired instance nobody replaces would otherwise be
    # collected with its queue before anyone heard of it); the event itself is not pinned.
    event_ref = weakref.ref(event)

    def _check():
        held_event = event_ref()
        if held_event is None:
            return
        if any(existing is held_event for existing in adapter.__dict__.get(_HELD, ())):
            logger.error(
                "[%s] Inbound %s still held %.0fs after its adapter was replaced: no reconnected adapter "
                "has taken it yet; kept for redelivery if the platform reconnects", getattr(adapter, "name", "adapter"),
                _describe(held_event), ALERT_SECONDS)

    loop.call_later(ALERT_SECONDS, _check)


def held_events(adapter) -> list:
    return list(adapter.__dict__.get(_HELD, ()))
