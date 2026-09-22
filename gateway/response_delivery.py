"""Turn-local receipts for responses sent by tools outside final-text delivery.

Call ``record_response_delivery`` only after the transport confirms a message,
attachment, or reaction. Receipts authorize an explicit silence marker; they do
not suppress substantive final text, failures, or blank-response diagnostics.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock

from hermes_constants import get_hermes_home


@dataclass
class _Delivery:
    home: str
    platform: str
    chat_id: str
    thread_id: str
    delivered: bool = False
    active: bool = True
    lock: Lock = field(default_factory=Lock)


_current: ContextVar[_Delivery | None] = ContextVar("response_delivery", default=None)


@contextmanager
def response_delivery_scope(source):
    """A fresh receipt per invocation, shared with context-copied tool threads.

Recursive queued turns get separate receipts. Closing fences off late workers.
No environment fallback or process-global session map is involved.
"""
    platform = getattr(source.platform, "value", source.platform)
    receipt = _Delivery(str(get_hermes_home().resolve()), str(platform),
                        str(source.chat_id), str(source.thread_id or ""))
    token = _current.set(receipt)
    try:
        yield
    finally:
        with receipt.lock:
            receipt.active = False
        _current.reset(token)


def record_response_delivery(*, platform: str, chat_id: str, thread_id: str | None = None) -> bool:
    """Report confirmed user-visible output to this turn's conversation.

Native plugins may call this after a successful send/reaction. Returns False
outside a gateway turn, after it ends, or for a different profile/destination.
Tool success without delivery (reads, scheduled/skipped sends) must not call it.
"""
    receipt = _current.get()
    if receipt is None:
        return False
    from agent.delegation_context import is_delegated_child_context
    if is_delegated_child_context():
        return False
    route = (str(get_hermes_home().resolve()), str(platform), str(chat_id), str(thread_id or ""))
    if route != (receipt.home, receipt.platform, receipt.chat_id, receipt.thread_id):
        return False
    with receipt.lock:
        if not receipt.active:
            return False
        receipt.delivered = True
    return True


def capture_response_delivery(result):
    """Stamp the current turn's result before queued follow-ups can replace it."""
    receipt = _current.get()
    if isinstance(result, dict) and receipt is not None:
        with receipt.lock:
            result["user_visible_response"] = receipt.active and receipt.delivered


def capture_stream_delivery(result, consumer, audio_consumer=None):
    """Count actual conversational output, never ephemeral previews/status UI."""
    if not isinstance(result, dict):
        return
    if getattr(audio_consumer, "suppress_whole_file", False) is True:
        result["user_visible_response"] = True
    probe = getattr(consumer, "has_delivered_response", None)
    if callable(probe) and probe() is True:
        result["user_visible_response"] = True
