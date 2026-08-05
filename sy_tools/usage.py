#!/usr/bin/env python3
"""Aggregate Claude Code token usage across a session and all subagent transcripts.

The parser is deliberately tolerant of transcript schema additions. It counts API usage
blocks attached to assistant messages, de-duplicates by message/request id, and scans the
main transcript plus the documented nested subagent transcript tree.

Two surfaces over one implementation. `summarize()` and `render()` are the portable subset the `sy`
MCP server exposes as the `usage_summarize` and `export_transcript` tools, which is how a workflow
reaches them; `summarize`'s output is compact JSON suitable for a small standalone tracker comment.
The one command left is the hook:

  PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python -m sy_tools.usage hook
      Read Claude Code hook JSON from stdin and record agent-id/type/transcript mapping.

That hook runs on bare `python` with no environment of its own, so **this module's import graph must
stay standard library only** — `sy_tools.config` is admissible because it is stdlib-only too, and
nothing here may reach the MCP server or anything the server needs. The dependency runs one way:
`server.py` imports this module, never the reverse. `sy_tools/guards/secret_guard.py` is the sibling
under the same constraint.
"""
from __future__ import annotations

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
        # Claude/Anthropic usage fields have used both cache_*_input_tokens and
        # cache_*_tokens spellings across surfaces. Accept either without double-counting.
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


def _iter_jsonl(path: Path, warnings: list[str]) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    warnings.append(f"malformed_json:{path.name}:{lineno}")
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError as exc:
        warnings.append(f"read_error:{path.name}:{exc.__class__.__name__}")


def resolve_main_transcript(session_id: str | None, transcript: str | None) -> Path:
    """The one on-disk main transcript a session id or an explicit path names.

    Takes exactly one of the two. A session id that matches no transcript, or more than one, is an
    error naming what was found rather than a silently-picked file: both tools' whole output is
    derived from this path, so guessing here would misreport somebody else's session.
    """
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
    if len(matches) > 1:
        joined = "\n  ".join(str(p) for p in matches)
        raise RuntimeError(f"multiple transcripts found for session {session_id}:\n  {joined}")
    return matches[0]


def _session_id_from_path(main: Path) -> str:
    return main.stem


def _discover_transcripts(main: Path) -> list[Path]:
    main = main.resolve()
    paths = [main]
    session_dir = main.with_suffix("")
    if session_dir.is_dir():
        paths.extend(p.resolve() for p in session_dir.rglob("*.jsonl"))
    # Be tolerant of older/local layouts that place subagents beside the main file. That
    # directory is project-level, so admit only files whose records claim this session.
    sibling_subagents = main.parent / "subagents"
    if sibling_subagents.is_dir():
        paths.extend(
            p.resolve()
            for p in sibling_subagents.rglob("*.jsonl")
            if _claims_session(p, main.stem)
        )
    return sorted(set(paths), key=lambda p: (p != main, str(p)))


def _claims_session(path: Path, session_id: str) -> bool:
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
    except OSError:
        return False
    # No record claims any session: keep the file rather than silently dropping usage.
    return True


def _load_agent_map(session_id: str) -> tuple[dict[Path, str], dict[str, str]]:
    by_path: dict[Path, str] = {}
    by_id: dict[str, str] = {}
    primary = _ledger_path(session_id)
    paths: list[Path] = [primary] if primary.is_file() else []
    # Some Claude Code versions/surfaces may expose a subagent-local session_id to
    # an agent Stop hook. Exact transcript paths make it safe to scan the tiny
    # mapping ledger directory as a fallback.
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

    try:
        fh = path.open("r", encoding="utf-8")
    except OSError as exc:
        warnings.append(f"read_error:{path.name}:{exc.__class__.__name__}")
        return total, by_model, inferred_agent_type, inferred_agent_id

    with fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
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
    # Plugin-namespaced agent types arrive as "<plugin>:<name>" (e.g. sy:gate). Accounting and
    # --require-agent refer to agents by bare name, so strip the namespace before grouping.
    return agent_type.split(":")[-1]


def summarize(
    main: Path,
    *,
    phase: str,
    task: str | None,
) -> dict[str, Any]:
    session_id = _session_id_from_path(main)
    transcripts = _discover_transcripts(main)
    by_path, by_id = _load_agent_map(session_id)
    warnings: list[str] = []
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
    if warnings:
        result["warnings"] = sorted(set(warnings))
    return result


# ---- readable transcript rendering (replaces the manual /export step) ----

_DEFAULT_RENDER_LIMITS = {"tool_input": 1500, "tool_result": 4000, "thinking": 1200}
_RENDER_LIMITS: dict[str, int] | None = None


def render_limits() -> dict[str, int]:
    """Per-block character limits for transcript rendering, from `transcript.truncation_limits`.

    Resolved once per process and cached. `sy_tools.config._flatten()` only ever stores leaf keys, so
    `transcript.truncation_limits` as a whole path is never resolvable — each of its three fields
    must be fetched individually. A repo whose config can't be resolved at all (run outside a
    checkout, a corrupted layer), or whose resolved value isn't actually numeric (a hand-edited
    layer bypassing `validate`), falls back to the shipped defaults rather than crashing a render
    that's usually happening late in a session.

    The import stays inside the function so the `hook` command — the hot path, one process per Stop
    and SubagentStop firing — never pays for `sy_tools.config` at all: it records a ledger line and
    resolves nothing. `ConfigError` is the shape every `sy_tools.config` refusal takes, including the
    environment faults (an unrunnable `git`, an unreadable layer) that `scripts/sy_config.py` used to
    raise `SystemExit` for, so it is what this degradation catches.
    """
    global _RENDER_LIMITS
    if _RENDER_LIMITS is None:
        try:
            from .config import ConfigError
            from .config import get as config_get
            _RENDER_LIMITS = {
                # `get()` is typed as returning `object` because a layer can hold anything, and calling
                # int() on that unchecked is the point rather than a gap: a non-numeric value raises
                # straight into the fallback below instead of rendering with a nonsense limit.
                key: int(config_get(f"transcript.truncation_limits.{key}"))  # ty: ignore[invalid-argument-type]
                for key in _DEFAULT_RENDER_LIMITS
            }
        except (ConfigError, ValueError, TypeError):
            _RENDER_LIMITS = dict(_DEFAULT_RENDER_LIMITS)
    return _RENDER_LIMITS


def _truncate(text: str, limit: int) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more chars truncated ...]"


def _content_blocks(content: Any) -> Iterable[dict[str, Any]]:
    if isinstance(content, str):
        yield {"type": "text", "text": content}
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in _content_blocks(content):
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "image":
            parts.append("[image omitted]")
        else:
            parts.append(json.dumps(block, separators=(",", ":")))
    return "\n".join(parts)


def _indent(text: str) -> str:
    return "  " + text.replace("\n", "\n  ")


def _render_row(
    record: dict[str, Any], tool_names: dict[str, str], pending_interjections: dict[str, int], lines: list[str]
) -> None:
    ts = str(record.get("timestamp") or "")[:19].replace("T", " ")
    if record.get("type") == "queue-operation":
        text = record.get("content")
        if isinstance(text, str) and text.strip():
            key = text.strip()
            if record.get("operation") == "enqueue":
                pending_interjections[key] = pending_interjections.get(key, 0) + 1
                lines.append(f"[{ts}] USER (queued interjection)")
                lines.append(text.rstrip())
            elif record.get("operation") == "remove" and pending_interjections.get(key):
                pending_interjections[key] -= 1
                lines.append(f"[{ts}] (queued interjection above was cancelled before delivery)")
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if record.get("type") == "assistant":
        for block in _content_blocks(content):
            kind = block.get("type")
            if kind == "text" and str(block.get("text", "")).strip():
                lines.append(f"[{ts}] ASSISTANT")
                lines.append(str(block["text"]).rstrip())
            elif kind == "thinking" and str(block.get("thinking", "")).strip():
                lines.append(f"[{ts}] (thinking)")
                lines.append(_indent(_truncate(str(block["thinking"]), render_limits()["thinking"])))
            elif kind == "tool_use":
                name = str(block.get("name", "?"))
                tool_names[str(block.get("id"))] = name
                args = _truncate(json.dumps(block.get("input", {}), indent=2), render_limits()["tool_input"])
                lines.append(f"[{ts}] TOOL CALL {name}")
                lines.append(_indent(args))
    elif record.get("type") == "user":
        for block in _content_blocks(content):
            kind = block.get("type")
            if kind == "tool_result":
                name = tool_names.get(str(block.get("tool_use_id")), "?")
                body = _truncate(_tool_result_text(block.get("content")), render_limits()["tool_result"])
                lines.append(f"[{ts}] RESULT ({name})")
                lines.append(_indent(body))
            elif kind == "text" and str(block.get("text", "")).strip():
                text = str(block["text"])
                if pending_interjections.get(text.strip()):
                    pending_interjections[text.strip()] -= 1
                    continue
                lines.append(f"[{ts}] USER")
                lines.append(text.rstrip())


def _first_timestamp(path: Path) -> str:
    warnings: list[str] = []
    for record in _iter_jsonl(path, warnings):
        ts = record.get("timestamp")
        if ts:
            return str(ts)
    return ""


def _agent_header(path: Path, by_path: dict[Path, str]) -> str:
    agent_type = by_path.get(path.resolve(), "")
    description = ""
    meta = path.with_suffix(".meta.json")
    if meta.is_file():
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            agent_type = agent_type or str(info.get("agentType", ""))
            description = str(info.get("description", ""))
        except (OSError, json.JSONDecodeError):
            pass
    tail = f": {description}" if description else ""
    return f"SUBAGENT {agent_type or 'subagent'}{tail}  [{path.name}]"


def render(main: Path, *, task: str | None) -> str:
    session_id = _session_id_from_path(main)
    transcripts = _discover_transcripts(main)
    by_path, _ = _load_agent_map(session_id)
    main_resolved = main.resolve()
    subs = sorted(
        (p for p in transcripts if p != main_resolved),
        key=lambda p: (_first_timestamp(p), str(p)),
    )

    out: list[str] = ["# Claude Code session transcript", f"session_id: {session_id}"]
    if task:
        out.append(f"task: {task}")
    out.append(f"transcripts: 1 main + {len(subs)} subagents")
    out.append("")

    sections = [(main_resolved, f"MAIN SESSION {session_id}")]
    sections += [(p, _agent_header(p, by_path)) for p in subs]
    for path, header in sections:
        out += ["=" * 78, header, "=" * 78]
        tool_names: dict[str, str] = {}
        pending_interjections: dict[str, int] = {}
        warnings: list[str] = []
        for record in _iter_jsonl(path, warnings):
            _render_row(record, tool_names, pending_interjections, out)
        out.append("")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run the `hook` command: append one agent/transcript mapping line from hook JSON on stdin.

    The only command this module still has. `summarize` and `export` were subcommands here and are now
    the `usage_summarize` and `export_transcript` MCP tools, so each mechanism has exactly one path to
    it and nothing shells out to this module except the hook. Malformed stdin is a success: a hook that
    exits non-zero on a payload it merely could not parse would interrupt the session it is observing.
    """
    parser = argparse.ArgumentParser(prog="python -m sy_tools.usage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="record agent/transcript mapping from hook stdin")
    parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if isinstance(payload, dict):
        record_hook_event(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
