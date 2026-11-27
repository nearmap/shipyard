"""A read-only snapshot of the Claude Code sessions alive on this machine, for `/sy:status`.

`snapshot()` reads the session registry under the Claude config home, keeps each row whose process is
alive, and joins it to the newest `/sy:` command in its conversation file, to its last step, and to any
`/sy:ship` state under the scratch root. A state is keyed by its structured `task` field and counts only at the
directory `scratch_dir` gives that task; no path is ever built from a typed ticket.
It writes nothing, creates nothing, and sends no signal except `kill(pid, 0)` on POSIX.
One unreadable file skips that one item with a warning and never ends the read.

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
import time
from typing import Any

import yaml

SCHEMA = "shipyard.live_sessions.v1"

_COMMAND_MARKER = b"<command-name>/sy:"
_COMMAND_NAME = re.compile(r"<command-name>(/sy:[\w-]+)</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_TICKET_COMMANDS = frozenset({"/sy:spec", "/sy:ship"})
# `/sy:doit` runs `/sy:ship` inline, so no `/sy:ship` record follows it. It clears the typed ticket and its
# arguments are never read (SY-001), which leaves the states the session owns to name the ticket.
_TICKET_RESET = frozenset({"/sy:doit"})
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

    Returns `{"schema", "generated_at", "sessions", "skipped", "warnings"}`. `generated_at` is the UTC time
    of the read, and each row's `state_seconds` is how long it has been in its state at that time, or
    `None` when the registry gives no time. `skipped` counts the registry rows left out:
    `invalid` (not a session record), `foreign_domain` (another PID namespace or machine) and `dead`
    (the process is gone or its PID was reused). A session whose `sessionId` has a newer live row
    carries `stale_copy_of` set to that row's PID.
    """
    # One clock read, so `generated_at` and every `state_seconds` describe the same moment.
    now = time.time()
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
        command = command_ticket = ticket_args = last_step = None
        transcript = _transcript(home / "projects", session_id, warnings)
        if transcript is not None:
            command, command_ticket, ticket_args, readable = _commands(transcript, warnings)
            # A file that could not be opened is one warning already, so it is not opened a second time.
            if readable:
                last_step = _last_step(transcript, warnings)
        ticket, ship = _ship_join(states, session_id, command_ticket)
        sessions.append({
            "pid": record["pid"],
            "session_id": session_id,
            "name": _text(record.get("name")),
            "state": _text(record.get("status")),
            "state_since": _iso(record.get("statusUpdatedAt")),
            "state_seconds": _elapsed(now, record.get("statusUpdatedAt")),
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
        # The whole typed arguments go out with a typed ticket, so the tool can scrub them before it takes the
        # first token: a credential that spans that token and the next would otherwise be cut before its scrub.
        if command_ticket is not None:
            sessions[-1]["ticket_args"] = ticket_args
    return {
        "schema": SCHEMA, "generated_at": _iso(now * 1000), "sessions": sessions, "skipped": skipped,
        "warnings": warnings,
    }


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


def _commands(path: Path, warnings: list[str]) -> tuple[str | None, str | None, str | None, bool]:
    """The latest `/sy:` command the user typed, the ticket of the latest `/sy:spec` or `/sy:ship`
    (cleared by a later `/sy:doit`), the whole arguments of the command that set that ticket, and whether
    the file could be read."""
    command = ticket = ticket_args = None
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
                    # A `/sy:spec` goal in words (more than one token) names no ticket, as with `/sy:doit`.
                    goal = name == "/sy:spec" and len(tokens) > 1
                    ticket = tokens[0] if tokens and not goal else None
                    ticket_args = args if ticket is not None else None
                elif name in _TICKET_RESET:
                    ticket = ticket_args = None
    except OSError as exc:
        warnings.append(f"conversation file {path.name} unreadable: {exc.__class__.__name__}")
        return command, ticket, ticket_args, False
    return command, ticket, ticket_args, True


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
    """The session's last assistant step, read from the file's last 64 KiB only."""
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
        text = block["text"].strip()
        return f"says: {text}" if text else None
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
    """The real task states under the scratch root, each keyed by its `task` field.

    Only two shapes are searched: `<task>/ship-state.yaml`, and below a top-level `<scheme>:` directory,
    where `scratch_dir` nests the task directory of a URL key. A state counts only when its `task` is a
    non-empty string and its directory is the one `scratch_dir` gives that task; any other file is skipped
    without a warning."""
    if not scratch.is_dir():
        return []
    candidates = list(scratch.glob("*/ship-state.yaml"))
    for scheme in scratch.glob("*:"):
        if scheme.is_dir():
            candidates.extend(scheme.glob("**/ship-state.yaml"))
    states = []
    for path in sorted(set(candidates)):
        label = path.parent.relative_to(scratch).as_posix()
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        # `ValueError` also covers a date that does not exist, such as `2026-02-30`.
        except (OSError, ValueError, RecursionError, yaml.YAMLError) as exc:
            warnings.append(f"ship state {label} unreadable: {exc.__class__.__name__}")
            continue
        if not isinstance(loaded, dict):
            warnings.append(f"ship state {label} is not a mapping")
            continue
        task = loaded.get("task")
        if isinstance(task, str) and task and path.parent == scratch / Path(task):
            states.append((task, loaded))
    return states


def _ship_join(
    states: list[tuple[str, dict[str, Any]]], session_id: str | None, command_ticket: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """The ticket and ship progress for one session. Compares strings only and builds no path.

    A ticket the newest command names decides the join: its own state, or no progress when it has none.
    Only a session with no typed ticket falls back to the states it owns, newest start first."""
    if command_ticket is not None:
        match = next((entry for entry in states if entry[0] == command_ticket), None)
        if match is None:
            return command_ticket, None
    else:
        owned = [entry for entry in states if session_id is not None and entry[1].get("ship_session_id") == session_id]
        owned.sort(key=lambda entry: _started(entry[1].get("ship_session_started_at")), reverse=True)
        if not owned:
            return None, None
        match = owned[0]
    ticket, state = match
    return ticket, {key: _plain(state.get(key)) for key in ("phase_active", "gate_rounds_total", "pr")}


_NEVER = dt.datetime.min.replace(tzinfo=dt.UTC)


def _started(value: object) -> dt.datetime:
    """A ship start stamp as an aware time, so mixed spellings order by the moment.

    A naive time or a date is UTC; an unusable value sorts last."""
    try:
        if isinstance(value, dt.datetime):
            parsed = value
        elif isinstance(value, dt.date):
            parsed = dt.datetime(value.year, value.month, value.day)
        elif isinstance(value, str):
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return _NEVER
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except (ValueError, OverflowError):
        return _NEVER


def _plain(value: object) -> object:
    return value if value is None or isinstance(value, str | int | float | bool) else str(value)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _epoch(record: dict[str, Any]) -> int | float:
    # The raw number, never `float(value)`: an int and a float compare natively, and an int too large for a
    # float would raise `OverflowError`.
    value = record.get("startedAt")
    return value if isinstance(value, int | float) and not isinstance(value, bool) else float("-inf")


def _elapsed(now: float, milliseconds: object) -> int | None:
    """Whole seconds from `milliseconds` to `now`, never below 0, or `None` when it is not a usable number."""
    if not isinstance(milliseconds, int | float) or isinstance(milliseconds, bool):
        return None
    try:
        return max(0, int(now - milliseconds / 1000))
    except (OverflowError, ValueError):  # too large for a float, infinite, or NaN
        return None


def _iso(milliseconds: object) -> str | None:
    if not isinstance(milliseconds, int | float) or isinstance(milliseconds, bool):
        return None
    try:
        moment = dt.datetime.fromtimestamp(milliseconds / 1000, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")
