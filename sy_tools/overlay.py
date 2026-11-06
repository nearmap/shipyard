#!/usr/bin/env python3
"""Structure check for a repository's `.shipyard/severity-overlay.md`, the contract in `docs/severity-overlay.md`.

Commands:
  PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python -m sy_tools.overlay check <path>
      Exit 0 if the overlay is well formed, 1 if malformed (one stderr line per failed rule), 2 if missing.
"""
from __future__ import annotations

# START runs this on whatever bare `python` is on `PATH` (3.9 on some machines), so it stays stdlib-only
# and must import on 3.9; `sy_tools/eval_events.py` and `sy_tools/usage.py` are under the same constraint.
import argparse
from pathlib import Path
import re
import sys

GRADES = ("HIGH", "MED", "LOW", "NIT")
SECTIONS = ("Scale mapping", "Pinned patterns", "Notes")
TABLE_HEADERS = {
    "Scale mapping": ["Source", "Label", "Grade"],
    "Pinned patterns": ["Grade", "Pattern", "Recognise by"],
}

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def check(text: str) -> list[str]:
    """Return one failure line per failed rule 2-4 of § What START checks; empty means well formed."""
    sections = _sections(text)
    failures = []
    order_failure = _check_sections(sections)
    if order_failure:
        failures.append(order_failure)
    table_problems: list[str] = []
    grade_problems: list[str] = []
    for name, body in sections:
        expected = TABLE_HEADERS.get(name)
        if expected is None:
            continue
        tables = _tables(body)
        table_problems.extend(_check_tables(name, tables, expected))
        grade_problems.extend(_check_grades(name, tables))
    if table_problems:
        failures.append("rule 3: " + "; ".join(table_problems))
    if grade_problems:
        failures.append("rule 4: " + "; ".join(grade_problems))
    return failures


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; see the module docstring for the exit codes."""
    parser = argparse.ArgumentParser(prog="python -m sy_tools.overlay")
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="check a severity overlay's structure")
    check_parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        text = args.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"{args.path}: rule 1: the file does not exist", file=sys.stderr)
        return 2
    failures = check(text)
    for line in failures:
        print(f"{args.path}: {line}", file=sys.stderr)
    return 1 if failures else 0


def _sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    body: list[str] | None = None
    fence = ""
    for line in text.splitlines():
        fence_match = _FENCE.match(line)
        if fence:
            if fence_match and _closes(fence, fence_match):
                fence = ""
            continue
        if fence_match:
            fence = fence_match.group(1)
            continue
        heading = _HEADING.match(line)
        if heading and len(heading.group(1)) <= 2:
            body = None
            if len(heading.group(1)) == 2:
                body = []
                sections.append((heading.group(2), body))
        elif body is not None:
            body.append(line)
    return sections


def _closes(fence: str, match: re.Match[str]) -> bool:
    run = match.group(1)
    return run[0] == fence[0] and len(run) >= len(fence) and not match.group(2).strip()


def _check_sections(sections: list[tuple[str, list[str]]]) -> str:
    names = [name for name, _ in sections]
    problems = []
    for required in SECTIONS:
        count = names.count(required)
        if count == 0:
            problems.append(f"## {required} is missing")
        elif count > 1:
            problems.append(f"## {required} appears {count} times")
    present = [name for name in names if name in SECTIONS]
    first_seen = list(dict.fromkeys(present))
    if first_seen != [name for name in SECTIONS if name in first_seen]:
        problems.append("sections are out of order: " + ", ".join(f"## {name}" for name in first_seen))
    if not problems:
        return ""
    expected = ", ".join(f"## {name}" for name in SECTIONS)
    return f"rule 2: {'; '.join(problems)} (expected {expected}, each once, in that order)"


def _tables(body: list[str]) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    previous_was_row = False
    for line in body:
        is_row = line.lstrip().startswith("|")
        if is_row:
            if not previous_was_row:
                tables.append([])
            tables[-1].append(_cells(line))
        previous_was_row = is_row
    return tables


def _cells(line: str) -> list[str]:
    row = line.strip().removeprefix("|")
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip() for cell in _UNESCAPED_PIPE.split(row)]


def _check_tables(name: str, tables: list[list[list[str]]], expected: list[str]) -> list[str]:
    if len(tables) != 1:
        return [f"## {name} holds {len(tables)} tables (expected one)"]
    table = tables[0]
    problems = []
    if table[0] != expected:
        problems.append(f"## {name} header is {' | '.join(table[0])!r} (expected {' | '.join(expected)!r})")
    if len(table) < 2 or not all(_SEPARATOR_CELL.match(cell) for cell in table[1]):
        problems.append(f"## {name} table has no separator row under its header")
    return problems


def _check_grades(name: str, tables: list[list[list[str]]]) -> list[str]:
    problems = []
    for table in tables:
        if "Grade" not in table[0]:
            continue
        column = table[0].index("Grade")
        for number, row in enumerate(table[2:], start=1):
            grade = row[column] if column < len(row) else ""
            if grade not in GRADES:
                problems.append(f"## {name} row {number} Grade is {grade!r} (expected one of {', '.join(GRADES)})")
    return problems


if __name__ == "__main__":
    raise SystemExit(main())
