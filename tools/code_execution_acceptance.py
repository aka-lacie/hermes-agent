"""RPC acceptance records shared by socket and remote-file transports.

Records survive client reconnects for the life of their kernel. Cell retirement
invalidates identities independently of record retention; loss of a kernel or
host never authorizes replay. There is no durable crash recovery promise.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

HOST_ID = secrets.token_hex(16)


def rpc_error(code, message, request):
    return {"version": 2, "status": "error", "code": code, "message": message,
            "receipt": {k: request.get(k) for k in ("host_id", "cell_id", "nonce", "request_id")}}


@dataclass
class Acceptance:
    request: dict
    authority: object
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: dict | None = None


class RPCState:
    """One sequential kernel's acceptance ledger, independent of connections."""

    def __init__(self, authority=None):
        self.authority = authority
        self.records = {}
        self.lock = threading.Lock()
        self.retention_seconds = 600

    def _validate(self, request):
        if request.get("host_id") != HOST_ID:
            return rpc_error("outcome_unknown", "Host acceptance state changed; reconcile the original receipt before retrying.", request)
        authority = self.authority
        if authority is None or request.get("nonce") != authority.nonce or request.get("cell_id") != authority.cell_id:
            return rpc_error("retired_cell", "The originating cell has retired. Submit new work from an active cell.", request)
        if not authority.active:
            return rpc_error("retired_cell", "The originating cell has retired. Submit new work from an active cell.", request)
        if authority.parent_context is None or authority.parent_context.task_id != authority.task_id:
            return rpc_error("missing_call_context", "Code tools require a validated originating agent call.", request)
        if authority.cancelled():
            authority.cancel.set()
            return rpc_error("cancelled", "The originating cell was cancelled or reached its absolute deadline.", request)
        try:
            authority.parent_context.validate()
        except ValueError as exc:
            return rpc_error("invalid_call_context", str(exc), request)
        except RuntimeError as exc:
            authority.cancel.set()
            return rpc_error("cancelled", str(exc), request)
        return None

    def handle(self, request, execute):
        """Record acceptance before starting effects. Repeated submits only read it."""
        with self.lock:
            error = self._validate(request)
            if error:
                return error
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
                return rpc_error("invalid_request", "A bounded request ID is required.", request)
            key = (request["nonce"], request_id)
            record = self.records.get(key)
            op = request.get("op")
            if op == "cancel":
                self.authority.cancel.set()
                return rpc_error("cancelled", "Cancellation requested.", request)
            if record is not None:
                if op == "submit" and tuple(record.request.get(k) for k in ("tool", "args", "workflow_contract")) != tuple(request.get(k) for k in ("tool", "args", "workflow_contract")):
                    return rpc_error("request_id_conflict", "A request ID cannot name different effects.", request)
                return self._response(record)
            if op == "query":
                return rpc_error("outcome_unknown", "No retained acceptance record. Inspect external state; do not automatically resubmit.", request)
            if op != "submit":
                return rpc_error("invalid_request", "Expected submit, query or cancel.", request)
            if any(not r.done.is_set() for r in self.records.values()):
                return rpc_error("bridge_busy", "This kernel already has an accepted tool call.", request)
            self._prune()
            record = Acceptance(dict(request), self.authority)
            self.records[key] = record
            record.authority.begin_wait()
            try:
                threading.Thread(target=self._execute, args=(record, execute), daemon=True,
                                 name="hermes-code-rpc").start()
            except RuntimeError as exc:
                record.result = json.dumps({"error": "Host could not start tool work: " + str(exc)})
                record.authority.end_wait()
                record.done.set()
                return self._response(record)
            return {"version": 2, "status": "accepted", "request_id": request_id}

    def _prune(self):
        # Never evict current-cell identities, including completed effects.
        cutoff = time.monotonic() - self.retention_seconds
        self.records = {key: record for key, record in self.records.items()
                        if record.authority is self.authority or not record.done.is_set()
                        or record.authority.retired_at is None or record.authority.retired_at >= cutoff}

    @staticmethod
    def _execute(record, execute):
        try:
            from agent.code_rpc_results import bounded_result
            value = execute(record.authority)
            record.result = record.authority.ctx.copy().run(bounded_result, value, record.authority)
        except BaseException as exc:
            logger.error("Accepted code request failed", exc_info=True)
            # Even handler exceptions are settled outcomes, never replay candidates.
            serializer = getattr(exc, "to_dict", None)
            try:
                details = serializer() if callable(serializer) else {}
                if not isinstance(details, dict):
                    details = {}
            except Exception:
                logger.exception("Could not serialize typed RPC error evidence")
                details = {}
            prefix = str(exc).split(":", 1)[0]
            code = details.get("code") or getattr(exc, "code", None)
            if not code:
                code = prefix if prefix.isidentifier() else "tool_error"
            record.error = rpc_error(code, str(exc), record.request)
            record.error.update(details, type=details.get("type", type(exc).__name__))
            from agent.code_rpc_results import bounded_error
            record.error = record.authority.ctx.copy().run(bounded_error, record.error, record.authority)
        finally:
            record.authority.end_wait()
            record.done.set()

    @staticmethod
    def _response(record):
        response = {"version": 2, "request_id": record.request["request_id"]}
        if record.done.is_set() and record.error is not None:
            return dict(record.error, request_id=record.request["request_id"])
        if record.done.is_set():
            response.update(status="completed", result=record.result)
        else:
            response.update(status="in_progress", heartbeat=time.monotonic())
        return response

    def retire(self, grace=1.0):
        with self.lock:
            authority = self.authority
            if authority is None:
                return []
            authority.retire()
            pending = [r for r in self.records.values() if r.authority is authority]
        deadline = time.monotonic() + grace
        for record in pending:
            record.done.wait(max(0, deadline - time.monotonic()))
        return [{"request_id": r.request["request_id"], "tool": r.request.get("tool"),
                 "status": "teardown_incomplete"} for r in pending if not r.done.is_set()]
