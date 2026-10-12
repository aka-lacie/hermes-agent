"""Plugin pre-tool policy resolution shared by model and code invocations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

@dataclass(frozen=True)
class _PreToolCallDirective:
    action: Optional[str] = None
    message: Optional[str] = None
    rule_key: Optional[str] = None
    modified_args: Optional[dict[str, Any]] = None


def _get_pre_tool_call_directive_details(
    tool_name: str, args: Optional[dict[str, Any]], task_id: str = "", session_id: str = "",
    tool_call_id: str = "", turn_id: str = "", api_request_id: str = "",
    middleware_trace: Optional[list[dict[str, Any]]] = None,
    parent_call_id: Optional[str] = None, origin: Optional[str] = None, cell_id: Optional[str] = None,
) -> _PreToolCallDirective:
    """Check ``pre_tool_call`` hooks for ``{"action": "block", "message"}`` (veto; message becomes
    the tool result) or ``{"action": "approve", "message", "rule_key"?}`` (escalate ANY tool to the
    human-approval gate; ``rule_key`` picks the ``[a]lways`` allowlist grain). Precedence is
    ``block`` > ``approve`` > none, not registration order: any plugin's valid veto wins over an
    earlier plugin's request for human confirmation (#87420); among approves the first valid one
    wins. Irrelevant returns are ignored."""
    from hermes_cli.plugins import _thread_tool_whitelist
    allowed = getattr(_thread_tool_whitelist, "allowed", None)
    if allowed is not None and tool_name not in allowed:
        fmt = getattr(_thread_tool_whitelist, "fmt", "Tool '{tool_name}' denied")
        return _PreToolCallDirective(action="block", message=fmt.format(tool_name=tool_name))
    from hermes_cli.lifecycle import invoke_hook as invoke_lifecycle_hook
    provenance = {key: value for key, value in
                  (("parent_call_id", parent_call_id), ("origin", origin), ("cell_id", cell_id))
                  if value is not None}
    hook_results = invoke_lifecycle_hook(
        "pre_tool_call", tool_name=tool_name, args=args if isinstance(args, dict) else {},
        task_id=task_id, session_id=session_id, tool_call_id=tool_call_id, turn_id=turn_id,
        api_request_id=api_request_id, middleware_trace=list(middleware_trace or []),
        **provenance,
    )
    modified_args: Optional[dict[str, Any]] = None
    first_approve: Optional[tuple[Optional[str], Optional[str]]] = None  # (message, rule_key)
    for result in hook_results:
        if not isinstance(result, dict):
            continue
        action = result.get("action")
        # "modify" — transform tool_input before dispatch. Processed before the block/approve gate
        # so modify directives are visible even when a later hook blocks. Each modify directive
        # shallow-merges its keys into one accumulated dict built from the original args.
        if action == "modify":
            partial = result.get("args")
            if isinstance(partial, dict) and partial:
                modified_args = {**(modified_args if modified_args is not None else
                                    (args if isinstance(args, dict) else {})), **partial}
            continue
        if action not in ("block", "approve"):
            continue
        message = result.get("message")
        message = message if isinstance(message, str) and message else None
        # A block directive requires a message (it becomes the tool result); approve's is optional.
        if action == "block" and not message:
            continue
        if action == "block":
            return _PreToolCallDirective(action="block", message=message, modified_args=modified_args)
        # approve is held back until the whole list has been scanned for a veto.
        if first_approve is None:
            rule_key = result.get("rule_key")
            first_approve = (message, (rule_key.strip() or None) if isinstance(rule_key, str) else None)
    if first_approve is not None:
        return _PreToolCallDirective(action="approve", message=first_approve[0], rule_key=first_approve[1],
                                     modified_args=modified_args)
    return _PreToolCallDirective(modified_args=modified_args)
