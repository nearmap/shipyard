"""A real MCP handshake against the real server process, over a real stdio pipe.

Everything else in this suite talks to the server in-process. This does not: it spawns
`python -m sy_tools.server` as a child, connects the SDK's own stdio client transport to its
pipes, and drives `initialize` -> `tools/list` -> `tools/call` across them. The point is to catch
the class of breakage that only exists on the wire and that an in-memory client cannot see — a
module that fails to import under `-m`, a startup path that writes to stdout before the first
frame, an entry point that never starts the transport at all.

It is deliberately a test rather than a manual step: the pixi environment makes the interpreter
that runs the suite the same one the server is launched with, so `sys.executable` is the honest
command and this runs identically in CI.
"""
from __future__ import annotations

from pathlib import Path
import sys

import mcp
from mcp import StdioServerParameters, stdio_client
import pytest

from sy_tools import SERVER_NAME, SERVER_VERSION

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TOOL_NAMES = {"attach-artifact", "reload_config", "validate_config"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _server_process() -> StdioServerParameters:
    """The same module entry point `.mcp.json`'s task reaches, spawned directly rather than via pixi."""
    return StdioServerParameters(
        command=sys.executable, args=["-m", "sy_tools.server"], cwd=str(PLUGIN_ROOT)
    )


@pytest.mark.anyio
async def test_a_real_client_handshakes_with_a_real_server_process():
    async with mcp.Client(stdio_client(_server_process())) as client:
        info = client.server_info
        assert info is not None, "the handshake produced no serverInfo at all"
        assert info.name == SERVER_NAME
        assert info.version == SERVER_VERSION
        assert client.protocol_version, "the handshake must have settled on a protocol version"

        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == TOOL_NAMES, "the wire must expose the same three tools"

        result = await client.call_tool("validate_config", {})
        assert result.is_error is False, result.content
        assert result.content, "a successful call must carry a content block over the wire"


@pytest.mark.anyio
async def test_a_failing_call_leaves_the_stdio_stream_usable():
    """stdout is protocol-only, and the proof is that the session survives a diagnostic.

    A failing tool makes the server log a traceback. If any of that reached stdout it would land
    mid-frame and desynchronise this client, so the surviving second call is the assertion: the
    diagnostic went to stderr and the stream is still parseable.

    `kind="report"` is deliberate. It is the one artifact kind the gate lets straight through, so
    the missing file is reached and raised over the wire whatever `transcript.attach` resolves to
    in the environment this happens to run in.
    """
    async with mcp.Client(stdio_client(_server_process())) as client:
        failed = await client.call_tool(
            "attach-artifact", {"issue": "PROJ-1", "path": "/nonexistent/x", "kind": "report"}
        )
        assert failed.is_error is True, "a missing artifact must surface as a tool error"

        after = await client.call_tool("validate_config", {})
        assert after.is_error is False, "a logged traceback corrupted the protocol stream"
