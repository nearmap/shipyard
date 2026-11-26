"""Build one row per Claude Code session from local transcripts: the session table `/sy:retro` discovery reads.

Each row folds the session's subagent tree into the main transcript's row and keeps the raw link evidence
(branches, commit SHAs, PR URLs, issue keys) a later step joins to PR outcomes. Nothing here calls a remote host.

  python -m sy_tools.sessions --since 2026-01-01 --out sessions.jsonl
"""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from .usage import summarize

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
ACTIVE_GAP = timedelta(minutes=15)
# Host-neutral: any `https://<host>/<owner>/<repo>/pull/<n>`, so an enterprise host links the same way.
PR_URL = re.compile(r"https://[\w.-]+/[\w.-]+/[\w.-]+/pull/\d+")
PR_CREATE = re.compile(r"\bpr create\b")
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")
# `git commit` prints `[branch sha] subject`, or `[branch (root-commit) sha] subject` for a first commit.
COMMIT_BRACKET = re.compile(r"^\[[^\]\n]*?\b([0-9a-f]{7,40})\]", re.M)
LEADING_SHA = re.compile(r"^([0-9a-f]{7,40})(?:\s|$)", re.M)
WRAPPER = re.compile(r"<(local-command-[\w-]+|command-[\w-]+|system-reminder|task-notification)>.*?</\1>", re.S)
INTERRUPT = "[Request interrupted by user"
USER_DENIAL = "The user doesn't want to"
AGENT_TOOLS = {"Agent", "Task"}
_ToolUses = dict[str, tuple[str, dict[str, Any]]]


def collect(projects_root: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """One row per main transcript `<projects_root>/<project-dir>/<session-id>.jsonl`, oldest first.

    A session whose last record is before `since` is left out; its file mtime is checked before it is read.
    """
    rows = []
    for main in sorted(projects_root.glob("*/*.jsonl")):
        if since is not None and datetime.fromtimestamp(main.stat().st_mtime, UTC) < since:
            continue
        row = session_row(main)
        if row["ended_at"] is None or (since is not None and _parse_ts(row["ended_at"]) < since):
            continue
        rows.append(row)
    rows.sort(key=lambda r: r["started_at"])
    return rows


def session_row(main: Path) -> dict[str, Any]:
    """The session-table row for one main transcript and the subagent tree under `<session-id>/`."""
    scan = _Scan()
    scan.read(main, is_main=True)
    session_dir = main.with_suffix("")
    for path in _tree_files(session_dir, ".jsonl"):
        scan.read(path, is_main=False)
    spawned: Counter[str] = Counter()
    max_depth = 0
    for meta_path in _tree_files(session_dir, ".meta.json"):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        spawned[meta["agentType"]] += 1
        max_depth = max(max_depth, int(meta.get("spawnDepth") or 0))

    usage = summarize(main, phase="retro", task=None, agent_ledger=False)
    by_model: dict[str, Counter[str]] = {}
    for item in usage["by_agent"]:
        tokens = by_model.setdefault(item["model"], Counter())
        tokens.update({k: item[k] for k in usage["totals"]})

    stamps = sorted(scan.timestamps)
    active = sum((b - a for a, b in pairwise(stamps) if b - a < ACTIVE_GAP), timedelta())
    branches = sorted(scan.branches - {"HEAD", ""})
    created = sorted(scan.pr_created)
    return {
        "session_id": main.stem,
        "project_dir": main.parent.name,
        "cwd": scan.cwd,
        "started_at": _iso(stamps[0]) if stamps else None,
        "ended_at": _iso(stamps[-1]) if stamps else None,
        "active_minutes": round(active.total_seconds() / 60, 1),
        "usage": {"totals": usage["totals"], "by_model": {m: dict(t) for m, t in sorted(by_model.items())}},
        "skills": {"slash": dict(scan.slash), "tool": dict(scan.skill_tool)},
        "agents": {"dispatched": dict(scan.dispatched), "spawned": dict(spawned), "max_spawn_depth": max_depth},
        "interventions": {
            "user_turns": scan.user_turns,
            "interrupts": scan.interrupts,
            "ask_user_question": scan.asks,
            "ask_answers": scan.ask_answers,
            "permission_denials": scan.denials,
        },
        "tool_errors": dict(scan.tool_errors),
        "compactions": scan.compactions,
        "links": {
            "branches": branches,
            "commits": sorted(scan.commits),
            "pr_urls_created": created,
            "pr_urls_mentioned": sorted(scan.pr_mentioned - scan.pr_created),
            "pr_urls_linked": sorted(scan.pr_linked),
            "issue_keys": sorted({key for b in branches for key in ISSUE_KEY.findall(b)}),
        },
        "warnings": scan.warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", type=lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    rows = collect(DEFAULT_PROJECTS_ROOT, args.since)
    with args.out.open("w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row) + "\n" for row in rows)
    span = f"{rows[0]['started_at']}..{rows[-1]['ended_at']}" if rows else "none"
    print(f"sessions={len(rows)} span={span} elapsed={time.monotonic() - started:.1f}s out={args.out}")
    return 0


@dataclass
class _Scan:
    timestamps: list[datetime] = field(default_factory=list)
    cwd: str | None = None
    branches: set[str] = field(default_factory=set)
    slash: Counter[str] = field(default_factory=Counter)
    skill_tool: Counter[str] = field(default_factory=Counter)
    dispatched: Counter[str] = field(default_factory=Counter)
    tool_errors: Counter[str] = field(default_factory=Counter)
    user_turns: int = 0
    interrupts: int = 0
    asks: int = 0
    ask_answers: int = 0
    denials: int = 0
    compactions: int = 0
    commits: set[str] = field(default_factory=set)
    pr_created: set[str] = field(default_factory=set)
    pr_mentioned: set[str] = field(default_factory=set)
    pr_linked: set[str] = field(default_factory=set)
    warnings: int = 0

    def read(self, path: Path, *, is_main: bool) -> None:
        # Tool-use ids are per transcript, so the id -> (tool, input) map is too.
        tool_uses: _ToolUses = {}
        for record in self._records(path):
            if is_main:
                self.timestamps.append(_parse_ts(record["timestamp"]))
                self.cwd = self.cwd or record.get("cwd")
            if record.get("gitBranch"):
                self.branches.add(record["gitBranch"])
            kind = record["type"]
            if kind == "pr-link":
                self.pr_linked.add(record["prUrl"])
            elif kind == "assistant":
                self._assistant(record["message"]["content"], tool_uses, is_main=is_main)
            elif kind == "user":
                self._user(record, tool_uses, is_main=is_main)

    def _records(self, path: Path) -> Iterator[dict[str, Any]]:
        with path.open("rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.warnings += 1
                    continue
                if record.get("type") == "api-request-blob" or "timestamp" not in record:
                    continue
                yield record

    def _assistant(self, content: Any, tool_uses: _ToolUses, *, is_main: bool) -> None:
        for block in _blocks(content):
            if block.get("type") != "tool_use":
                continue
            name, tool_input = block["name"], block.get("input") or {}
            tool_uses[block["id"]] = (name, tool_input)
            self.pr_mentioned.update(url for s in _strings(tool_input) for url in PR_URL.findall(s))
            if name == "Skill" and tool_input.get("skill"):
                self.skill_tool[tool_input["skill"]] += 1
            elif name in AGENT_TOOLS and is_main:
                self.dispatched[tool_input.get("subagent_type") or "general-purpose"] += 1
            elif name == "AskUserQuestion" and is_main:
                self.asks += 1

    def _user(self, record: dict[str, Any], tool_uses: _ToolUses, *, is_main: bool) -> None:
        content = record["message"]["content"]
        texts: list[str] = [content] if isinstance(content, str) else []
        for block in _blocks(content):
            if block.get("type") == "text":
                texts.append(block["text"])
            elif block.get("type") == "tool_result":
                self._tool_result(block, tool_uses, is_main=is_main)
        if not is_main:
            return
        if record.get("isCompactSummary"):
            self.compactions += 1
            return
        text = "\n".join(texts)
        self.slash.update(name.lstrip("/") for name in re.findall(r"<command-name>([^<]*)</command-name>", text))
        if INTERRUPT in text:
            self.interrupts += 1
            return
        origin = record.get("origin")
        machine_origin = isinstance(origin, dict) and origin.get("kind") not in (None, "human")
        if not record.get("isMeta") and not machine_origin and WRAPPER.sub("", text).strip():
            self.user_turns += 1

    def _tool_result(self, block: dict[str, Any], tool_uses: _ToolUses, *, is_main: bool) -> None:
        name, tool_input = tool_uses.get(block["tool_use_id"], ("unknown", {}))
        text = _result_text(block.get("content"))
        urls = set(PR_URL.findall(text))
        self.pr_mentioned.update(urls)
        if block.get("is_error"):
            self.tool_errors[name] += 1
            if is_main and text.startswith(USER_DENIAL):
                self.denials += 1
        elif name == "AskUserQuestion" and is_main:
            self.ask_answers += 1
        if name != "Bash":
            return
        command = str(tool_input.get("command", ""))
        if PR_CREATE.search(command):
            self.pr_created.update(urls)
        if "git commit" in command and not block.get("is_error"):
            shas = COMMIT_BRACKET.findall(text)
            # `git commit -q && git log --oneline -1` prints no bracket; its first line-leading SHA is the commit.
            leading = LEADING_SHA.search(text)
            self.commits.update(shas or ([leading.group(1)] if leading else []))


def _blocks(content: Any) -> Iterable[dict[str, Any]]:
    if isinstance(content, list):
        yield from (block for block in content if isinstance(block, dict))


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in _blocks(content))


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _tree_files(root: Path, suffix: str) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(Path(d, n) for d, _, names in os.walk(root) for n in names if n.endswith(suffix))


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    sys.exit(main())
