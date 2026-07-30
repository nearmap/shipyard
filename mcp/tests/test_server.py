"""Protocol round-trip, and the gate-off no-op that AM-1220's `transcript.attach` key requires."""
from __future__ import annotations

import io
import json

import pytest

from mcp import SERVER_NAME, server


def _roundtrip(*requests: dict) -> list[dict]:
    """Drive `serve` over a real newline-delimited stdio pair and return the parsed responses."""
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    assert server.serve(stdin, stdout) == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line]


def _payload(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


def test_initialize_list_and_call_roundtrip():
    responses = _roundtrip(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "validate_config", "arguments": {}}},
    )
    assert [r["id"] for r in responses] == [1, 2, 3], "a notification must produce no response frame"

    init = responses[0]["result"]
    assert init["protocolVersion"] == server.PROTOCOL_VERSION
    assert init["serverInfo"]["name"] == SERVER_NAME
    assert "tools" in init["capabilities"]

    listed = {t["name"] for t in responses[1]["result"]["tools"]}
    assert listed == {"attach-artifact", "reload_config", "validate_config"}
    for tool in responses[1]["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object", tool["name"]

    assert responses[2]["result"]["isError"] is False
    assert set(_payload(responses[2])) == {"valid", "errors", "tracker", "fingerprint"}


def test_malformed_and_unknown_input_do_not_kill_the_loop():
    stdin = io.StringIO('not json\n{"jsonrpc":"2.0","id":9,"method":"nope"}\n'
                        '{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"ghost"}}\n')
    stdout = io.StringIO()
    assert server.serve(stdin, stdout) == 0
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == server.PARSE_ERROR
    assert responses[1]["error"]["code"] == server.METHOD_NOT_FOUND
    assert responses[2]["result"]["isError"] is True, "an unknown tool is a tool error, not a protocol error"


def test_tool_failure_is_reported_as_a_tool_result():
    responses = _roundtrip({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "attach-artifact", "arguments": {}}})
    assert "error" not in responses[0], "a failing tool must not become a JSON-RPC protocol error"
    assert responses[0]["result"]["isError"] is True


def test_gate_off_is_a_no_op_skip(monkeypatch):
    """AM-1220 coupling: with `transcript.attach` off, nothing is read, scrubbed, or uploaded."""
    def explode(*_args, **_kwargs):
        raise AssertionError("gate-off must skip before any render/scrub/scan/upload work")

    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: False)
    monkeypatch.setattr(server.secrets, "sanitize", explode)
    monkeypatch.setattr(server.tracker, "adapter", explode)

    result = server.tool_attach_artifact({"issue": "PROJ-1", "path": "/nonexistent/never-opened.txt"})
    assert result == {"attached": False, "skipped": True, "reason": "transcript.attach is false", "issue": "PROJ-1"}


def test_ship_caller_needs_the_full_process_tier(monkeypatch):
    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    monkeypatch.setattr(server.secrets, "sanitize", pytest.fail)
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)

    result = server.tool_attach_artifact(
        {"issue": "PROJ-1", "path": "/nonexistent/never-opened.txt", "caller": "ship", "process_tier": "light"}
    )
    assert result["skipped"] is True
    assert "full process tier" in result["reason"]


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
