"""Typed harness events that several surfaces append to a conversation (Rule 2: provenance is
structure, never a bracket banner in ``content``). Each builder returns a complete ``role="user"``
system_event row (``agent.message_parts.system_event_message``); the wire renderer presents it.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from agent.message_parts import system_event_message

MCP_RELOAD_KIND = "mcp_reload"
MCP_RELOAD_SENDER = "system:mcp_reload"


def mcp_reload_message(changes: Mapping[str, Iterable[str]], tool_count: int) -> dict:
    """The note that MCP servers were reloaded (gateway ``/reload-mcp`` and the CLI): one system_event
    whose text states what changed and whose meta carries the server names and the tool count."""
    named = {label: sorted(names) for label, names in changes.items() if names}
    detail = "".join(f"{label} servers: {', '.join(names)}. " for label, names in named.items())
    summary = f"{tool_count} MCP tool(s) now available" if tool_count else "No MCP tools available"
    text = f"MCP servers were reloaded. {detail}{summary}. The tool list for this conversation was updated."
    meta = {"tool_count": int(tool_count), **{label.lower(): names for label, names in named.items()}}
    return system_event_message(MCP_RELOAD_KIND, text, sender_id=MCP_RELOAD_SENDER, meta=meta)
