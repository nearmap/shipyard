#!/usr/bin/env python3
"""Tell the main thread, and the user, when its context is large enough that an idle gap costs real money.

A main-thread request that follows a gap longer than the prompt-cache TTL re-writes its whole context into
cache. Above `RECYCLE_TOKENS` that re-write is the largest avoidable cost of a long session, so:

- PostToolUse (main thread only): the first time the newest main-thread request on disk is in a band
  (`RECYCLE_TOKENS`, then every `BAND_TOKENS` above it), add one line of model context naming the size
  and `skills/shared/references/context-recycle.md`. Each band is announced once per session; a drop
  below `RECYCLE_TOKENS` (a compaction) re-arms them.
- Stop: at or above `RECYCLE_TOKENS`, with no background task still running, show the user one warning
  line naming the TTL of the newest cache write. Transcript drift and an unwritable marker root are
  reported here too.

Commands:
  PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python -m sy_tools.context_watch hook
      Read Claude Code hook JSON from stdin; print hook JSON on stdout when there is something to say.
"""
from __future__ import annotations

# Runs on whatever bare `python` is on `PATH` (3.9 on some machines): stdlib-only, like `usage.py`.
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import sys

RECYCLE_TOKENS = 250_000
BAND_TOKENS = 150_000
HARD_RECYCLE_TOKENS = RECYCLE_TOKENS + BAND_TOKENS
# Enough tail to hold the last few main-thread requests; one tool result can be a few hundred KB.
TAIL_BYTES = 2 * 1024 * 1024
MARKER_ROOT = Path.home() / ".claude" / "shipyard" / "context-watch"


@dataclass
class Request:
    """One main-thread API request, merged across the several records the transcript writes for it."""

    context: int = 0
    wrote_cache: bool = False
    one_hour_ttl: bool = False


def hook_output(event: dict) -> dict | None:
    """The hook JSON to print for one hook event, or None when there is nothing to say."""
    if event.get("agent_id") or not event.get("transcript_path"):
        return None
    name = event.get("hook_event_name")
    requests, problem = recent_requests(Path(event["transcript_path"]))
    if name == "Stop" and not problem:
        problem = marker_root_problem()
    if problem:
        # PostToolUse stays quiet; Stop is the one surface a user sees, so drift is reported there.
        return {"systemMessage": f"sy context_watch is not working: {problem}"} if name == "Stop" else None
    if not requests:
        return None
    current = requests[0]
    if name == "PostToolUse":
        if not claim_band(str(event.get("session_id") or ""), band(current.context)):
            return None
        root = os.environ.get("CLAUDE_PLUGIN_ROOT", "<shipyard plugin root>")
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
            f"sy: main-thread context is about {current.context // 1000}k tokens. "
            f"Apply {root}/skills/shared/references/context-recycle.md at the next phase boundary."
        )}}
    if name == "Stop" and current.context >= RECYCLE_TOKENS and not event.get("background_tasks"):
        newest_write = next((r for r in requests if r.wrote_cache), None)
        ttl = "an hour" if newest_write and newest_write.one_hour_ttl else "5 minutes"
        return {"systemMessage": (
            f"Context is about {current.context // 1000}k tokens. After a gap of more than {ttl}, your next "
            "message re-writes all of it into cache. Stepping away from /sy:ship, /sy:spec, /sy:spike or /sy:mvp? "
            "Ask for a checkpoint first, then /clear and resume."
        )}
    return None


def band(tokens: int) -> int:
    """0 below `RECYCLE_TOKENS`, then 1, 2, ... for each `BAND_TOKENS` step at or above it."""
    if tokens < RECYCLE_TOKENS:
        return 0
    return 1 + (tokens - RECYCLE_TOKENS) // BAND_TOKENS


def marker_root_problem() -> str | None:
    """Why `claim_band` cannot record a band, or None when it can."""
    try:
        MARKER_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot write {MARKER_ROOT} ({exc}), so no context line reaches the model"
    if not os.access(MARKER_ROOT, os.W_OK):
        return f"cannot write {MARKER_ROOT}, so no context line reaches the model"
    return None


def claim_band(session_id: str, level: int) -> bool:
    """True exactly once per session for each band above 0; band 0 clears the session's claims."""
    if not session_id:
        return False
    directory = MARKER_ROOT / re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    if level == 0:
        shutil.rmtree(directory, ignore_errors=True)
        return False
    # An exclusive create, not a transcript read: the transcript lags the hook, and parallel calls race.
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.close(os.open(directory / f"band-{level}", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    except OSError:
        return False  # an unwritable root is reported on Stop; a per-session create failure only loses the line
    return True


def recent_requests(path: Path) -> tuple[list[Request], str | None]:
    """Main-thread requests in the transcript tail, newest first, and what stopped them being read, if anything."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], f"cannot read the transcript ({exc})"
    order: list[str] = []
    by_id: dict[str, Request] = {}
    saw_main_assistant = False
    for raw in reversed(lines):
        if b'"assistant"' not in raw:
            continue
        try:
            record = json.loads(raw)
        # A tail read starts mid-record, sometimes mid-character: UnicodeDecodeError is a ValueError.
        except ValueError:
            continue
        message = record.get("message") or {}
        if record.get("type") != "assistant" or record.get("isSidechain") or message.get("model") == "<synthetic>":
            continue
        saw_main_assistant = True
        rid = record.get("requestId")
        if not rid:
            continue
        usage = message.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        request = by_id.get(rid)
        if request is None:
            request = by_id[rid] = Request()
            order.append(rid)
        # Both spellings occur across Claude Code surfaces, as `usage.py` notes.
        request.context = max(request.context, int(usage.get("input_tokens") or 0)
                              + int(usage.get("cache_read_input_tokens", usage.get("cache_read_tokens")) or 0)
                              + int(usage.get("cache_creation_input_tokens", usage.get("cache_creation_tokens")) or 0))
        cache = usage.get("cache_creation") or {}
        one_hour, five_min = cache.get("ephemeral_1h_input_tokens"), cache.get("ephemeral_5m_input_tokens")
        if not request.wrote_cache and (one_hour or five_min):
            request.wrote_cache, request.one_hour_ttl = True, bool(one_hour)
    requests = [by_id[rid] for rid in order]
    if saw_main_assistant and not requests:
        return [], "no main-thread request with a requestId in the transcript tail; its format may have changed"
    if requests and requests[0].context == 0:
        return [], "the newest request carries no token usage; the usage keys may have changed"
    return requests, None


def main() -> int:
    """Run the `hook` command."""
    if (sys.argv[1] if len(sys.argv) > 1 else None) != "hook":
        print("usage: python -m sy_tools.context_watch hook", file=sys.stderr)
        return 2
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0  # a broken payload must never interrupt the session
    out = hook_output(event) if isinstance(event, dict) else None
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
