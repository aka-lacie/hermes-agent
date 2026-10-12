"""Result fidelity and scoped delivery from the accepted invocation contract.

Real immutable files and real shell staging catch truncation, media/base64 leaks,
false completeness, and remote handles exposed before copy/hash verification.
The MCP renderer/dispatch boundary is exercised; no provider/network is used.
"""
import asyncio
import base64
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from agent.code_artifacts import (ToolCallError, normalize_result, prepare_artifact, read_artifact,
                                  store_json, artifact_info, grant_artifact, MAX_DELIVERY_BYTES)
from agent.code_bridge import bind_cell_authority
from agent.tool_invocation import CallContext, bind_call_context
from tools.code_kernel import CellAuthority
from tools.code_tools_stub import SOURCE


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return CallContext(SimpleNamespace(session_id="owner"), "task", "nested-call", origin="code")


def normalize(raw, call, name="fixture"):
    return normalize_result(raw, context=call, name=name, fingerprint="sha256:fixture", duration_seconds=.25)


def client_tools(call, authority=None):
    namespace = {"ToolCallError": ToolCallError,
                 "_call": lambda name, args: prepare_artifact(args["handle"], context=call, authority=authority)}
    exec(SOURCE, namespace)
    return namespace["tools"]


@pytest.mark.parametrize("payload", [{"structuredContent": {"rows": [1, 2]}, "result": "Retrieved two rows"},
                                      {"result": {"rows": [1, 2]}}, {"result": '{"rows":[1,2]}'}])
def test_all_mcp_structured_locations_preserve_complete_data(payload, call):
    result = normalize(json.dumps(payload), call, "mcp_fixture_query")
    assert result["data"] == {"rows": [1, 2]}
    assert result["meta"]["complete"] is True
    assert result["meta"]["duration_seconds"] == .25


def test_non_json_text_is_never_inferred_and_native_objects_are_preserved(call):
    text = 'Summary mentions {"rows":[1]} but this is prose.'
    result = normalize(text, call)
    assert result["data"] is None and result["text"] == text
    payload = {"status": "ok", "arbitrary": [False, 0, None]}
    assert normalize(json.dumps(payload), call)["data"] == payload


@pytest.mark.parametrize("meta", [{"partial": True}, {"complete": False}, {"truncated": True}, {"status": "incomplete"}])
def test_provider_partial_is_never_success_data(meta, call):
    with pytest.raises(ToolCallError) as raised:
        normalize({"result": '{"rows":[1]}', "_meta": meta}, call, "mcp_fixture_query")
    error = raised.value.record
    assert error["code"] == "result_incomplete" and error["complete"] is False
    assert error["artifact"]["complete"] is False
    assert read_artifact(error["artifact"], "owner")["meta"]["complete"] is False


def test_converted_mcp_error_keeps_diagnostic_and_dispatch_metadata(call):
    with pytest.raises(ToolCallError) as raised:
        normalize(json.dumps({"error": "Provider rejected the operation", "structuredContent": {"retry_after": 4}}), call,
                  "mcp_fixture_write")
    error = raised.value.record
    assert error["code"] == "tool_operation_failed"
    assert error["diagnostic"]["structuredContent"] == {"retry_after": 4}
    assert error["dispatched"] and error["complete"] is False
    assert error["retry_safe"] is None


def test_multimodal_bytes_become_scoped_artifacts_before_transport(call):
    media = b"binary-fixture" * 30
    raw = {"_multimodal": True, "text_summary": "A fixture image", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(media).decode()}},
        {"type": "audio", "mimeType": "audio/wav", "data": base64.b64encode(media).decode()}]}
    result = normalize(raw, call)
    assert "base64" not in json.dumps(result) and "binary-fixture" not in json.dumps(result)
    assert [read_artifact(item["artifact"], "owner") for item in result["media"]] == [media, media]
    with pytest.raises(PermissionError):
        read_artifact(result["media"][0]["artifact"], "another-session")


def test_large_local_result_can_load_or_stream_beyond_remote_ceiling_without_redispatch(call):
    payload = {"value": "x" * (MAX_DELIVERY_BYTES + 1)}
    result = normalize(payload, call)
    assert result["data"] is None and result["artifact"]["complete"] is True
    tools = client_tools(call)
    assert tools.read_artifact(result["artifact"])["data"] == payload
    with tools.open_artifact(result["artifact"]) as stream:
        assert stream.read(1) == b"{"
    # Metadata and paths in a caller handle never override immutable host facts.
    forged = {**result["artifact"], "path": "/etc/passwd", "media_type": "text/plain"}
    assert tools.read_artifact(forged)["data"] == payload


class Shell:
    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        result = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, timeout=timeout,
                                input=stdin_data, text=True, capture_output=True, check=False)
        return {"returncode": result.returncode, "output": result.stdout + result.stderr}


def remote_authority(call, directory):
    authority = CellAuthority("task")
    authority.parent_context = call
    authority.artifact_delivery = {"kind": "remote", "env": Shell(), "directory": str(directory)}
    return authority


def test_remote_file_is_complete_before_handle_exposure_and_kernel_verifies_it(call, tmp_path):
    authority = remote_authority(call, tmp_path / "remote-kernel")
    value = {"rows": ["é" * 1000] * 300}
    with bind_cell_authority(authority):
        result = normalize(value, call)
    prepared = result["artifact"]
    assert prepared["delivery"] == "remote"
    staged = Path(prepared["path"])
    assert staged.is_file() and staged.parent == tmp_path / "remote-kernel"
    assert staged.stat().st_mode & 0o777 == 0o600
    assert json.loads(staged.read_text())["data"] == value
    # Exercise the shipped stdlib client reader against the fully materialized
    # file. The reader never accepts the opaque handle as already-read data.
    namespace = {"ToolCallError": ToolCallError, "_call": lambda *a: prepared}
    exec(SOURCE, namespace)
    assert namespace["tools"].read_artifact(prepared)["data"] == value
    staged.write_bytes(b"incomplete copy")
    with pytest.raises(ToolCallError, match="integrity"):
        namespace["tools"].read_artifact(prepared)


def test_remote_ceiling_preserves_complete_host_result_and_never_returns_unreadable_handle(call, tmp_path):
    value = {"value": "x" * (MAX_DELIVERY_BYTES + 1)}
    authority = remote_authority(call, tmp_path / "remote-kernel")
    with bind_cell_authority(authority), pytest.raises(ToolCallError) as raised:
        normalize(value, call)
    error = raised.value.record
    assert error["code"] == "result_delivery_unsupported"
    assert error["complete"] is True and error["dispatched"] is True
    assert error["limit_bytes"] == MAX_DELIVERY_BYTES
    assert read_artifact(error["artifact"], "owner")["data"] == value
    assert not (tmp_path / "remote-kernel").exists()


def test_child_artifact_survives_session_rotation_but_not_foreign_profile(call, tmp_path, monkeypatch):
    handle = store_json({"input": "complete"}, "owner")
    grant_artifact(handle, "stable-child", owner_session_id="owner")
    child = CallContext(SimpleNamespace(session_id="rotated-child", _delegate_session_id="stable-child"), "child-task", "call")
    assert client_tools(child).read_artifact(handle) == {"input": "complete"}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-profile"))
    with pytest.raises(FileNotFoundError):
        artifact_info(handle, "owner")


@pytest.mark.parametrize("location", ["text", "structuredContent", "result"])
def test_mcp_flood_never_becomes_complete_data_or_uncapped_artifact(location, call):
    from tools.mcp_tool_content import _truncate_mcp_text_result
    from tools.mcp_tool_handlers import _dispatch
    from unittest.mock import patch
    payload = "f" * 2_000_001
    if location == "text":
        async def render():
            return _truncate_mcp_text_result(payload)
        with bind_call_context(call), patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=lambda fn, **kw: asyncio.run(fn())):
            converted = _dispatch("fixture", SimpleNamespace(), "read", render, 5, [], lambda e: None)
        raw = json.loads(converted)
        assert raw["code"] == "result_limit_exceeded" and raw["size_chars"] == 2_000_001
    else:
        raw = {location: payload}
    with pytest.raises(ToolCallError) as raised:
        normalize(raw, call, "mcp_fixture_flood")
    error = raised.value.record
    assert error["code"] == "result_limit_exceeded" and error["complete"] is False
    assert error["limit_chars"] == 2_000_000 and "artifact" not in error


def test_remote_execution_kernel_reads_host_artifact_via_complete_file_bridge(call, tmp_path):
    """The actual shipped file-RPC runner reads its own staged copy, not host JSON."""
    from tools.code_execution_tool import _run_remote_per_call
    import time
    class RemoteShell(Shell):
        def get_temp_dir(self):
            return str(tmp_path / "sandbox")
    handle = store_json({"rows": ["complete"] * 80_000}, "owner")
    code = f'''
from hermes_tools import tools
handle = {handle!r}
value = tools.read_artifact(handle)
assert len(value['rows']) == 80000
assert value['rows'][-1] == 'complete'
with tools.open_artifact(handle) as stream:
    assert '/artifacts/' in stream.name
    assert stream.read(1) == b'{{'
print('artifact-complete-in-own-kernel')
'''
    with bind_call_context(call):
        result = json.loads(_run_remote_per_call(RemoteShell(), "fixture", code, "task", frozenset(),
                            timeout=20, max_tool_calls=5, exec_start=time.monotonic()))
    assert result["status"] == "success", result
    assert "artifact-complete-in-own-kernel" in result["output"]
