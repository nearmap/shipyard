"""The `sy` MCP server: newline-delimited JSON-RPC 2.0 over stdio, stdlib only.

Hand-rolled rather than built on the `mcp` PyPI package because Shipyard ships with no
third-party runtime dependency at all, and the protocol surface a stdio tool server actually
needs is small: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`. Frames are
one JSON object per line — no `Content-Length` header framing.

**stdout carries protocol frames and nothing else.** Anything a helper prints to stdout corrupts
the stream and desynchronises the client, which is why the ported adapter code raises instead of
printing and why every diagnostic here goes to stderr.

Run it with `python -m mcp.server` from the plugin root.
"""
from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sys
import traceback
from typing import Any

from . import SERVER_NAME, SERVER_VERSION, config, secrets, tracker

PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 reserved codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class ToolError(RuntimeError):
    """A tool failed in a way the caller should see as a tool result, not a protocol error."""


TOOLS: list[dict[str, Any]] = [
    {
        "name": "attach-artifact",
        "description": (
            "Sanitise a local file and attach it to a tracker issue as a durable artifact "
            "(canonical verb `attach-artifact`). Runs the known-value scrub and the pattern "
            "scanner, in that order, before anything leaves the machine. Gated by the "
            "`transcript.attach` config key; for `ship` callers the `full` process tier is "
            "required on top of it. When the gate is off the call is a no-op skip: nothing is "
            "read, scrubbed, scanned, or uploaded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string", "description": "Canonical issue id, e.g. PROJ-123."},
                "path": {"type": "string", "description": "Path to the artifact to attach."},
                "kind": {
                    "type": "string",
                    "description": "Artifact kind. `transcript` is gated; anything else is ungated.",
                    "default": "transcript",
                },
                "process_tier": {
                    "type": "string",
                    "enum": ["full", "light"],
                    "description": "The calling workflow's process tier. `ship` requires `full`.",
                },
                "caller": {
                    "type": "string",
                    "description": "Workflow asking for the attachment, e.g. ship, spec, plan.",
                },
            },
            "required": ["issue", "path"],
        },
    },
    {
        "name": "reload_config",
        "description": (
            "Re-read the Shipyard configuration layer chain from disk and replace the server's "
            "hot copy. Reports whether the resolved values changed; never reports a value."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "validate_config",
        "description": (
            "Report every reason the resolved configuration would be rejected: schema violations, "
            "missing required keys, an unknown tracker, a required credential absent from the "
            "environment, and model-floor breaches. Side-effect-free, and never prints a value."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def tool_attach_artifact(args: dict[str, Any]) -> dict[str, Any]:
    """Gate, sanitise, upload. The gate is checked before the artifact is so much as opened."""
    issue = _require_str(args, "issue")
    kind = str(args.get("kind") or "transcript")
    caller = str(args.get("caller") or "")
    tier = args.get("process_tier")

    skip = _gate_skip_reason(kind, caller, tier)
    if skip is not None:
        return {"attached": False, "skipped": True, "reason": skip, "issue": issue}

    path = Path(_require_str(args, "path"))
    if not path.is_file():
        raise ToolError(f"artifact not found: {path}")
    backend = tracker.adapter()
    required = tuple(config.adapter_map().get("secret_env", []))
    report = secrets.sanitize(path, require=required, extra_words=config.extra_secret_words())
    evidence = backend.attach_artifact(issue, path)
    return {"attached": True, "skipped": False, "issue": issue, "sanitize": report, "evidence": evidence}


def _gate_skip_reason(kind: str, caller: str, tier: object) -> str | None:
    """Why this attachment must not happen, or None to proceed.

    Mirrors the adapter attachments reference under `skills/tracker/`: `transcript.attach` gates
    transcript attachment everywhere, and a `ship` caller additionally requires the `full`
    process tier on top of it.
    """
    if kind != "transcript":
        return None
    if not config.get("transcript.attach"):
        return "transcript.attach is false"
    if caller == "ship" and tier != "full":
        return f"ship requires the full process tier; got {tier!r}"
    return None


def tool_reload_config(_args: dict[str, Any]) -> dict[str, Any]:
    return config.reload()


def tool_validate_config(_args: dict[str, Any]) -> dict[str, Any]:
    errors = config.validate()
    return {
        "valid": not errors,
        "errors": errors,
        "tracker": config.get("tracker"),
        "fingerprint": config.fingerprint(),
    }


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "attach-artifact": tool_attach_artifact,
    "reload_config": tool_reload_config,
    "validate_config": tool_validate_config,
}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """One request in, one response out — or None for a notification, which takes no reply."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None
    if request_id is None:
        return None  # any other notification: acknowledged by silence, per JSON-RPC 2.0
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        return _result(request_id, _call_tool(params))
    if method == "ping":
        return _result(request_id, {})
    return _error(request_id, METHOD_NOT_FOUND, f"unknown method {method!r}")


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    """A tool result, including a failed one.

    A tool that raises returns `isError` rather than a JSON-RPC error: the call reached the
    server and produced an answer the model should see and can act on, which is exactly the
    distinction the MCP spec draws between a tool failure and a protocol failure.
    """
    name = params.get("name")
    handler = HANDLERS.get(str(name))
    if handler is None:
        return _tool_result({"error": f"unknown tool {name!r}"}, is_error=True)
    try:
        return _tool_result(handler(params.get("arguments") or {}))
    except Exception as exc:  # surfaced to the model, never crashes the loop
        print(f"sy: tool {name} failed: {traceback.format_exc()}", file=sys.stderr, flush=True)
        return _tool_result({"error": f"{type(exc).__name__}: {exc}"}, is_error=True)


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, indent=2, sort_keys=True)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{key!r} is required and must be a non-empty string")
    return value


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """Read newline-delimited JSON-RPC frames until EOF, writing one response line per request."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(stdout, _error(None, PARSE_ERROR, f"invalid JSON: {exc}"))
            continue
        if not isinstance(message, dict):
            _write(stdout, _error(None, INVALID_REQUEST, "a request must be a JSON object"))
            continue
        try:
            response = handle(message)
        except Exception as exc:  # a protocol-level bug must not take the server down
            print(f"sy: {traceback.format_exc()}", file=sys.stderr, flush=True)
            response = _error(message.get("id"), INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        if response is not None:
            _write(stdout, response)
    return 0


def _write(stdout: Any, message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    stdout.flush()


if __name__ == "__main__":
    raise SystemExit(serve())
