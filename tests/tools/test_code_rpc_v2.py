"""RPC lifetime boundaries from the accepted workflow design.

Real sockets, remote-file I/O and child kernels protect against duplicate effects,
retired thread authority, submission context loss and premature long-call timeout.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.usefixtures("rpc_transport_context")

from tools.code_execution_acceptance import RPCState
from tools.code_execution_client import client_source
from tools.code_execution_rpc import _rpc_poll_loop, _rpc_server_loop
from tools.code_kernel import CellAuthority, shutdown_all_kernels


@contextmanager
def socket_host(authority, token="token"):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    stop, state, log, counter = threading.Event(), RPCState(authority), [], [0]
    def serve():
        while not stop.is_set():
            _rpc_server_loop(server, "fixture", log, counter, 20, frozenset({"effect"}),
                             stop, token, rpc_state=state)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname(), state, counter
    finally:
        state.retire()
        stop.set()
        server.close()
        thread.join(2)


def exchange(address, request):
    with socket.create_connection(address, timeout=5) as conn:
        conn.sendall((json.dumps(request) + "\n").encode())
        return json.loads(conn.makefile("r").readline())


def request_for(authority, **overrides):
    return dict(authority.identity(), version=2, op="submit", request_id="original",
                tool="effect", args={}, token="token", **overrides)


def await_result(address, request):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        result = exchange(address, dict(request, op="query"))
        if result["status"] != "in_progress":
            return result
        time.sleep(.01)
    raise AssertionError("request did not settle")


def test_lost_acceptance_and_response_never_repeat_effect(tmp_path):
    """Dropping the submit acknowledgement must recover only the same receipt."""
    authority = CellAuthority("fixture")
    entered, release = threading.Event(), threading.Event()
    effect = tmp_path / "effects"
    def dispatch(name, args, contract=None):
        effect.write_text("once")
        entered.set()
        assert release.wait(5)
        return "non-JSON complete data"
    authority._invoke = dispatch
    with socket_host(authority) as (address, state, counter):
        request = request_for(authority)
        with socket.create_connection(address) as conn:
            conn.sendall((json.dumps(request) + "\n").encode())
        assert entered.wait(5)
        assert exchange(address, request)["status"] == "in_progress"
        assert exchange(address, dict(request, op="query"))["status"] == "in_progress"
        release.set()
        first = await_result(address, request)
        assert first["result"] == "non-JSON complete data"
        assert exchange(address, request) == first
        assert counter == [1] and effect.read_text() == "once"
        # Simulate acceptance-state loss, not proof of nonexecution.
        state.records.clear()
        unknown = exchange(address, dict(request, op="query"))
        assert unknown["code"] == "outcome_unknown"
        assert counter == [1]
        authority.retire()
        assert exchange(address, request)["code"] == "retired_cell"


def test_deadline_and_uncooperative_teardown_do_not_extend_on_heartbeat():
    authority = CellAuthority("fixture", timeout=.15)
    entered, release = threading.Event(), threading.Event()
    def dispatch(*args):
        entered.set()
        release.wait(5)
        return "done"
    authority._invoke = dispatch
    try:
        with socket_host(authority) as (address, state, counter):
            request = request_for(authority)
            exchange(address, request)
            assert entered.wait(5)
            end = time.monotonic() + 5
            while time.monotonic() < end:
                response = exchange(address, dict(request, op="query"))
                if response.get("code") == "cancelled":
                    break
                time.sleep(.01)
            assert response["code"] == "cancelled" and authority.cancel.is_set()
            pending = state.retire(grace=.01)
            assert pending[0]["request_id"] == "original"
            assert counter == [1]  # accepted work consumes budget before completion
    finally:
        release.set()


def test_client_disconnect_queries_original_receipt_instead_of_resubmit(tmp_path):
    authority = CellAuthority("fixture")
    authority._invoke = lambda *args: '{"value": 42}'
    with socket_host(authority) as (address, _state, counter):
        code = client_source() + '''
_bind_cell(IDENTITY)
original = _exchange
seen = []
def lose_ack(request):
    seen.append(request["op"])
    response = original(request)
    if len(seen) == 1:
        raise OSError("acceptance acknowledgement lost")
    return response
_exchange = lose_ack
print(json.dumps([_call("effect", {}), seen]))
'''
        code = code.replace("IDENTITY", repr(authority.identity()))
        env = dict(os.environ, HERMES_RPC_SOCKET=f"tcp://{address[0]}:{address[1]}", HERMES_RPC_TOKEN="token")
        result = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        value, operations = json.loads(result.stdout)
        assert value == {"value": 42}
        assert operations[0] == "submit" and set(operations[1:]) == {"query"}
        assert counter == [1]


@pytest.mark.platforms("posix")
def test_remote_file_submit_recovery_keeps_acceptance_after_result_write_failure(tmp_path, monkeypatch):
    """An env transport failure after a remote response write cannot repeat effects."""
    authority = CellAuthority("fixture")
    effects = []
    authority._invoke = lambda *args: effects.append(1) or '{"complete": true}'
    state, stop, log, counter = RPCState(authority), threading.Event(), [], [0]
    class Shell:
        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            result = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, timeout=timeout,
                                    input=stdin_data, text=True, capture_output=True, check=False)
            return {"output": result.stdout, "returncode": result.returncode}
    import tools.code_execution_rpc as rpc
    original = rpc._remote_write
    writes = []
    def lose_write_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        writes.append(1)
        if len(writes) == 1:
            raise OSError("lost acknowledgement after successful remote write")
        return result
    monkeypatch.setattr(rpc, "_remote_write", lose_write_ack)
    poller = threading.Thread(target=_rpc_poll_loop, args=(Shell(), str(tmp_path), "fixture", log, counter,
                              20, frozenset({"effect"}), stop, "token"), kwargs={"rpc_state": state}, daemon=True)
    poller.start()
    try:
        code = client_source("file") + "\n_bind_cell(" + repr(authority.identity()) + ")\nprint(json.dumps(_call('effect', {})))"
        env = dict(os.environ, HERMES_RPC_DIR=str(tmp_path), HERMES_RPC_TOKEN="token")
        result = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"complete": True}
        assert effects == [1] and counter == [1]
    finally:
        state.retire()
        stop.set()
        poller.join(5)


def run_kernel(code, tmp_path, *, timeout=3):
    from tools.code_kernel import execute_in_session_kernel
    return json.loads(execute_in_session_kernel(
        code, task_id="rpc-v2-kernel", mode="project", child_python=sys.executable,
        child_cwd=str(tmp_path), sandbox_tools=frozenset({"terminal"}), timeout=timeout,
        max_tool_calls=20, reset=False, is_interrupted=lambda: False))


def test_active_thread_and_reused_executor_keep_submission_cell(tmp_path, monkeypatch):
    """A stale thread cannot borrow a later cell; a reused worker can accept new work."""
    calls = []
    def dispatch(name, args, **kwargs):
        calls.append(args["command"])
        return json.dumps({"command": args["command"]})
    monkeypatch.setattr("model_tools.handle_function_call", dispatch)
    try:
        first = run_kernel('''
from hermes_tools import terminal
import threading
from concurrent.futures import ThreadPoolExecutor
pool = ThreadPoolExecutor(max_workers=1)
print(pool.submit(terminal, "first").result())
gate = threading.Event()
old_errors = []
def old_thread():
    gate.wait()
    try:
        terminal("must-not-run")
    except Exception as error:
        old_errors.append(error.code)
old = threading.Thread(target=old_thread)
old.start()
''', tmp_path)
        assert first["status"] == "success", first
        second = run_kernel('''
gate.set()
old.join(2)
print(old_errors)
print(pool.submit(terminal, "second").result())
active = threading.Thread(target=terminal, args=("active-thread",))
active.start()
active.join(2)
pool.shutdown()
''', tmp_path)
        assert second["status"] == "success", second
        assert "retired_cell" in second["output"]
        assert calls == ["first", "second", "active-thread"]
    finally:
        shutdown_all_kernels()


def test_accepted_host_wait_uses_outer_deadline_not_local_code_budget(tmp_path, monkeypatch):
    """A healthy slow host call can exceed the local Python execution allowance."""
    def dispatch(*args, **kwargs):
        time.sleep(2.5)
        return '{"complete": true}'
    monkeypatch.setattr("model_tools.handle_function_call", dispatch)
    try:
        result = run_kernel('from hermes_tools import terminal\nprint(terminal("wait"))', tmp_path, timeout=2)
        assert result["status"] == "success", result
        assert "True" in result["output"] and result["tool_calls_made"] == 1
    finally:
        shutdown_all_kernels()


@pytest.mark.platforms("posix")
def test_remote_kernel_and_per_call_fallback_preserve_wait_deadline(tmp_path, monkeypatch):
    """Run both shipped runners through real shell I/O and the remote python3."""
    from tools.code_kernel_remote import execute_in_remote_kernel, shutdown_all_remote_kernels
    from tools.code_execution_tool import _run_remote_per_call, _finish_remote_kernel_result
    calls = []
    def dispatch(name, args, **kwargs):
        calls.append(args["command"])
        time.sleep(2.5)
        return '{"complete": true}'
    monkeypatch.setattr("model_tools.handle_function_call", dispatch)
    class Shell:
        def get_temp_dir(self):
            return str(tmp_path)
        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            result = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, timeout=timeout,
                                    input=stdin_data, text=True, capture_output=True, check=False)
            return {"output": result.stdout + result.stderr, "returncode": result.returncode}
    code = '''
from hermes_tools import terminal
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=1) as pool:
    print(pool.submit(terminal, "wait").result())
'''
    try:
        result = execute_in_remote_kernel(code, env=Shell(), env_type="fixture", task_env_id="remote-fixture",
                                          sandbox_tools=frozenset({"terminal"}), timeout=2,
                                          max_tool_calls=10, reset=False)
        assert result is not None and result["status"] == "success", result
        assert "True" in result["stdout"]
        visible = json.loads(_finish_remote_kernel_result(result, timeout=2, exec_start=time.monotonic()))
        receipt = visible["call_receipts"][0]
        assert receipt["request_id"] and receipt["cell_id"] and receipt["complete"]
        assert "args" not in receipt and "result" not in receipt
        fallback = json.loads(_run_remote_per_call(Shell(), "fixture", code, "fallback-fixture",
                              frozenset({"terminal"}), timeout=2, max_tool_calls=10, exec_start=time.monotonic()))
        assert fallback["status"] == "success", fallback
        assert "True" in fallback["output"] and calls == ["wait", "wait"]
    finally:
        shutdown_all_remote_kernels()


def test_missing_parent_context_refuses_before_dispatch():
    authority = CellAuthority("fixture")
    authority.parent_context = None
    effects = []
    authority._invoke = lambda *args: effects.append(1)
    with socket_host(authority) as (address, _state, counter):
        response = exchange(address, request_for(authority))
        assert response["code"] == "missing_call_context"
        assert effects == [] and counter == [0]


@pytest.mark.parametrize("error_name,error_code", [("AgentOutputError", "agent_output_invalid"),
                                                    ("AgentInputError", "input_delivery_unsupported")])
def test_host_typed_failure_crosses_client_with_partial_evidence(error_name, error_code):
    from agent import workflow_agents
    error_type = getattr(workflow_agents, error_name)
    authority = CellAuthority("fixture")
    def fail(*args):
        raise error_type("invalid structured reply", session_id="retained-child", partial={"step": 2})
    authority._invoke = fail
    with socket_host(authority) as (address, _state, counter):
        code = client_source() + "\n_bind_cell(" + repr(authority.identity()) + ''')
try:
    _call("effect", {})
except ERROR_TYPE as error:
    print(json.dumps([error.code, error.session_id, error.partial, error.receipt["request_id"]]))
else:
    raise AssertionError("host failure became success")
'''
        code = code.replace("ERROR_TYPE", error_name)
        env = dict(os.environ, HERMES_RPC_SOCKET=f"tcp://{address[0]}:{address[1]}", HERMES_RPC_TOKEN="token")
        result = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        code, child, partial, request_id = json.loads(result.stdout)
        assert (code, child, partial) == (error_code, "retained-child", {"step": 2})
        assert request_id and counter == [1]
