"""Additive approval metadata and process-local, exact-request card bindings."""
from copy import deepcopy
import secrets

_KEY = "_hermes_approval"


def approval_metadata(metadata, data):
    result = dict(metadata or {})
    result[_KEY] = {"request_id": data.get("request_id"), "provenance": deepcopy(data.get("provenance"))}
    return result


def prompt_fields(metadata):
    value = (metadata or {}).get(_KEY, {})
    request_id = value.get("request_id")
    return {"request_id": request_id if isinstance(request_id, str) and request_id else None,
            "provenance": deepcopy(value.get("provenance"))}


def public_metadata(metadata):
    """Platform metadata without the private approval key: adapters (and the relay, which forwards
    metadata verbatim) only ever see thread routing (upstream cfed2ca71f)."""
    if not metadata or _KEY not in metadata:
        return metadata
    return {k: v for k, v in metadata.items() if k != _KEY}


def pending_request_fields(session_key, request_id):
    """Request id and frozen origin of the exact pending approval, read from the approval registry
    by its explicit id instead of being tunnelled through platform metadata."""
    if not (isinstance(request_id, str) and request_id):
        return {"request_id": None, "provenance": None}
    try:
        from tools.approval_gateway_wait import _presenting_entries
        entry = _presenting_entries.get((session_key, request_id))
        if entry is None:
            from tools import approval
            if approval._lock.acquire(timeout=1):
                try:
                    entry = next((e for e in approval._gateway_queues.get(session_key, [])
                                  if e.data.get("request_id") == request_id), None)
                finally:
                    approval._lock.release()
        provenance = deepcopy(entry.data.get("provenance")) if entry is not None else None
    except Exception:
        provenance = None
    return {"request_id": request_id, "provenance": provenance}


class ApprovalCardBinding:
    """Tokens live only with this view; no restart restoration or FIFO fallback."""

    def __init__(self, session_key, request_id, choices):
        self.session_key = session_key
        self.request_id = request_id if isinstance(request_id, str) and request_id else None
        token = secrets.token_hex(16)
        self.ids = {choice: f"ha:1:{token}:{choice}" for choice in choices}

    def resolve(self, custom_id, choice):
        if not self.request_id or not isinstance(custom_id, str) or self.ids.get(choice) != custom_id:
            return 0
        from tools.approval import resolve_gateway_approval
        return resolve_gateway_approval(self.session_key, choice, request_id=self.request_id)


def build_approval_prompt(adapter, chat_id, command, session_key, description, metadata,
                          allow_permanent, allow_session, smart_denied, request_id=None):
    from agent.approval_provenance import format_provenance
    from gateway.platforms.base import ExecApprovalPrompt
    from gateway.platforms.base_exec_approval import ea_default_reason_text
    description = ea_default_reason_text() if description is None else description
    if adapter._EA_REASON_BUDGET:
        description = adapter._ea_fit(str(description or ""), adapter._EA_REASON_BUDGET)
    fields = prompt_fields(metadata)
    if isinstance(request_id, str) and request_id and fields["request_id"] in (None, request_id):
        # The explicit id wins; its frozen origin comes from the approval registry.
        explicit = pending_request_fields(session_key, request_id)
        fields = {"request_id": request_id, "provenance": fields["provenance"] or explicit["provenance"]}
    elif isinstance(request_id, str) and request_id:
        fields = pending_request_fields(session_key, request_id)
    origin = adapter._ea_escape(format_provenance(fields["provenance"]))
    budget = adapter._exec_approval_cmd_budget(description, smart_denied)
    preview = adapter._ea_fit(str(command or ""), max(0, budget - adapter.message_len_fn("\n\n" + origin)))
    return ExecApprovalPrompt(**fields, chat_id=chat_id, session_key=session_key, metadata=public_metadata(metadata),
        command=str(command or ""), description=description, smart_denied=smart_denied,
        text=adapter._format_exec_approval(preview, description, smart_denied) + "\n\n" + origin,
        actions=adapter._exec_approval_actions(allow_permanent=allow_permanent,
            allow_session=allow_session, smart_denied=smart_denied))


def renders_exec_approval_buttons(adapter_cls: type) -> bool:
    """True when the adapter class renders native approval buttons. BasePlatformAdapter subclasses
    say so through ``supports_exec_approval_buttons``; anything else (test doubles, relay-style
    duck types) counts when it defines ``send_exec_approval`` itself."""
    from gateway.platforms.base import BasePlatformAdapter
    probe = getattr(adapter_cls, "supports_exec_approval_buttons", None)
    if callable(probe) and issubclass(adapter_cls, BasePlatformAdapter):
        return bool(probe())
    return getattr(adapter_cls, "send_exec_approval", None) is not None
