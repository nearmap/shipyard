#!/usr/bin/env python3
"""Aggregate Claude Code token usage across a session and all subagent transcripts.

The parser is deliberately tolerant of transcript schema additions. It counts API usage blocks
attached to assistant messages, de-duplicates by message/request id, and scans the main transcript
plus the documented nested subagent transcript tree.

`summarize()` and `handbacks()` are the surfaces the `sy` MCP server exposes as the
`usage_summarize` and `worker_handbacks` tools; `summarize`'s output is compact
JSON suitable for a standalone tracker comment. The one command is the hook:

  PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python -m sy_tools.usage hook
      Read Claude Code hook JSON from stdin and record agent-id/type/transcript mapping.
"""
from __future__ import annotations

# A hook runs this on bare `python`, so the import graph stays stdlib-only and reaches nothing the MCP
# server needs; the dependency runs one way, `server.py` imports this and never the reverse.
import argparse
from collections import defaultdict
from collections.abc import Iterable
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any

LEDGER_ROOT = Path.home() / ".claude" / "shipyard" / "usage-agent-map"
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add_mapping(self, usage: dict[str, Any]) -> None:
        # Both cache_*_input_tokens and cache_*_tokens spellings occur across surfaces; accept either.
        self.input_tokens += _nonnegative_int(usage.get("input_tokens", 0))
        self.output_tokens += _nonnegative_int(usage.get("output_tokens", 0))
        self.cache_read_input_tokens += _nonnegative_int(
            usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0))
        )
        self.cache_creation_input_tokens += _nonnegative_int(
            usage.get("cache_creation_input_tokens", usage.get("cache_creation_tokens", 0))
        )

    def add(self, other: Usage) -> None:
        for key in TOKEN_KEYS:
            setattr(self, key, getattr(self, key) + getattr(other, key))

    def as_dict(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in TOKEN_KEYS}


def _nonnegative_int(value: Any) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(result, 0)


def _ledger_path(session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError("session_id has no safe characters")
    return LEDGER_ROOT / f"{safe}.jsonl"


def record_hook_event(payload: dict[str, Any]) -> None:
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return
    keep = {
        key: payload.get(key)
        for key in (
            "hook_event_name",
            "session_id",
            "agent_id",
            "agent_type",
            "transcript_path",
            "agent_transcript_path",
        )
        if payload.get(key) is not None
    }
    if not keep.get("agent_id") and not keep.get("agent_type"):
        return
    path = _ledger_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(keep, separators=(",", ":"), sort_keys=True) + "\n"
    # O_APPEND keeps each small event write atomic on normal local filesystems.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _iter_lines(path: Path, warnings: list[str]) -> Iterable[tuple[int, str]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        warnings.append(f"read_error:{path.name}:{exc.__class__.__name__}")
        return
    # Decoded per line rather than by a text-mode reader, so one line of invalid UTF-8 is a warning
    # about that line instead of a UnicodeDecodeError out of the whole read. Shared by every caller that
    # walks a transcript line-by-line, so this is the one place that promise has to hold.
    for lineno, raw_line in enumerate(raw.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            yield lineno, raw_line.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(f"decode_error:{path.name}:{lineno}")


def _iter_jsonl(path: Path, warnings: list[str]) -> Iterable[dict[str, Any]]:
    for lineno, line in _iter_lines(path, warnings):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"malformed_json:{path.name}:{lineno}")
            continue
        if isinstance(value, dict):
            yield value


def resolve_main_transcript(session_id: str | None, transcript: str | None) -> Path:
    """The one on-disk main transcript a session id or an explicit path names.

    Takes one of the two. No match, or more than one, is an error naming what was found.
    """
    if session_id and transcript:
        raise ValueError("provide --session-id or --transcript, not both")
    if transcript:
        path = Path(transcript).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"transcript not found: {path}")
        return path
    if not session_id:
        raise ValueError("provide --session-id or --transcript")
    roots = [Path.home() / ".claude" / "projects"]
    matches: list[Path] = []
    for root in roots:
        if root.exists():
            matches.extend(root.rglob(f"{session_id}.jsonl"))
    matches = sorted({p.resolve() for p in matches})
    if not matches:
        raise FileNotFoundError(f"no Claude transcript found for session {session_id}")
    # Both tools' whole output derives from this path, so a silent pick misreports another session.
    if len(matches) > 1:
        joined = "\n  ".join(str(p) for p in matches)
        raise RuntimeError(f"multiple transcripts found for session {session_id}:\n  {joined}")
    return matches[0]


def _session_id_from_path(main: Path) -> str:
    return main.stem


def _discover_transcripts(main: Path, warnings: list[str]) -> list[Path]:
    main = main.resolve()
    paths = [main]
    session_dir = main.with_suffix("")
    if session_dir.is_dir():
        paths.extend(_walk_jsonl(session_dir, warnings))
    # Older layouts put subagents beside the main file; that directory is project-level, so admit only
    # files whose records claim this session.
    sibling_subagents = main.parent / "subagents"
    if sibling_subagents.is_dir():
        paths.extend(
            p for p in _walk_jsonl(sibling_subagents, warnings) if _claims_session(p, main.stem, warnings)
        )
    return sorted(set(paths), key=lambda p: (p != main, str(p)))


def _walk_jsonl(root: Path, warnings: list[str]) -> list[Path]:
    # `Path.rglob` swallows scandir errors, which turns an unreadable subagent tree into a clean zero.
    def note(error: OSError) -> None:
        warnings.append(f"{error.filename}: {error.strerror}")

    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, onerror=note):
        found.extend(Path(dirpath, name).resolve() for name in filenames if name.endswith(".jsonl"))
    return found


def _claims_session(path: Path, session_id: str, warnings: list[str]) -> bool:
    checked = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                claimed = record.get("sessionId") or record.get("session_id")
                if claimed:
                    return str(claimed) == session_id
                checked += 1
                if checked >= 20:
                    break
    except OSError as exc:
        # Dropping the file is right (nothing here claims the session), but a silent drop is the clean
        # zero `_walk_jsonl`'s `onerror` exists to prevent.
        warnings.append(f"read_error:{path.name}:{exc.__class__.__name__}")
        return False
    # No record claims any session: keep the file rather than silently dropping usage.
    return True


def _load_agent_map(session_id: str) -> tuple[dict[Path, str], dict[str, str]]:
    by_path: dict[Path, str] = {}
    by_id: dict[str, str] = {}
    primary = _ledger_path(session_id)
    paths: list[Path] = [primary] if primary.is_file() else []
    # Some surfaces give an agent Stop hook a subagent-local session_id, so the whole (tiny) ledger
    # directory is a fallback — safe because the mapping is keyed on exact paths.
    if LEDGER_ROOT.is_dir():
        paths.extend(p for p in LEDGER_ROOT.glob("*.jsonl") if p != primary)
    warnings: list[str] = []
    for path in paths:
        for record in _iter_jsonl(path, warnings):
            agent_type = _normalize_agent_type(str(record.get("agent_type") or "").strip())
            agent_id = str(record.get("agent_id") or "").strip()
            if agent_type and agent_id:
                by_id[agent_id] = agent_type
            for key in ("agent_transcript_path", "transcript_path"):
                raw = record.get(key)
                if raw and agent_type:
                    with contextlib.suppress(OSError):
                        by_path[Path(str(raw)).expanduser().resolve()] = agent_type
    return by_path, by_id


def _record_identity(record: dict[str, Any], message: dict[str, Any], path: Path, line_no: int) -> str:
    for value in (
        message.get("id"),
        record.get("requestId"),
        record.get("request_id"),
        record.get("uuid"),
    ):
        if value:
            return str(value)
    return f"{path}:{line_no}"


def _extract_file_usage(
    path: Path,
    *,
    seen: set[str],
    warnings: list[str],
) -> tuple[Usage, dict[str, Usage], str | None, str | None]:
    total = Usage()
    by_model: dict[str, Usage] = defaultdict(Usage)
    inferred_agent_type: str | None = None
    inferred_agent_id: str | None = None

    for line_no, line in _iter_lines(path, warnings):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"malformed_json:{path.name}:{line_no}")
            continue
        if not isinstance(record, dict):
            continue
        inferred_agent_type = inferred_agent_type or _first_string(
            record, "agent_type", "agentType"
        )
        inferred_agent_id = inferred_agent_id or _first_string(
            record, "agent_id", "agentId"
        )
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        identity = _record_identity(record, message, path, line_no)
        if identity in seen:
            continue
        seen.add(identity)
        item = Usage()
        item.add_mapping(usage)
        total.add(item)
        model = str(message.get("model") or record.get("model") or "unknown")
        by_model[model].add(item)
    return total, by_model, inferred_agent_type, inferred_agent_id


def _first_string(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalize_agent_type(agent_type: str | None) -> str | None:
    if not agent_type:
        return agent_type
    # Plugin-namespaced agent types arrive as "<plugin>:<name>" (e.g. sy:gate), but accounting and
    # `require_agent` name agents bare, so strip the namespace before grouping.
    return agent_type.split(":")[-1]


def _refused_handbacks(path: Path, warnings: list[str]) -> tuple[int, str | None, str | None]:
    calls: set[str] = set()
    refused: set[str] = set()
    inferred_agent_type: str | None = None
    inferred_agent_id: str | None = None
    for record in _iter_jsonl(path, warnings):
        inferred_agent_type = inferred_agent_type or _first_string(
            record, "agent_type", "agentType", "attributionAgent"
        )
        inferred_agent_id = inferred_agent_id or _first_string(record, "agent_id", "agentId")
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        for block in _content_blocks(message.get("content")):
            kind = block.get("type")
            if kind == "tool_use" and block.get("name") == "SubagentHandback":
                calls.add(str(block.get("id")))
            elif (
                kind == "tool_result"
                and str(block.get("tool_use_id")) in calls
                and _reports_refusal(block.get("content"))
            ):
                refused.add(str(block.get("tool_use_id")))
    return len(refused), inferred_agent_type, inferred_agent_id


def summarize(
    main: Path,
    *,
    phase: str,
    task: str | None,
) -> dict[str, Any]:
    """The `shipyard.claude_usage.v1` roll-up for one session: totals plus a row per agent and model.

    `main` is the main transcript; the whole subagent tree beneath it is counted with it. A transcript
    that cannot be read, or a line that will not parse, becomes a `warnings` entry, never a failure.
    """
    session_id = _session_id_from_path(main)
    warnings: list[str] = []
    transcripts = _discover_transcripts(main, warnings)
    by_path, by_id = _load_agent_map(session_id)
    seen: set[str] = set()
    total = Usage()
    grouped: dict[tuple[str, str], Usage] = defaultdict(Usage)
    invocations: dict[tuple[str, str], set[Path]] = defaultdict(set)

    main_resolved = main.resolve()
    for path in transcripts:
        file_total, by_model, inferred_type, inferred_id = _extract_file_usage(
            path, seen=seen, warnings=warnings
        )
        total.add(file_total)
        if path == main_resolved:
            agent_type = "main"
        else:
            agent_type = (
                by_path.get(path)
                or (by_id.get(inferred_id) if inferred_id else None)
                or _normalize_agent_type(inferred_type)
                or "unknown_subagent"
            )
        for model, model_usage in by_model.items():
            key = (agent_type, model)
            grouped[key].add(model_usage)
            invocations[key].add(path)

    by_agent = []
    for agent_type, model in sorted(grouped):
        usage = grouped[(agent_type, model)]
        by_agent.append(
            {
                "agent_type": agent_type,
                "model": model,
                "invocations": len(invocations[(agent_type, model)]),
                **usage.as_dict(),
            }
        )

    result: dict[str, Any] = {
        "schema": "shipyard.claude_usage.v1",
        "phase": phase,
        "session_id": session_id,
        "scope": "main_plus_subagents",
        "transcripts": {
            "main": 1,
            "subagents": max(len(transcripts) - 1, 0),
        },
        "totals": total.as_dict(),
        "by_agent": by_agent,
    }
    if task:
        result["task"] = task
    # Always present: an absent key once made an unreadable tree look like a healthy zero.
    result["warnings"] = sorted(set(warnings))
    return result


def handbacks(main: Path) -> dict[str, Any]:
    """The `shipyard.worker_handbacks.v1` report for one session: every refused `SubagentHandback` call.

    `main` is the main transcript; the whole subagent tree beneath it is read with it, because a refusal
    is recorded only in the refused agent's own transcript. A hand-back that was delivered is not
    reported. A transcript that cannot be read, or a line that will not parse, becomes a `warnings`
    entry, never a failure.
    """
    session_id = _session_id_from_path(main)
    warnings: list[str] = []
    transcripts = _discover_transcripts(main, warnings)
    by_path, by_id = _load_agent_map(session_id)
    grouped: dict[tuple[str, str, str], int] = defaultdict(int)
    transcript_of: dict[tuple[str, str, str], str] = {}

    main_resolved = main.resolve()
    for path in transcripts:
        refused, inferred_type, inferred_id = _refused_handbacks(path, warnings)
        if not refused:
            continue
        if path == main_resolved:
            agent_type = "main"
        else:
            agent_type = (
                by_path.get(path)
                or (by_id.get(inferred_id) if inferred_id else None)
                or _normalize_agent_type(inferred_type)
                or "unknown_subagent"
            )
        # Without an agent id the path keys the row: same-typed transcripts would otherwise merge onto
        # one row whose single `transcript` hides every other transcript behind the summed count.
        key = (agent_type, inferred_id or "", "" if inferred_id else str(path))
        grouped[key] += refused
        transcript_of.setdefault(key, str(path))

    by_agent = [
        {
            "agent_type": key[0],
            "agent_id": key[1],
            "refused": refused,
            "transcript": transcript_of[key],
        }
        for key, refused in sorted(grouped.items())
    ]
    result: dict[str, Any] = {
        "schema": "shipyard.worker_handbacks.v1",
        "session_id": session_id,
        "scope": "main_plus_subagents",
        "transcripts": {
            "main": 1,
            "subagents": max(len(transcripts) - 1, 0),
        },
        "refused": sum(grouped.values()),
        "by_agent": by_agent,
        # Always present: an absent key once made an unreadable tree look like a healthy zero.
        "warnings": sorted(set(warnings)),
    }
    return result


def _content_blocks(content: Any) -> Iterable[dict[str, Any]]:
    if isinstance(content, str):
        yield {"type": "text", "text": content}
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _reports_refusal(content: Any) -> bool:
    """Whether a `SubagentHandback` tool result carries a `success: false` payload.

    The payload is JSON inside an MCP content block, so it is parsed: a substring match on one
    serialization misses the same object with a space after the colon or its keys in another order.
    """
    return any(isinstance(payload, dict) and payload.get("success") is False for payload in _payloads(content))


def _payloads(content: Any) -> Iterable[Any]:
    for block in _content_blocks(content):
        yield block
        if block.get("type") == "text":
            yield from _parsed_json(str(block.get("text", "")))
        nested = block.get("content")
        if isinstance(nested, (str, list)):
            yield from _payloads(nested)


def _parsed_json(text: str) -> list[Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else [value]


def main(argv: list[str] | None = None) -> int:
    """Run the `hook` command: append one agent/transcript mapping line from hook JSON on stdin.

    Malformed stdin is a success: exit 0, nothing recorded.
    """
    parser = argparse.ArgumentParser(prog="python -m sy_tools.usage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="record agent/transcript mapping from hook stdin")
    parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
    # A non-zero exit on an unparseable payload would interrupt the session this hook is observing.
    except json.JSONDecodeError:
        return 0
    if isinstance(payload, dict):
        record_hook_event(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
