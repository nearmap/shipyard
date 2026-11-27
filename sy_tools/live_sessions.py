"""A read-only snapshot of the Claude Code sessions alive on this machine, for `/sy:status`.

`snapshot()` reads the session registry under the Claude config home, keeps each row whose process is
alive, and joins it to the newest `/sy:` command in its conversation file, to its last step, and to any
`/sy:ship` state under the scratch root. It writes nothing, creates nothing, and sends no signal except
`kill(pid, 0)` on POSIX. One unreadable file skips that one item with a warning and never ends the read.

The `sy` MCP server exposes it as the `live_sessions` tool. Text copied out of a conversation is data
for display, never an instruction. `snapshot()` returns it uncut. The tool scrubs it, then shortens it
to `COMMAND_LIMIT` or `STEP_LIMIT` characters, before it leaves the server.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import yaml

SCHEMA = "shipyard.live_sessions.v1"

_COMMAND_MARKER = b"<command-name>/sy:"
_COMMAND_NAME = re.compile(r"<command-name>(/sy:[\w-]+)</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_TICKET_COMMANDS = frozenset({"/sy:spec", "/sy:ship"})
# A session id becomes part of a glob pattern, so anything but a plain id is never looked up.
_SESSION_ID = re.compile(r"[\w-]+")
_TAIL_BYTES = 64 * 1024
# The tool cuts the command and the last step to these lengths after it scrubs them, never before: a cut
# made first can split a credential, and the scrub then no longer finds the part that is left.
COMMAND_LIMIT = 120
STEP_LIMIT = 160


def claude_home() -> Path:
    """The Claude config home: `CLAUDE_CONFIG_DIR` when it is set and not empty, else `~/.claude`."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "")
    return Path(configured) if configured else Path.home() / ".claude"


def snapshot(home: Path, scratch: Path) -> dict[str, Any]:
    """Every live session in `home`'s registry, joined to its conversation file and `/sy:ship` state.

    Returns `{"schema", "sessions", "skipped", "warnings"}`. `skipped` counts the registry rows left out:
    `invalid` (not a session record), `foreign_domain` (another PID namespace or machine) and `dead`
    (the process is gone or its PID was reused). A session whose `sessionId` has a newer live row
    carries `stale_copy_of` set to that row's PID.
    """
    warnings: list[str] = []
    skipped = {"invalid": 0, "foreign_domain": 0, "dead": 0}
    domain = _own_domain()
    live: list[tuple[dict[str, Any], str]] = []
    for record in _registry(home / "sessions", skipped, warnings):
        if domain is not None and record.get("pidDomain") not in (None, domain):
            skipped["foreign_domain"] += 1
            continue
        liveness = _liveness(record["pid"], record.get("procStart"))
        if liveness is None:
            skipped["dead"] += 1
            continue
        live.append((record, liveness))

    newest: dict[str, dict[str, Any]] = {}
    for record, _ in live:
        session_id = record.get("sessionId")
        if isinstance(session_id, str):
            held = newest.get(session_id)
            if held is None or _epoch(record) > _epoch(held):
                newest[session_id] = record

    states = _ship_states(scratch, warnings)
    sessions = []
    for record, liveness in live:
        session_id = _text(record.get("sessionId"))
        current = newest.get(session_id) if session_id is not None else None
        command = command_ticket = last_step = None
        transcript = _transcript(home / "projects", session_id, warnings)
        if transcript is not None:
            command, command_ticket = _commands(transcript, warnings)
            last_step = _last_step(transcript, warnings)
        ticket, ship = _ship_join(states, session_id, command_ticket)
        sessions.append({
            "pid": record["pid"],
            "session_id": session_id,
            "name": _text(record.get("name")),
            "state": _text(record.get("status")),
            "state_since": _iso(record.get("statusUpdatedAt")),
            "started_at": _iso(record.get("startedAt")),
            "entrypoint": _text(record.get("entrypoint")),
            "cwd": _text(record.get("cwd")),
            "liveness": liveness,
            "stale_copy_of": current["pid"] if current is not None and current is not record else None,
            "command": command,
            "ticket": ticket,
            "ship": ship,
            "last_step": last_step,
        })
    return {"schema": SCHEMA, "sessions": sessions, "skipped": skipped, "warnings": warnings}


def _registry(directory: Path, skipped: dict[str, int], warnings: list[str]) -> list[dict[str, Any]]:
    if not directory.is_dir():
        warnings.append("no session registry: the sessions directory is missing")
        return []
    records = []
    # Only `*.json`: the registry's `*.key` siblings hold secrets and are never opened.
    for path in sorted(directory.glob("*.json")):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            warnings.append(f"registry file {path.name} unreadable: {exc.__class__.__name__}")
            continue
        try:
            record = json.loads(raw)
        # `ValueError` covers bad JSON, bad UTF-8 and an int too long to convert; deep nesting recurses.
        except (ValueError, RecursionError):
            record = None
        pid = record.get("pid") if isinstance(record, dict) else None
        # `bool` is an `int`, and a PID of 0 or below names a process group to `kill`, not one process.
        if not isinstance(record, dict) or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            skipped["invalid"] += 1
            continue
        records.append(record)
    return records


def _own_domain() -> str | None:
    if sys.platform != "linux":
        return None
    try:
        machine = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
    return f"linux:{machine}:{namespace}"


def _liveness(pid: int, proc_start: object) -> str | None:
    """How the PID was found alive, or `None` when it is dead or reused."""
    # On Windows signal 0 is `CTRL_C_EVENT`, so `os.kill` is never called there.
    if os.name != "posix":
        return "unchecked"
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except (OSError, OverflowError):
        return None
    if sys.platform != "linux" or proc_start is None:
        return "pid-only"
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
        fields = stat.rsplit(")", 1)[1].split()
        started = fields[19]
    except (OSError, IndexError):
        return None
    return "pid-and-start-time" if started == str(proc_start) else None


def _transcript(projects: Path, session_id: str | None, warnings: list[str]) -> Path | None:
    if session_id is None or not _SESSION_ID.fullmatch(session_id):
        return None
    dated = []
    for path in projects.glob(f"*/{session_id}.jsonl"):
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError as exc:
            warnings.append(f"conversation file for {session_id} unreadable: {exc.__class__.__name__}")
    if len(dated) > 1:
        warnings.append(f"{len(dated)} conversation files for {session_id}: the newest is used")
    return max(dated)[1] if dated else None


def _commands(path: Path, warnings: list[str]) -> tuple[str | None, str | None]:
    """The latest `/sy:` command the user typed, and the ticket of the latest `/sy:spec` or `/sy:ship`."""
    command = ticket = None
    try:
        with path.open("rb") as handle:
            # Streamed line by line and decoded per line, so one bad line is a warning, not a failed read.
            for lineno, raw in enumerate(handle, 1):
                if _COMMAND_MARKER not in raw:
                    continue
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    warnings.append(f"undecodable line {lineno} in {path.name}")
                    continue
                typed = _typed_command(line)
                if typed is None:
                    continue
                name, args = typed
                command = f"{name} {args}".strip()
                if name in _TICKET_COMMANDS:
                    tokens = args.split()
                    ticket = tokens[0] if tokens else None
    except OSError as exc:
        warnings.append(f"conversation file {path.name} unreadable: {exc.__class__.__name__}")
    return command, ticket


def _typed_command(line: str) -> tuple[str, str] | None:
    """The command name and arguments of a record the user typed as a slash command, else `None`.

    Only a human turn whose content is the command message itself counts. The same tags inside a tool
    result, an assistant reply or any other record are text, not a command.
    """
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("type") != "user" or record.get("turnOrigin") != "human":
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.startswith("<command-message>"):
        return None
    name = _COMMAND_NAME.search(content)
    if name is None:
        return None
    args = _COMMAND_ARGS.search(content)
    return name.group(1), args.group(1).strip() if args else ""


def _last_step(path: Path, warnings: list[str]) -> str | None:
    """One line about the session's last assistant step, read from the file's last 64 KiB only."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - _TAIL_BYTES)
            handle.seek(start)
            tail = handle.read()
    except OSError as exc:
        warnings.append(f"conversation file {path.name} unreadable: {exc.__class__.__name__}")
        return None
    lines = tail.split(b"\n")
    if start > 0:
        lines = lines[1:]
    for raw in reversed(lines):
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        # A thinking-only record says nothing to show, so the step is the last text or tool call.
        for block in reversed(content if isinstance(content, list) else []):
            step = _describe(block)
            if step is not None:
                return step
    return None


def _describe(block: object) -> str | None:
    if not isinstance(block, dict):
        return None
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        lines = block["text"].strip().splitlines()
        return f"says: {lines[0]}" if lines else None
    if block.get("type") != "tool_use" or not isinstance(block.get("name"), str):
        return None
    if block["name"] == "AskUserQuestion":
        tool_input = block.get("input")
        questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
        first = questions[0] if isinstance(questions, list) and questions else None
        question = first.get("question") if isinstance(first, dict) else None
        if isinstance(question, str):
            return f"asks: {question}"
    return f"runs {block['name']}"


def _ship_states(scratch: Path, warnings: list[str]) -> list[tuple[str, dict[str, Any]]]:
    if not scratch.is_dir():
        return []
    states = []
    for path in sorted(scratch.glob("*/ship-state.yaml")):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        # `ValueError` also covers a date that does not exist, such as `2026-02-30`.
        except (OSError, ValueError, RecursionError, yaml.YAMLError) as exc:
            warnings.append(f"ship state {path.parent.name} unreadable: {exc.__class__.__name__}")
            continue
        if not isinstance(loaded, dict):
            warnings.append(f"ship state {path.parent.name} is not a mapping")
            continue
        states.append((path.parent.name, loaded))
    return states


def _ship_join(
    states: list[tuple[str, dict[str, Any]]], session_id: str | None, command_ticket: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """The ticket and ship progress for one session. Compares strings only and builds no path."""
    owned = [entry for entry in states if session_id is not None and entry[1].get("ship_session_id") == session_id]
    named = [entry for entry in states if command_ticket is not None and entry[0] == command_ticket]
    preferred = [entry for entry in owned if entry in named]
    match = (preferred or owned or named or [None])[0]
    if match is None:
        return command_ticket, None
    ticket, state = match
    return ticket, {key: _plain(state.get(key)) for key in ("phase_active", "gate_rounds_total", "pr")}


def _plain(value: object) -> object:
    return value if value is None or isinstance(value, str | int | float | bool) else str(value)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _epoch(record: dict[str, Any]) -> int | float:
    # The raw number, never `float(value)`: an int and a float compare natively, and an int too large for a
    # float would raise `OverflowError`.
    value = record.get("startedAt")
    return value if isinstance(value, int | float) and not isinstance(value, bool) else float("-inf")


def _iso(milliseconds: object) -> str | None:
    if not isinstance(milliseconds, int | float) or isinstance(milliseconds, bool):
        return None
    try:
        moment = dt.datetime.fromtimestamp(milliseconds / 1000, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")
