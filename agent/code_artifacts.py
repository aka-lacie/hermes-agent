"""Immutable, profile/session-scoped code results and complete-file delivery.

Model presentation budgets never truncate these records. MCP flood/resource
limits still apply before storage; an artifact cannot bypass those limits.
"""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import uuid

INLINE_BYTES = 256 * 1024
MAX_DELIVERY_BYTES = 8 * 1024 * 1024
MAX_MEDIA_BYTES = 50 * 1024 * 1024
MCP_TEXT_LIMIT = 2_000_000


class ToolCallError(RuntimeError):
    """Known tool/delivery failure with a serializable, inspectable receipt."""
    def __init__(self, code, message, **record):
        self.code = code
        self.record = {"code": code, "message": message, **record}
        self.partial = self.record
        super().__init__(message)

    def to_dict(self):
        return {"type": type(self).__name__, **self.record, "partial": self.record}


def _root():
    from hermes_constants import get_hermes_home
    root = get_hermes_home() / "cache" / "workflow-artifacts"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def is_artifact_handle(value):
    return isinstance(value, dict) and value.get("kind") == "hermes_artifact"


def _path(handle):
    ident = handle.get("id") if isinstance(handle, dict) else None
    if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
        raise ToolCallError("invalid_artifact", "Supply a returned immutable artifact handle.", dispatched=False)
    return _root() / ident


def _private_write(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".artifact-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _public(meta):
    return {key: value for key, value in meta.items() if key != "sessions"}


def store_bytes(data, session_id, *, media_type="application/octet-stream", complete=True):
    if not session_id:
        raise ToolCallError("missing_session", "Artifacts require an owning session.", dispatched=False)
    ident = uuid.uuid4().hex
    path = _root() / ident
    meta = {"kind": "hermes_artifact", "id": ident, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "media_type": media_type,
            "complete": complete, "sessions": [session_id]}
    _private_write(path, data)
    _private_write(path.with_suffix(".json"), json.dumps(meta).encode())
    return _public(meta)


def store_json(value, session_id, *, complete=True):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    return store_bytes(data, session_id, media_type="application/json", complete=complete)


@contextmanager
def _locked(handle):
    from tools.skill_usage import skill_file_lock
    path = _path(handle)
    with skill_file_lock(path.with_suffix(".lock")):
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8-sig"))
        yield path, meta


def _authorized(meta, session_id):
    identities = session_id if isinstance(session_id, (tuple, list)) else (session_id,)
    if not any(identity and identity in meta["sessions"] for identity in identities):
        raise PermissionError("artifact_scope: artifact is not readable by this session")


def _verified_bytes(path, meta):
    data = path.read_bytes()
    if len(data) != meta["size_bytes"] or hashlib.sha256(data).hexdigest() != meta["sha256"]:
        raise ToolCallError("artifact_changed", "Immutable artifact checksum does not match.", complete=False, dispatched=False)
    return data


def artifact_info(handle, session_id):
    with _locked(handle) as (path, meta):
        _authorized(meta, session_id)
        _verified_bytes(path, meta)
        return _public(meta)


def read_artifact(handle, session_id):
    """Host reader for authorized input validation; local data has no remote cap."""
    with _locked(handle) as (path, meta):
        _authorized(meta, session_id)
        data = _verified_bytes(path, meta)
        return json.loads(data) if meta["media_type"] == "application/json" else data


def grant_artifact(handle, child_session_id, *, owner_session_id):
    with _locked(handle) as (path, meta):
        _authorized(meta, owner_session_id)
        if child_session_id not in meta["sessions"]:
            meta["sessions"].append(child_session_id)
            _private_write(path.with_suffix(".json"), json.dumps(meta).encode())


def _identities(context):
    agent = context.agent
    return (getattr(agent, "session_id", ""), getattr(agent, "_delegate_session_id", ""))


def prepare_artifact(handle, *, context, authority=None):
    """Resolve a scoped handle into a verified file in the calling kernel.

    The remote file is written atomically and hash-checked before its path is
    returned. Host artifacts remain available if materialization fails; this
    operation never calls the original tool again.
    """
    if authority is None:
        from agent.code_bridge import current_cell_authority
        authority = current_cell_authority()
    delivery = getattr(authority, "artifact_delivery", {"kind": "local"})
    context.validate()
    with _locked(handle) as (path, meta):
        _authorized(meta, _identities(context))
        data = _verified_bytes(path, meta)
        public = _public(meta)
    if delivery.get("kind") == "local":
        return {**public, "path": str(path), "delivery": "local"}
    if delivery.get("kind") != "remote" or not delivery.get("env") or not delivery.get("directory"):
        raise ToolCallError("result_delivery_unsupported", "No artifact adapter is available for this kernel.",
                            artifact=public, complete=meta["complete"], dispatched=True)
    if len(data) > MAX_DELIVERY_BYTES:
        raise ToolCallError("result_delivery_unsupported", "Remote artifacts are limited to 8 MiB; retrieve the retained host artifact locally.",
                            artifact=public, size_bytes=len(data), limit_bytes=MAX_DELIVERY_BYTES,
                            complete=meta["complete"], dispatched=True, retry_safe=False)
    from tools.code_execution_rpc import _execute_checked, _private_dirs_cmd
    env, directory = delivery["env"], delivery["directory"]
    target = directory.rstrip("/") + "/" + meta["id"]
    script = (
        "import base64,hashlib,os,sys,tempfile; "
        "data=base64.b64decode(sys.stdin.buffer.read(),validate=True); "
        "target,digest,size=sys.argv[1:]; "
        "assert len(data)==int(size) and hashlib.sha256(data).hexdigest()==digest,'artifact checksum mismatch'; "
        "fd,tmp=tempfile.mkstemp(dir=os.path.dirname(target),prefix='.artifact-'); "
        "stream=os.fdopen(fd,'wb'); stream.write(data); stream.close(); os.replace(tmp,target)"
    )
    try:
        context.validate()
        _execute_checked(env, _private_dirs_cmd(directory), "artifact directory setup", timeout=30)
        command = "python3 -c " + shlex.quote(script) + " " + " ".join(map(shlex.quote, (target, meta["sha256"], str(len(data)))))
        _execute_checked(env, command, "complete artifact materialization", timeout=60,
                         stdin_data=base64.b64encode(data).decode("ascii"))
        context.validate()
    except Exception as exc:
        raise ToolCallError("result_delivery_unsupported", f"Artifact materialization failed: {exc}",
                            artifact=public, complete=meta["complete"], dispatched=True, retry_safe=False) from exc
    return {**public, "path": target, "delivery": "remote"}


def _media_reference(value, session_id, *, media_type=None):
    if isinstance(value, dict):
        value = value.get("url") or value.get("data")
    if not isinstance(value, str):
        raise ToolCallError("result_delivery_unsupported", "Media result has no supported reference.", complete=False, dispatched=True)
    if not value.startswith("data:"):
        return {"reference": value, "media_type": media_type}
    header, separator, encoded = value.partition(",")
    if not separator or ";base64" not in header:
        raise ToolCallError("result_delivery_unsupported", "Only base64 data URLs are supported for inline media.", complete=False, dispatched=True)
    if len(encoded) > (MAX_MEDIA_BYTES * 4 // 3 + 4):
        raise ToolCallError("result_limit_exceeded", "Media exceeds the existing 50 MiB resource limit.",
                            complete=False, limit_bytes=MAX_MEDIA_BYTES, dispatched=True)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ToolCallError("result_delivery_unsupported", "Malformed media encoding.", complete=False, dispatched=True) from exc
    return {"artifact": store_bytes(decoded, session_id, media_type=media_type or header[5:].split(";", 1)[0])}


def _partial_metadata(data):
    if not isinstance(data, dict):
        return {}
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    own_meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    for candidate in (data, meta, own_meta):
        if (candidate.get("complete") is False or candidate.get("partial") is True
                or candidate.get("truncated") is True or candidate.get("status") in ("partial", "incomplete", "truncated")):
            return {"complete": False, "reason": "provider_partial"}
    return {}


def normalize_result(raw, *, context, name, fingerprint, duration_seconds=None, dispatched=True):
    """Normalize supported native/MCP envelopes without guessing JSON from prose."""
    parsed = raw
    text = raw if isinstance(raw, str) else ""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    session_id = getattr(context.agent, "session_id", "")
    meta = {"tool": name, "call_id": context.call_id, "fingerprint": fingerprint,
            "duration_seconds": duration_seconds, "complete": True, "dispatched": bool(dispatched)}
    if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("isError")):
        code = parsed.get("code") or ("result_limit_exceeded" if "result_limit_exceeded" in str(parsed.get("error", "")) else "tool_operation_failed")
        uncertain = parsed.get("outcome_uncertain") is True
        if uncertain:
            code = "outcome_unknown"
        details = {key: parsed[key] for key in ("limit_chars", "size_chars", "limit_bytes", "size_bytes", "retry_after") if key in parsed}
        raise ToolCallError(code, str(parsed.get("error") or "MCP tool operation failed"),
                            **{**meta, "complete": False}, **details, diagnostic=parsed, retry_safe=False if uncertain else None)
    partial = _partial_metadata(parsed)
    if partial:
        meta.update(partial)
    media = []
    data = parsed
    if isinstance(parsed, dict) and parsed.get("_multimodal"):
        from agent.code_artifact_results import multimodal_content
        data, text, media = multimodal_content(parsed, context=context, name=name, fingerprint=fingerprint,
                                              duration_seconds=duration_seconds, dispatched=dispatched)
    elif isinstance(parsed, dict) and name.startswith("mcp_"):
        from agent.code_artifact_results import mcp_content
        data, text = mcp_content(parsed, meta)
    for reference in re.findall(r"MEDIA:(?:\"([^\"]+)\"|([^\s]+))", text):
        media.append({"reference": "MEDIA:" + (reference[0] or reference[1])})
    if partial:
        preview = {"data": data, "text": text, "media": media, "artifact": None, "meta": meta}
        artifact = store_json(preview, session_id, complete=False)
        raise ToolCallError("result_incomplete", "The provider returned partial data; inspect the retained partial artifact.",
                            **meta, artifact=artifact, retry_safe=None)
    result = {"data": data, "text": text, "media": media, "artifact": None, "meta": meta}
    # Media must be materialized before its handle is exposed to a remote kernel.
    for item in media:
        if "artifact" in item:
            item["artifact"] = prepare_artifact(item["artifact"], context=context)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > INLINE_BYTES:
        handle = store_json(result, session_id)
        prepared = prepare_artifact(handle, context=context)
        return {"data": None, "text": "", "media": [], "artifact": prepared, "meta": meta}
    return result
