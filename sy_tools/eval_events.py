#!/usr/bin/env python3
"""Trigger/trace event log for building Shipyard eval harnesses against real runs.

Disabled by default — zero cost unless `debug.evals` is true. When enabled, appends one compact JSON
line per hook firing to ~/.claude/shipyard/eval-events/<session_id>.jsonl: which skill or subagent
triggered (Trigger), and the tool-call sequence around it (Trace). It fires on every PreToolUse call,
not only the mutating ones, so a Trigger/Trace eval sees Skill and Agent invocations too.

Commands:
  PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python -m sy_tools.eval_events hook
      Read Claude Code hook JSON from stdin; append an event line if enabled.

That hook runs on bare `python` with no environment of its own, so **this module's import graph must
stay standard library only** — `sy_tools.config` is admissible because it is stdlib-only too, and
nothing here may reach the MCP server or anything the server needs. "Bare `python`" is also whatever
interpreter the operator has on `PATH`, so the graph must import on 3.9. `sy_tools/usage.py` and
`sy_tools/guards/secret_guard.py` are the siblings under the same constraint.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from .config import ConfigError
from .config import get as config_get

SCHEMA = "shipyard.eval_events.v1"
AGENT_TOOL_NAMES = {"Agent", "Task"}
# Keyed by session id, never by task or repository: an eval must read exactly one run, and any
# coarser key accumulates runs and interleaves concurrent sessions' traces in a single file.
EVENTS_ROOT = Path.home() / ".claude" / "shipyard" / "eval-events"


def enabled() -> bool:
    """Whether the event log is on, per `debug.evals`; an unresolvable config leaves it off."""
    try:
        return bool(config_get("debug.evals"))
    # This runs on every hook firing, so a misconfigured repo must cost a trace, not the tool call.
    # `OSError` too: the resolver shells out to `git` and reads layer files.
    except (ConfigError, OSError):
        return False


def normalize_agent_type(agent_type: str | None) -> str:
    if not agent_type:
        return "main"
    return str(agent_type).split(":")[-1]


def detail(tool_name: str, tool_input: dict) -> dict:
    if tool_name == "Skill":
        return {"skill": tool_input.get("skill")}
    if tool_name in AGENT_TOOL_NAMES:
        out: dict = {"subagent_type": tool_input.get("subagent_type")}
        description = tool_input.get("description")
        if description:
            out["description"] = description
        return out
    return {}


def build_event(payload: dict) -> dict | None:
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return None
    event: dict = {
        "schema": SCHEMA,
        # `datetime.UTC` is 3.11+ and this runs on whatever bare `python` is on `PATH` — 3.9 on some
        # machines, where a crash is the one failure mode a disabled-by-default log must not have.
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),  # noqa: UP017
        "session_id": session_id,
        "hook_event": payload.get("hook_event_name"),
        "agent_type": normalize_agent_type(payload.get("agent_type") or payload.get("agentType")),
    }
    agent_id = payload.get("agent_id") or payload.get("agentId")
    if agent_id:
        event["agent_id"] = agent_id
    tool_name = payload.get("tool_name")
    if tool_name:
        event["tool"] = tool_name
        extra = detail(str(tool_name), payload.get("tool_input") or {})
        if extra:
            event["detail"] = extra
    return event


def events_path(session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_") or "unknown"
    return EVENTS_ROOT / f"{safe}.jsonl"


def record(payload: dict) -> None:
    event = build_event(payload)
    if event is None:
        return
    path = events_path(event["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
    # O_APPEND keeps each small event write atomic on normal local filesystems.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def main() -> int:
    """Run the `hook` command: append one event line for this hook firing, if the log is enabled.

    The only command. Malformed stdin is a success: exit 0, nothing recorded.
    """
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg != "hook":
        print("usage: python -m sy_tools.eval_events hook", file=sys.stderr)
        return 2
    if not enabled():
        return 0
    try:
        payload = json.load(sys.stdin)
    # A non-zero exit on an unparseable payload would interrupt the session this log exists to observe.
    except json.JSONDecodeError:
        return 0
    if isinstance(payload, dict):
        record(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
