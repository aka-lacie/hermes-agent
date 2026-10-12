"""Session-scoped code-tool discovery and validation-relevant fingerprints."""
from copy import deepcopy
import hashlib
import json


_ANNOTATIONS = {"description", "title", "examples", "$comment", "deprecated", "readOnly", "writeOnly"}
_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_CHILDREN = {"items", "additionalItems", "additionalProperties", "contains", "not",
                    "if", "then", "else", "propertyNames", "unevaluatedProperties", "unevaluatedItems"}
_VALUES = {"type", "required", "enum", "const", "default", "format", "minimum", "maximum",
           "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "pattern",
           "minItems", "maxItems", "uniqueItems", "minContains", "maxContains", "minProperties",
           "maxProperties", "dependentRequired", "$schema"}
_UNSUPPORTED = {"$dynamicRef", "$recursiveRef", "$dynamicAnchor", "$recursiveAnchor", "$vocabulary"}


def _reference(root, ref):
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError("invalid_schema: only local resolvable schema references are supported")
    result = root
    for key in ref[2:].split("/"):
        result = result[key.replace("~1", "/").replace("~0", "~")]
    return result


def _canonical(schema, root, stack=()):
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):
        raise ValueError("invalid_schema: expected a schema object or boolean")
    # A nested resource identifier changes the target of fragment references.
    # Resolve inside that resource before discarding the identifier itself.
    if "$id" in schema:
        root = schema
    if "$ref" in schema:
        reference = schema["$ref"]
        target = (id(root), reference)
        if target in stack:
            raise ValueError("invalid_schema: recursive references are unsupported in v1")
        resolved = _canonical(_reference(root, reference), root, (*stack, target))
        siblings = _canonical({key: value for key, value in schema.items() if key != "$ref"}, root, stack)
        return {"allOf": [resolved, siblings]} if siblings else resolved
    result = {}
    for key, value in schema.items():
        if key in _ANNOTATIONS or key in {"$id", "$anchor"}:
            continue
        if key in _UNSUPPORTED:
            raise ValueError(f"invalid_schema: unsupported validation keyword {key}")
        if key in _SCHEMA_MAPS:
            if key not in {"$defs", "definitions"}:
                result[key] = {name: _canonical(child, root, stack) for name, child in value.items()}
        elif key in _SCHEMA_LISTS:
            result[key] = [_canonical(child, root, stack) for child in value]
        elif key in _SCHEMA_CHILDREN:
            result[key] = ([_canonical(child, root, stack) for child in value]
                           if isinstance(value, list) else _canonical(value, root, stack))
        elif key == "dependencies":
            result[key] = {name: child if isinstance(child, list) else _canonical(child, root, stack)
                           for name, child in value.items()}
        elif key in _VALUES:
            result[key] = sorted(value) if key == "required" else value
        else:
            raise ValueError(f"invalid_schema: unsupported schema keyword {key}")
    return result


def schema_fingerprint(schema):
    from jsonschema.validators import validator_for
    validator_for(schema).check_schema(schema)
    normalized = json.dumps(_canonical(schema, schema), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "v1:" + hashlib.sha256(normalized.encode()).hexdigest()


def validate_arguments(schema, arguments):
    from jsonschema.validators import validator_for
    from jsonschema import FormatChecker
    # Canonicalization also rejects unresolved/external references before a
    # validator could attempt a network fetch or silently ignore a constraint.
    schema_fingerprint(schema)
    validator = validator_for(schema)
    errors = list(validator(schema, format_checker=FormatChecker()).iter_errors(arguments))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ValueError(f"invalid_arguments: {path}: {error.message}; dispatch did not occur")


def current_definitions(context):
    from agent.tool_invocation import session_tool_names
    import model_tools
    context.validate()
    grant = session_tool_names(context)
    if not grant:
        return {}
    definitions = model_tools.get_execution_tool_definitions(
        enabled_toolsets=getattr(context.agent, "enabled_toolsets", None),
        disabled_toolsets=getattr(context.agent, "disabled_toolsets", None),
        quiet_mode=True,
    ) or []
    return {entry["function"]["name"]: entry["function"] for entry in definitions
            if entry.get("function", {}).get("name") in grant}


def describe_tool(context, name):
    from agent.code_cells import is_cell_entry_point
    unavailable = {"todo_list", "memory", "session_search", "delegate_task", "execute_code", "tool_call", "tool_search"}
    if name in unavailable or is_cell_entry_point(name):
        raise ValueError(f"unsupported_from_code: {name}; use agents or workflows namespace when applicable")
    definition = current_definitions(context).get(name)
    if definition is None:
        raise ValueError(f"tool_unavailable: {name}; refresh the current session catalog")
    schema = deepcopy(definition.get("parameters", {"type": "object"}))
    return {"name": name, "schema": schema, "fingerprint": schema_fingerprint(schema),
            "description": definition.get("description", "")}


def preflight_tools(context, expectations):
    for name, expected in expectations.items():
        current = describe_tool(context, name)
        if current["fingerprint"] != expected:
            from agent.code_artifacts import store_json, ToolCallError
            evidence = {"code": "workflow_schema_mismatch", "tool": name,
                        "expected": expected, "current": current, "dispatched": False,
                        "call_id": context.call_id,
                        "recovery": "Adapt the workflow and examples, then deliberately publish a new fingerprint; do not retry unchanged."}
            handle = store_json(evidence, context.agent.session_id)
            evidence["evidence_artifact"] = handle
            context.evidence.append({"code": "workflow_schema_mismatch", "tool": name,
                                     "call_id": context.call_id, "artifact": handle})
            raise ToolCallError("workflow_schema_mismatch", json.dumps(evidence),
                                **{key: value for key, value in evidence.items() if key != "code"})
