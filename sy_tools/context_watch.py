#!/usr/bin/env python3
"""Tell the main thread, and the user, when its context is large enough that an idle gap costs real money.

A main-thread request that follows a gap longer than the prompt-cache TTL re-writes its whole context into
cache. Above `RECYCLE_TOKENS` that re-write is the largest avoidable cost of a long session, so:

- PostToolUse (main thread only): when the request that issued this tool call is in a higher band than the
  last request that issued one (`RECYCLE_TOKENS`, then every `BAND_TOKENS` above it), add one line of model
  context naming the size and `skills/shared/references/context-recycle.md`. It fires for that request's
  first tool call only, so parallel tool calls add it once.
- Stop: at or above `RECYCLE_TOKENS`, show the user one warning line naming the TTL the session is on.

Stateless: everything is read off the transcript tail, so nothing is stored.

Commands:
  PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python -m sy_tools.context_watch hook
      Read Claude Code hook JSON from stdin; print hook JSON on stdout when there is something to say.
"""
from __future__ import annotations

# Runs on whatever bare `python` is on `PATH` (3.9 on some machines): stdlib-only, like `usage.py`.
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys

RECYCLE_TOKENS = 250_000
BAND_TOKENS = 150_000
HARD_RECYCLE_TOKENS = RECYCLE_TOKENS + BAND_TOKENS
# Enough tail to hold the last few main-thread requests; one tool result can be a few hundred KB.
TAIL_BYTES = 2 * 1024 * 1024


@dataclass
class Request:
    """One main-thread API request, merged across the several records the transcript writes for it."""

    context: int
    tool_use_ids: list[str] = field(default_factory=list)
    one_hour_ttl: bool = False


def hook_output(event: dict) -> dict | None:
    """The hook JSON to print for one hook event, or None when there is nothing to say."""
    if event.get("agent_id") or not event.get("transcript_path"):
        return None
    requests = recent_requests(Path(event["transcript_path"]))
    if not requests:
        return None
    current = requests[0]
    name = event.get("hook_event_name")
    if name == "PostToolUse":
        tool_use_id = event.get("tool_use_id")
        if tool_use_id and current.tool_use_ids and tool_use_id != current.tool_use_ids[0]:
            return None
        previous = next((r for r in requests[1:] if r.tool_use_ids), None)
        if band(current.context) <= band(previous.context if previous else 0):
            return None
        root = os.environ.get("CLAUDE_PLUGIN_ROOT", "<shipyard plugin root>")
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
            f"sy: main-thread context is about {current.context // 1000}k tokens. "
            f"Apply {root}/skills/shared/references/context-recycle.md at the next phase boundary."
        )}}
    if name == "Stop" and current.context >= RECYCLE_TOKENS:
        ttl = "an hour" if any(r.one_hour_ttl for r in requests) else "5 minutes"
        return {"systemMessage": (
            f"Context is about {current.context // 1000}k tokens. After a gap of more than {ttl}, your next "
            "message re-writes all of it into cache. Stepping away? Ask for a checkpoint first, then /clear and resume."
        )}
    return None


def band(tokens: int) -> int:
    """0 below `RECYCLE_TOKENS`, then 1, 2, ... for each `BAND_TOKENS` step at or above it."""
    if tokens < RECYCLE_TOKENS:
        return 0
    return 1 + (tokens - RECYCLE_TOKENS) // BAND_TOKENS


def recent_requests(path: Path) -> list[Request]:
    """Main-thread requests in the transcript tail, newest first; empty when the file cannot be read."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            lines = fh.read().splitlines()
    except OSError:
        return []
    order: list[str] = []
    by_id: dict[str, Request] = {}
    for raw in reversed(lines):
        if b'"requestId"' not in raw:
            continue
        try:
            record = json.loads(raw)
        # A tail read starts mid-record, sometimes mid-character: UnicodeDecodeError is a ValueError.
        except ValueError:
            continue
        message = record.get("message") or {}
        rid = record.get("requestId")
        if record.get("type") != "assistant" or record.get("isSidechain") or not rid:
            continue
        if message.get("model") == "<synthetic>":
            continue
        usage = message.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        request = by_id.get(rid)
        if request is None:
            request = by_id[rid] = Request(context=0)
            order.append(rid)
        request.context = max(request.context, sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
        request.one_hour_ttl |= bool((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens"))
        ids = [b["id"] for b in message.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
        request.tool_use_ids = ids + [i for i in request.tool_use_ids if i not in ids]
    if lines and not order:
        print("context_watch: no main-thread request found in the transcript tail", file=sys.stderr)
    return [by_id[rid] for rid in order]


def main() -> int:
    """Run the `hook` command; malformed stdin exits 0 so a broken payload never interrupts the session."""
    if (sys.argv[1] if len(sys.argv) > 1 else None) != "hook":
        print("usage: python -m sy_tools.context_watch hook", file=sys.stderr)
        return 2
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    out = hook_output(event) if isinstance(event, dict) else None
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
