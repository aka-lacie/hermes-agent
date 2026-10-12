"""Bounded inline input and scoped immutable handles for retained children."""
import json

INLINE_INPUT_BYTES = 64 * 1024
MAX_REMOTE_INPUT_BYTES = 8 * 1024 * 1024


def prepare_input(value, *, parent, authority):
    from agent.code_artifacts import artifact_info, is_artifact_handle, store_json
    from agent.workflow_agents import AgentInputError, AgentAdmissionError
    artifact = None
    if is_artifact_handle(value):
        try:
            artifact = artifact_info(value, (parent.session_id, getattr(parent, "_delegate_session_id", "")))
        except Exception as exc:
            raise AgentInputError(f"Input artifact is not accessible: {exc}") from exc
        if artifact.get("complete") is not True:
            raise AgentInputError("Input artifact is partial; provide complete input explicitly.")
        size, data = artifact["size_bytes"], None
    else:
        try:
            data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise AgentAdmissionError("input must be JSON serializable or an authorized artifact handle.") from exc
        size = len(data.encode("utf-8"))
    remote = getattr(authority, "artifact_delivery", {}).get("kind") == "remote"
    if remote and size > MAX_REMOTE_INPUT_BYTES:
        raise AgentInputError("Input exceeds the 8 MiB remote artifact ceiling; partition it before delegation.")
    if artifact is None and size <= INLINE_INPUT_BYTES:
        return "WORKFLOW INPUT (JSON):\n" + data, None
    if "execute_code" not in (getattr(parent, "valid_tool_names", None) or ()):
        raise AgentInputError("Artifact workflow input requires an execute_code grant readable by the child.")
    artifact = artifact or store_json(value, session_id=parent.session_id)
    context = (
        "WORKFLOW INPUT is an immutable artifact. Read its complete contents in your own execute_code kernel "
        "using: from hermes_tools import tools; data = tools.read_artifact("
        + repr(artifact) + ")\nDo not treat the handle as the input data."
    )
    return context, artifact
