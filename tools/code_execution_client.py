"""Stdlib-only RPC v2 client shipped into local and remote execution kernels."""

# This source runs only in child kernels, so its thread adapters never change host policy.
_CLIENT = r'''
import contextvars
import contextlib
import inspect
import hashlib
import concurrent.futures
import json
import os
import shlex
import socket
import threading
import time
import tempfile
import uuid

_cell_context = contextvars.ContextVar("hermes_originating_cell", default=None)
_workflow_contract = contextvars.ContextVar("hermes_workflow_contract", default=None)
_call_lock = threading.Lock()
_RPC_DIR = os.environ.get("HERMES_RPC_DIR") or os.path.join(tempfile.gettempdir(), "hermes_rpc")
_seq = 0
_host_wait = threading.Event()

class BridgeError(RuntimeError):
    def __init__(self, code, message, receipt=None, *, session_id=None, partial=None):
        self.code, self.receipt = code, receipt
        self.session_id, self.partial = session_id, partial
        super().__init__(code + ": " + message)

class ToolCallError(BridgeError):
    pass

class AgentError(BridgeError):
    pass
class AgentOutputError(AgentError):
    pass
class AgentAdmissionError(AgentError):
    pass
class AgentInputError(AgentError):
    pass
class AgentCancelledError(AgentError):
    pass
class AgentTimeoutError(AgentError):
    pass

_ERROR_TYPES = {cls.__name__: cls for cls in (ToolCallError, AgentError, AgentOutputError,
                AgentAdmissionError, AgentInputError, AgentCancelledError, AgentTimeoutError)}

def _bind_cell(identity):
    return _cell_context.set((dict(identity), threading.Event()) if identity else None)

@contextlib.contextmanager
def bind_workflow_contract(contract_id):
    token = _workflow_contract.set(contract_id)
    try:
        yield
    finally:
        _workflow_contract.reset(token)

def _reset_cell(token):
    binding = _cell_context.get()
    if binding is not None:
        binding[1].set()
    _cell_context.reset(token)

def _install_context_adapters():
    # Fresh copies are essential: a reused pool worker belongs to the submitting
    # cell, not the cell that happened to create the worker.
    if getattr(threading.Thread.start, "_hermes_context_adapter", False):
        return
    original_start = threading.Thread.start
    if "context" in inspect.signature(threading.Thread).parameters:
        original_init = threading.Thread.__init__
        def init(thread, *args, **kwargs):
            kwargs.setdefault("context", contextvars.copy_context())
            original_init(thread, *args, **kwargs)
        threading.Thread.__init__ = init
    original_submit = concurrent.futures.ThreadPoolExecutor.submit
    def start(thread, *args, **kwargs):
        context, run = contextvars.copy_context(), thread.run
        def run_bound():
            return context.run(run)
        thread.run = run_bound
        return original_start(thread, *args, **kwargs)
    def submit(executor, fn, /, *args, **kwargs):
        context = contextvars.copy_context()
        return original_submit(executor, context.run, fn, *args, **kwargs)
    start._hermes_context_adapter = True
    threading.Thread.start = start
    concurrent.futures.ThreadPoolExecutor.submit = submit

_install_context_adapters()

def _socket_exchange(request):
    endpoint = os.environ["HERMES_RPC_SOCKET"]
    if endpoint.startswith("tcp://"):
        host, _, port = endpoint[6:].rpartition(":")
        conn = socket.create_connection((host, int(port)), timeout=10)
    else:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(10)
        conn.connect(endpoint)
    with conn:
        conn.sendall((json.dumps(request) + "\n").encode())
        buffer = b""
        while b"\n" not in buffer:
            part = conn.recv(65536)
            if not part:
                raise OSError("Host disconnected")
            buffer += part
        return json.loads(buffer.split(b"\n", 1)[0])

def _file_exchange(request):
    global _seq
    _seq += 1
    request = dict(request, seq=_seq)
    directory = _RPC_DIR
    req = os.path.join(directory, "req_%06d" % _seq)
    res = os.path.join(directory, "res_%06d" % _seq)
    fd = os.open(req + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(request, stream)
    os.replace(req + ".tmp", req)
    end = time.monotonic() + 10
    while not os.path.exists(res):
        if time.monotonic() >= end:
            raise OSError("Host heartbeat not received")
        time.sleep(0.05)
    with open(res, encoding="utf-8") as stream:
        response = json.load(stream)
    os.unlink(res)
    return response

def _decode_result(result):
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    return result

def _start_cell_watchdog(local_seconds, absolute_seconds):
    """Per-call remote fallback cannot rely on a persistent host cell poller."""
    def watch():
        remaining = local_seconds or float("inf")
        last = time.monotonic()
        deadline = last + absolute_seconds
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            if not _host_wait.is_set():
                remaining -= now - last
            last = now
            if remaining <= 0 or now >= deadline:
                os._exit(124)
    threading.Thread(target=watch, daemon=True).start()

def _load_rpc_artifact(prepared, receipt):
    code = "artifact_changed"
    try:
        try:
            with open(prepared["path"], "rb") as stream:
                data = stream.read()
        except OSError:
            code = "result_delivery_unsupported"
            raise
        if len(data) != prepared["size_bytes"] or hashlib.sha256(data).hexdigest() != prepared["sha256"]:
            raise ValueError("Terminal RPC artifact failed integrity verification.")
        return json.loads(data)
    except (OSError, KeyError, TypeError, ValueError) as cause:
        record = {"code": code, "message": str(cause)[:4096], "receipt": receipt,
                  "artifact": prepared, "retry_safe": False, "complete": False}
        error = ToolCallError(code, record["message"], receipt, partial=record)
        error.record = record
        raise error from cause

def _call(tool_name, args):
    with _call_lock:
        try:
            return _call_serial(tool_name, args)
        finally:
            _host_wait.clear()

def _call_serial(tool_name, args):
    binding = _cell_context.get()
    if binding is None:
        raise BridgeError("missing_cell_context", "Submit tool work from an active cell using Thread or ThreadPoolExecutor.")
    identity, retired = binding
    if retired.is_set():
        raise BridgeError("retired_cell", "The originating cell retired; submit new work from an active cell.", dict(identity))
    request_id = uuid.uuid4().hex
    request = dict(identity, version=2, request_id=request_id, op="submit",
                   tool=tool_name, args=args, workflow_contract=_workflow_contract.get(), token=os.environ.get("HERMES_RPC_TOKEN", ""))
    receipt = {key: request[key] for key in ("host_id", "cell_id", "nonce", "request_id")}
    # Only submit once, even if the acceptance acknowledgement is lost. A query
    # can recover retained acceptance; an absent record is never permission to replay.
    last_heartbeat = time.monotonic()
    while True:
        try:
            response = _exchange(request)
            last_heartbeat = time.monotonic()
        except (OSError, ValueError):
            request["op"] = "query"
            if time.monotonic() - last_heartbeat >= 30:
                try:
                    _exchange(dict(request, op="cancel"))
                except OSError:
                    pass
                raise BridgeError("outcome_unknown", "Host disconnected; inspect the original receipt before retrying.", receipt)
            time.sleep(0.1)
            continue
        response_id = response.get("request_id", response.get("receipt", {}).get("request_id"))
        if response_id != request_id:
            raise BridgeError("outcome_unknown", "Response identity does not match the original receipt.", receipt)
        status = response.get("status")
        if status == "completed":
            result = response.get("result")
            if isinstance(result, dict) and "_hermes_rpc_artifact" in result:
                result = _load_rpc_artifact(result["_hermes_rpc_artifact"], response.get("receipt", receipt))
            return _decode_result(result)
        if status == "error":
            if "_hermes_rpc_error_artifact" in response:
                response = _load_rpc_artifact(response["_hermes_rpc_error_artifact"], response.get("receipt", receipt))
            error_type = _ERROR_TYPES.get(response.get("type"), BridgeError)
            error = error_type(response.get("code", "rpc_error"), response.get("message", "RPC failed"),
                               response.get("receipt", receipt), session_id=response.get("session_id"),
                               partial=response.get("partial"))
            error.record = dict(response)
            raise error
        if status not in ("accepted", "in_progress"):
            raise BridgeError("protocol_error", "Invalid RPC v2 response", receipt)
        _host_wait.set()
        request["op"] = "query"
        time.sleep(0.1)
'''


def client_source(transport: str = "uds") -> str:
    """Return a self-contained client; no host imports or installation required."""
    if transport not in ("uds", "file"):
        raise ValueError(f"Unknown code bridge transport: {transport}")
    return _CLIENT + "\n_exchange = " + ("_file_exchange" if transport == "file" else "_socket_exchange") + "\n"
