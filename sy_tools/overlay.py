#!/usr/bin/env python3
"""Shape check for a repository's `.shipyard/severity-overlay.yaml`, the contract in `docs/severity-overlay.md`.

Commands:
  pixi run --manifest-path "${CLAUDE_PLUGIN_ROOT}/pyproject.toml" overlay check <absolute path>
      Exit 0 if the overlay is well formed, 1 if malformed (one stderr line per failed rule), 2 if missing.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import yaml
import yaml.constructor

GRADES = ("HIGH", "MED", "LOW", "NIT")
ENTRY_KEYS = {"scale_mapping": ("source", "label", "grade"), "pinned_patterns": ("grade", "pattern", "recognise_by")}
TOP_KEYS = (*ENTRY_KEYS, "notes")


def check(text: str) -> list[str]:
    """Return one failure line per failed rule 2-4 of § What START checks; empty means well formed."""
    try:
        overlay = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        return [f"rule 2: not valid YAML: {' '.join(str(error).split())}"]
    if not isinstance(overlay, dict) or set(overlay) != set(TOP_KEYS):
        found = ", ".join(map(str, overlay)) if isinstance(overlay, dict) else type(overlay).__name__
        return [f"rule 2: expected a mapping with exactly the keys {', '.join(TOP_KEYS)}; found {found}"]
    wrong_types = [f"{key} is not a list" for key in ENTRY_KEYS if not isinstance(overlay[key], list)]
    if not isinstance(overlay["notes"], str):
        wrong_types.append("notes is not a string")
    if wrong_types:
        return ["rule 2: " + "; ".join(wrong_types)]
    entries = [(key, n, entry) for key in ENTRY_KEYS for n, entry in enumerate(overlay[key], start=1)]
    malformed = [
        f"{key} entry {n} needs exactly {', '.join(ENTRY_KEYS[key])}, each a non-empty string"
        for key, n, entry in entries
        if not _is_entry(entry, ENTRY_KEYS[key])
    ]
    bad_grades = [
        f"{key} entry {n} grade is {entry['grade']!r}"
        for key, n, entry in entries
        if isinstance(entry, dict) and "grade" in entry and entry["grade"] not in GRADES
    ]
    failures = ["rule 3: " + "; ".join(malformed)] if malformed else []
    if bad_grades:
        failures.append(f"rule 4: {'; '.join(bad_grades)} (expected one of {', '.join(GRADES)})")
    return failures


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; see the module docstring for the exit codes."""
    parser = argparse.ArgumentParser(prog="python -m sy_tools.overlay")
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="check a severity overlay's shape")
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


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
        keys = [self.construct_object(key, deep=True) for key, _ in node.value]
        for key in keys:
            if keys.count(key) > 1:
                raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", node.start_mark)
        return super().construct_mapping(node, deep)


def _is_entry(entry: object, keys: tuple[str, ...]) -> bool:
    return (
        isinstance(entry, dict)
        and set(entry) == set(keys)
        and all(isinstance(value, str) and value.strip() for value in entry.values())
    )


if __name__ == "__main__":
    raise SystemExit(main())
