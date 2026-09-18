"""Native MCP handshakes identify the harness without leaking profile metadata."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_cli import __version__
from tools.mcp_tool import MCPServerTask


@pytest.mark.asyncio
async def test_sdk_initialize_receives_hermes_identity():
    from mcp import ClientSession

    class CapturedInitialize(Exception):
        pass

    task = MCPServerTask("example")
    session = ClientSession(MagicMock(), MagicMock(), **task._session_kwargs())
    session.send_request = AsyncMock(side_effect=CapturedInitialize)
    with pytest.raises(CapturedInitialize):
        await session.initialize()
    request = session.send_request.call_args.args[0]
    wire = request.model_dump(by_alias=True, exclude_none=True)
    assert wire["params"]["clientInfo"] == {"name": "hermes-agent", "version": __version__}
