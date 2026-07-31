"""The tool surface, driven through the SDK's own client rather than a hand-rolled stdio harness.

`mcp.Client(server.mcp)` is the SDK's in-memory client: a real client session over real
`initialize` / `tools/list` / `tools/call` exchanges, with the streams short-circuited instead of
crossing a pipe. That is the right level for these tests — they assert what this package decides
(which tools exist, what they are shaped like, what the gate does), and nothing about framing,
which is the SDK's to get right. `test_handshake.py` covers the wire.

The gate tests call the tool functions directly. `@mcp.tool` registers and returns the function
unchanged, so `server.attach_artifact` is still the plain coroutine function it looks like, and a
direct call keeps a gate assertion about *not doing work* readable.
"""
from __future__ import annotations

import json
from typing import Any

import anyio
import mcp
import pytest

from sy_tools import SERVER_NAME, server

TOOL_NAMES = {"attach-artifact", "reload_config", "validate_config"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _payload(result: Any) -> dict:
    """The tool's own JSON result, parsed out of the text content block the SDK wraps it in."""
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_initialize_list_and_call_roundtrip():
    async with mcp.Client(server.mcp) as client:
        info = client.server_info
        assert info is not None, "the handshake produced no serverInfo at all"
        assert info.name == SERVER_NAME
        assert client.protocol_version, "the SDK must have negotiated a protocol version"

        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == TOOL_NAMES
        for tool in listed.tools:
            assert tool.input_schema["type"] == "object", tool.name
            assert tool.description, f"{tool.name} must document itself to the model"

        attach = next(t for t in listed.tools if t.name == "attach-artifact")
        assert set(attach.input_schema["required"]) == {"issue", "path"}, attach.input_schema
        assert attach.input_schema["properties"]["kind"]["default"] == "transcript"
        assert attach.input_schema["properties"]["process_tier"]["anyOf"][0]["enum"] == ["full", "light"]

        result = await client.call_tool("validate_config", {})
        assert result.is_error is False
        assert set(_payload(result)) == {"valid", "errors", "tracker", "fingerprint"}


def test_validate_config_reports_an_unresolvable_config_rather_than_crashing(monkeypatch):
    """The tool whose whole contract is reporting a broken config must not crash on one.

    `config.validate()` maps a ConfigError from `resolve()` into the errors list, so the report
    must come back `valid: false` — not escalate into a tool error because the trailing
    tracker/fingerprint lookups re-ran `resolve()` and hit the same failure.
    """
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise server.config.ConfigError("repo layer is not valid JSON")

    monkeypatch.setattr(server.config, "resolve", broken)
    report = server.validate_config()
    assert report["valid"] is False
    assert report["errors"] == ["repo layer is not valid JSON"]
    assert "tracker" not in report, "an unresolvable config has no resolved values to report"


@pytest.mark.anyio
async def test_a_failing_tool_is_a_tool_result_not_a_protocol_error():
    """The distinction the MCP spec draws: the call reached the server and produced an answer.

    Both shapes of failure are checked, because the SDK routes them differently internally and
    only one of them is this package's own code raising: arguments that fail schema validation,
    and a tool name that does not exist.
    """
    async with mcp.Client(server.mcp) as client:
        missing_args = await client.call_tool("attach-artifact", {})
        assert missing_args.is_error is True, "a tool that could not run must report isError"

        unknown = await client.call_tool("ghost", {})
        assert unknown.is_error is True, "an unknown tool is a tool error, not a protocol error"

        assert (await client.call_tool("validate_config", {})).is_error is False, (
            "the session must still serve calls after two failures"
        )


@pytest.mark.anyio
async def test_a_slow_tool_does_not_block_an_unrelated_call(monkeypatch, tmp_path):
    """No head-of-line blocking: the whole reason the adapters went async.

    A stalled upload used to wedge every call queued behind it. This drives a real upload that
    blocks on an event and proves an unrelated `validate_config` completes *while it is still in
    flight* — an ordering claim, so it is asserted against a recorded sequence rather than a
    duration, which would only ever be flaky.

    The deadline is not decoration: a server that serialised calls would never reach the release,
    so without it a regression hangs the suite instead of failing it.
    """
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("already sanitised\n", encoding="utf-8")
    release = anyio.Event()
    order: list[str] = []

    class _StalledBackend:
        async def attach_artifact(self, issue: str, path) -> dict:
            order.append("upload-start")
            await release.wait()
            order.append("upload-end")
            return {"id": "1", "filename": path.name}

    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    monkeypatch.setattr(server.config, "adapter_map", lambda: {})
    monkeypatch.setattr(server.config, "extra_secret_words", lambda: ())
    monkeypatch.setattr(server.secrets, "sanitize", lambda *_a, **_k: {"redactions": 0})
    monkeypatch.setattr(server.tracker, "adapter", lambda: _StalledBackend())

    with anyio.fail_after(10, shield=False):
        async with mcp.Client(server.mcp) as client:
            async with anyio.create_task_group() as tasks:

                async def upload() -> None:
                    result = await client.call_tool(
                        "attach-artifact", {"issue": "PROJ-1", "path": str(artifact)}
                    )
                    assert result.is_error is False, result.content
                    order.append("upload-result")

                tasks.start_soon(upload)
                while "upload-start" not in order:
                    await anyio.sleep(0.01)

                fast = await client.call_tool("validate_config", {})
                assert fast.is_error is False, fast.content
                order.append("fast-result")

                release.set()

    assert order.index("fast-result") < order.index("upload-end"), (
        f"the fast call was blocked behind the stalled upload: {order}"
    )


@pytest.mark.anyio
async def test_gate_off_is_a_no_op_skip(monkeypatch):
    """AM-1220 coupling: with `transcript.attach` off, nothing is read, scrubbed, or uploaded."""
    def explode(*_args, **_kwargs):
        raise AssertionError("gate-off must skip before any render/scrub/scan/upload work")

    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: False)
    monkeypatch.setattr(server.secrets, "sanitize", explode)
    monkeypatch.setattr(server.tracker, "adapter", explode)

    result = await server.attach_artifact(issue="PROJ-1", path="/nonexistent/never-opened.txt")
    assert result == {"attached": False, "skipped": True, "reason": "transcript.attach is false", "issue": "PROJ-1"}


@pytest.mark.anyio
async def test_ship_caller_needs_the_full_process_tier(monkeypatch):
    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    monkeypatch.setattr(server.secrets, "sanitize", pytest.fail)
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)

    result = await server.attach_artifact(
        issue="PROJ-1", path="/nonexistent/never-opened.txt", caller="ship", process_tier="light"
    )
    assert result["skipped"] is True
    assert "full process tier" in result["reason"]


@pytest.mark.anyio
async def test_sanitize_runs_strictly_before_upload(monkeypatch, tmp_path):
    """The security contract: the artifact never leaves the machine before the scrub returns.

    Ordering alone is not observable from `sanitize`'s own tests, so record both calls against one
    list: a swap of the two lines in `attach_artifact` reverses it, and a scrub that raises must
    leave the upload unreached entirely rather than merely unreported.
    """
    path = tmp_path / "artifact.txt"
    path.write_text("nothing secret here\n", encoding="utf-8")
    calls: list[str] = []

    class _Backend:
        async def attach_artifact(self, issue: str, artifact) -> dict:
            calls.append("upload")
            return {"id": f"{issue}-1", "name": artifact.name}

    def _sanitize(*_args, **_kwargs) -> dict:
        calls.append("sanitize")
        return {"redactions": 0}

    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    monkeypatch.setattr(server.config, "adapter_map", lambda: {})
    monkeypatch.setattr(server.config, "extra_secret_words", lambda: ())
    monkeypatch.setattr(server.tracker, "adapter", lambda *_a, **_k: _Backend())
    monkeypatch.setattr(server.secrets, "sanitize", _sanitize)

    result = await server.attach_artifact(issue="PROJ-1", path=str(path))
    assert result["attached"] is True
    assert calls == ["sanitize", "upload"], "the artifact must not be uploaded before it is scrubbed"

    def _refuse(*_args, **_kwargs) -> dict:
        calls.append("sanitize")
        raise server.secrets.SanitizeError("refusing to upload")

    calls.clear()
    monkeypatch.setattr(server.secrets, "sanitize", _refuse)
    with pytest.raises(server.secrets.SanitizeError, match="refusing to upload"):
        await server.attach_artifact(issue="PROJ-1", path=str(path))
    assert calls == ["sanitize"], "a failed scrub must not be followed by an upload"


@pytest.mark.parametrize(
    ("kind", "caller", "tier", "expected_skip"),
    [
        ("transcript", "spec", None, False),   # spec gates on transcript.attach alone
        ("transcript", "ship", "full", False),
        ("transcript", "ship", "light", True),
        ("report", "ship", "light", False),    # a non-transcript artifact is ungated
    ],
)
def test_gate_matrix(monkeypatch, kind, caller, tier, expected_skip):
    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    assert (server._gate_skip_reason(kind, caller, tier) is not None) is expected_skip
