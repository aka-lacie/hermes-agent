"""Fail-closed structured results and a single response-only child repair."""
from copy import deepcopy
import json
import time


def schema_validator(schema):
    from agent.workflow_agents import AgentOutputError
    try:
        from jsonschema.validators import validator_for
    except ImportError as exc:
        raise AgentOutputError("Workflow output validation requires jsonschema; install the host dependency.") from exc
    if not isinstance(schema, dict):
        raise AgentOutputError("output_schema must be a JSON Schema object.")
    try:
        validator = validator_for(schema)
        validator.check_schema(schema)
        return validator(schema)
    except Exception as exc:
        raise AgentOutputError(f"Invalid output_schema: {exc}") from exc


def parse_output(text, validator):
    # Unlike legacy delegate summaries this is a machine-output boundary: prose
    # and markdown fences are invalid JSON and receive the one repair allowance.
    value = json.loads(text, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(f"Invalid JSON constant {s}")))
    validator.validate(value)
    return value


def response_only_repair(child, result, errors):
    """Reuse the child's provider transport exactly once, without an agent loop.

    Keep tools advertised for providers needing stable schemas; returned calls
    are always rejected before any execution. Never route into run_conversation.
    """
    from agent.workflow_agents import AgentOutputError
    from tools.delegation_output_schema import build_retry_message
    from agent.turn_truncation import normalize_response_for_agent
    request = child._workflow_request
    request.check()
    messages = deepcopy(result.get("messages") or getattr(child, "_session_messages", []) or [])
    messages.append({"role": "user", "content": build_retry_message(errors)})
    from agent.turn_context import build_api_messages
    from agent.agent_runtime_helpers import sanitize_api_messages
    api_messages, _ = build_api_messages(
        child, messages, current_turn_user_idx=len(messages) - 1,
        ext_prefetch_cache=None, plugin_user_context=None, moa_config=None,
        active_system_prompt=getattr(child, "_cached_system_prompt", None),
    )
    kwargs = child._build_api_kwargs(sanitize_api_messages(api_messages))
    started = time.monotonic()
    response = child._interruptible_api_call(kwargs)
    from agent.turn_usage import record_response_usage
    record_response_usage(
        child, response, messages=messages, api_call_count=int(result.get("api_calls", 0) or 0) + 1,
        api_duration=time.monotonic() - started, compression_attempts=0,
        max_compression_attempts=getattr(child, "max_compression_attempts", 3),
    )
    request.check()
    assistant = normalize_response_for_agent(child, response)
    if getattr(assistant, "tool_calls", None):
        raise AgentOutputError("Response-only repair returned tool calls; no tools were executed.",
                               session_id=request.session_id, partial={"text": getattr(assistant, "content", None)})
    text = getattr(assistant, "content", None) or ""
    if not isinstance(text, str):
        raise AgentOutputError("Response-only repair returned non-text content.", session_id=request.session_id)
    messages.append({"role": "assistant", "content": text, "finish_reason": "stop"})
    child._session_messages = messages
    flush = getattr(child, "_flush_messages_to_session_db", None)
    if flush is not None and flush(messages, None) is False:
        raise AgentOutputError("Could not persist the child's repaired response.", session_id=request.session_id)
    result["messages"] = messages
    result["api_calls"] = int(result.get("api_calls", 0) or 0) + 1
    return text


def validate_workflow_output(child, result):
    from agent.workflow_agents import AgentOutputError
    from tools.delegate_tool_child_run import _SchemaOutcome
    request = child._workflow_request
    validator = schema_validator(request.output_schema)
    from jsonschema.exceptions import ValidationError
    if result.get("failed") or result.get("error") or result.get("interrupted") or not result.get("completed"):
        return _SchemaOutcome(request.output_schema, False, ["Child did not complete successfully."], 0)
    text = result.get("final_response") or ""
    retries = 0
    try:
        value = parse_output(text, validator)
    except (ValueError, ValidationError) as exc:
        retries = 1
        text = response_only_repair(child, result, [str(exc)[:4000]])
        try:
            value = parse_output(text, validator)
        except (ValueError, ValidationError) as final:
            raise AgentOutputError(f"Child output remains invalid after one repair: {final}",
                                   session_id=request.session_id, partial={"text": text}) from final
    result["final_response"] = text
    result["workflow_output"] = value
    return _SchemaOutcome(request.output_schema, True, [], retries)
