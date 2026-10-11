"""Public delegation extension contract. Callbacks exchange data, never live agents.

The host owns child construction, identity, ownership, admission and teardown.
One extension per profile may supply the model-facing surface and retention policy.
"""
from __future__ import annotations

from copy import deepcopy


class SubagentExtension:
    def enabled(self) -> bool:
        return True

    def child_options(self, task: dict) -> dict:
        """Optional retain/review_skills booleans and a display title for new children."""
        return {}

    def extend_schema(self, schema: dict) -> dict:
        return schema

    def handle_action(self, action: str, request: dict) -> str | None:
        """Return a tool result, or None to use native dispatch. Spawn stays host-owned."""
        return None

    def format_assignment(self, message, *, session_id: str, timestamp: str):
        return message


def get_subagent_extension():
    from hermes_cli.plugins import get_plugin_manager
    extension = getattr(get_plugin_manager(), "_subagent_extensions", {}).get("default")
    return extension if extension is not None and extension.enabled() else None


def extend_delegation_schema(schema):
    extension = get_subagent_extension()
    return extension.extend_schema(deepcopy(schema)) if extension is not None else schema


def dispatch_extension(action, parent, request):
    from agent.subagent_lifecycle import bind_subagent_parent
    extension = get_subagent_extension()
    if extension is None or action in {"", "spawn"}:
        return None
    with bind_subagent_parent(parent):
        return extension.handle_action(action, deepcopy(request))
