#!/usr/bin/env python3
"""Tell the main thread, and the user, when its context is large enough that an idle gap costs real money.

A main-thread request at context C that follows a gap longer than the prompt-cache TTL re-writes all of C
into cache. Above `RECYCLE_TOKENS` that re-write is the largest avoidable cost of a long session, so:

- PostToolUse (main thread only): when the latest request's context first crosses a band boundary
  (`RECYCLE_TOKENS`, then every `BAND_TOKENS` above it), add one line of model context naming the size
  and the recycle rule in `skills/shared/references/context-recycle.md`.
- Stop: when the context is at or above `RECYCLE_TOKENS`, show the user one warning line.

Stateless: a crossing is read off the transcript's last two main-thread requests, so nothing is stored.

Commands:
  PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python -m sy_tools.context_watch hook
      Read Claude Code hook JSON from stdin; print hook JSON on stdout when there is something to say.
"""
from __future__ import annotations

# Runs on whatever bare `python` is on `PATH` (3.9 on some machines): stdlib-only, like `usage.py`.
import json
import os
from pathlib import Path
import sys

RECYCLE_TOKENS = 250_000
HARD_RECYCLE_TOKENS = 400_000
BAND_TOKENS = 150_000
# Enough tail to hold the last two main-thread requests; one tool result can be a few hundred KB.
TAIL_BYTES = 2 * 1024 * 1024


def hook_output(event: dict) -> dict | None:
    """The hook JSON to print for one hook event, or None when there is nothing to say."""
    if event.get("agent_id"):
        return None
    path = event.get("transcript_path")
    if not path:
        return None
    sizes = last_context_sizes(Path(path))
    if not sizes:
        return None
    current = sizes[-1]
    name = event.get("hook_event_name")
    if name == "PostToolUse":
        previous = sizes[-2] if len(sizes) > 1 else 0
        if band(current) <= band(previous):
            return None
        root = os.environ.get("CLAUDE_PLUGIN_ROOT", "<shipyard plugin root>")
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
            f"sy: main-thread context is about {current // 1000}k tokens (recycle at {RECYCLE_TOKENS // 1000}k, "
            f"spike/mvp hard recycle at {HARD_RECYCLE_TOKENS // 1000}k). At the next phase boundary apply "
            f"{root}/skills/shared/references/context-recycle.md."
        )}}
    if name == "Stop" and current >= RECYCLE_TOKENS:
        return {"systemMessage": (
            f"Context is about {current // 1000}k tokens. After a gap of more than 5 minutes your next message "
            "re-writes all of it into cache. Stepping away? Ask for a checkpoint first, then /clear and resume."
        )}
    return None


def band(tokens: int) -> int:
    """0 below `RECYCLE_TOKENS`, then 1, 2, ... for each `BAND_TOKENS` step at or above it."""
    if tokens < RECYCLE_TOKENS:
        return 0
    return 1 + (tokens - RECYCLE_TOKENS) // BAND_TOKENS


def last_context_sizes(path: Path) -> list[int]:
    """Context sizes of the transcript's last two distinct main-thread requests, oldest first."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            lines = fh.read().splitlines()
    except OSError:
        return []
    sizes: list[int] = []
    seen: set[str] = set()
    for raw in reversed(lines):
        if b'"usage"' not in raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue  # the first line of a tail read is usually cut mid-record
        if record.get("type") != "assistant" or record.get("isSidechain"):
            continue
        rid = record.get("requestId")
        usage = (record.get("message") or {}).get("usage")
        if not rid or rid in seen or not isinstance(usage, dict):
            continue
        seen.add(rid)
        sizes.append(sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
        if len(sizes) == 2:
            break
    return sizes[::-1]


def main() -> int:
    """Run the `hook` command; malformed stdin exits 0 so a broken payload never interrupts the session."""
    if (sys.argv[1] if len(sys.argv) > 1 else None) != "hook":
        print("usage: python -m sy_tools.context_watch hook", file=sys.stderr)
        return 2
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    out = hook_output(event) if isinstance(event, dict) else None
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
