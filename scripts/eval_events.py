#!/usr/bin/env python3
"""Trigger/trace event log for building Shipyard eval harnesses against real runs.

Disabled by default — zero cost unless `debug.evals` is true. When enabled, appends one
compact JSON line per hook firing to ~/.claude/shipyard/eval-events/<session_id>.jsonl: which
skill or subagent triggered (Trigger), and the tool-call sequence around it (Trace). Keyed by
session_id under the home directory, like session_usage.py's usage-agent-map ledger, rather
than a task- or repository-keyed scratch directory — such a directory accumulates every run
against that key, while an eval must read exactly one run, so any key coarser than the session
would interleave concurrent sessions' traces in a single file. Wired into
every PreToolUse call, not just the mutating ones review_guard.py cares about, because
Trigger/Trace evals need to see Skill and Agent invocations too.

Commands:
  hook        Read Claude Code hook JSON from stdin; append an event line if enabled.
  self-test
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from sy_config import get as config_get

SCHEMA = "shipyard.eval_events.v1"
AGENT_TOOL_NAMES = {"Agent", "Task"}
EVENTS_ROOT = Path.home() / ".claude" / "shipyard" / "eval-events"


def enabled() -> bool:
    """Whether the event log is on, per `debug.evals`.

    This runs on every hook firing, so a misconfigured repo must not turn every tool call into a
    hard failure: an unresolvable config leaves the log off, exactly as an unset var used to.
    """
    try:
        return bool(config_get("debug.evals"))
    except SystemExit:
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
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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


def _self_test() -> None:
    import tempfile

    assert normalize_agent_type("sy:gate") == "gate"
    assert normalize_agent_type(None) == "main"
    assert detail("Skill", {"skill": "ship"}) == {"skill": "ship"}
    assert detail("Agent", {"subagent_type": "gate", "description": "review"}) == {
        "subagent_type": "gate",
        "description": "review",
    }
    assert detail("Read", {"file_path": "a.py"}) == {}
    assert build_event({"session_id": ""}) is None

    global EVENTS_ROOT
    original = EVENTS_ROOT
    with tempfile.TemporaryDirectory() as tmp:
        EVENTS_ROOT = Path(tmp) / "eval-events"
        try:
            # Deliberately different cwd per call — a build/gate subagent runs in a worktree,
            # so the ledger must key on session_id alone, never on the caller's cwd.
            record({
                "session_id": "s1", "cwd": "/repo", "hook_event_name": "PreToolUse",
                "agent_type": "sy:gate", "tool_name": "Skill", "tool_input": {"skill": "ship"},
            })
            record({
                "session_id": "s1", "cwd": "/repo-worktrees/branch", "hook_event_name": "Stop",
            })
            lines = events_path("s1").read_text(encoding="utf-8").splitlines()
            assert len(lines) == 2
            first = json.loads(lines[0])
            assert first["tool"] == "Skill"
            assert first["detail"] == {"skill": "ship"}
            assert first["agent_type"] == "gate"
            second = json.loads(lines[1])
            assert second["hook_event"] == "Stop"
            assert "tool" not in second
        finally:
            EVENTS_ROOT = original


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "self-test":
        _self_test()
        print("eval_events self-test passed")
        return 0
    if arg != "hook":
        print("usage: eval_events.py hook|self-test", file=sys.stderr)
        return 2
    if not enabled():
        return 0
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if isinstance(payload, dict):
        record(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
