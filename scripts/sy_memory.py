#!/usr/bin/env python3
"""Durable cross-session memory: tool/skill-level lessons that outlive one repo and one session.

Stores one Markdown file per lesson (kebab-slug name, short frontmatter, body) under the
user-global root the resolver reports for `memory.dir`, plus a greppable
`index.md` regenerated on every write and rebuilt on read when missing or stale. Writes are
idempotent: re-adding a lesson with the same title replaces it, never duplicates it.

Commands:
  add --title "..." --scope <area> [--tags a,b] (--body "..." | --body-file PATH)
  search <term>
  list
  self-test

The write/read discipline (when a lesson is worth storing, who reads it back) lives in
skills/shared/references/memory.md.
"""
from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import re
import sys
from sy_config import get as config_get

INDEX_NAME = "index.md"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "add":
        body = args.body if args.body is not None else _read_body_file(args.body_file)
        path = add(args.title, args.scope, args.tags, body)
        print(f"stored: {path}")
        return 0
    if args.command == "search":
        matches = search(args.term)
        if not matches:
            print(f"no memory entries match {args.term!r} (root: {root()})")
            return 0
        sys.stdout.write("\n".join(matches) + "\n")
        return 0
    if args.command == "list":
        sys.stdout.write(index_text())
        return 0
    _self_test()
    print("sy_memory self-test passed")
    return 0


def root() -> Path:
    """Resolve the storage root from `memory.dir`, which the resolver defaults per user."""
    return Path(str(config_get("memory.dir")))


def add(title: str, scope: str, tags: str, body: str) -> Path:
    """Store one lesson and regenerate the index; same-title re-adds replace the entry."""
    if not title.strip() or not scope.strip() or not body.strip():
        raise SystemExit("sy_memory: title, scope, and body must all be non-empty")
    slug = _slug(title)
    directory = root()
    directory.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\ntitle: {title.strip()}\nscope: {scope.strip()}\ntags: {tags.strip()}\n"
        f"date: {date.today().isoformat()}\n---\n\n{body.strip()}\n"
    )
    _atomic_write(directory / f"{slug}.md", text)
    _rebuild_index(directory)
    return directory / f"{slug}.md"


def search(term: str) -> list[str]:
    """Case-insensitive substring search over all lessons; returns `path: title` lines."""
    if not term.strip():
        raise SystemExit("sy_memory: search term must be non-empty")
    _ensure_index()
    needle = term.lower()
    matches: list[str] = []
    for path in _lesson_paths(root()):
        text = path.read_text(encoding="utf-8")
        if needle in text.lower() or needle in path.stem.lower():
            matches.append(f"{path}: {_title_of(text, path)}")
    return matches


def index_text() -> str:
    """Return the greppable index, rebuilding it first when missing or stale."""
    _ensure_index()
    index = root() / INDEX_NAME
    return index.read_text(encoding="utf-8") if index.is_file() else "# Memory index\n\n(no entries)\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add", help="store one durable lesson (idempotent by title)")
    a.add_argument("--title", required=True, help="one-line lesson; becomes the kebab-slug filename")
    a.add_argument("--scope", required=True, help="where it applies, e.g. a tool, skill, or workflow area")
    a.add_argument("--tags", default="", help="comma-separated tags")
    body = a.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="lesson body text")
    body.add_argument("--body-file", type=Path, help="read the lesson body from a file")
    s = sub.add_parser("search", help="case-insensitive substring search over all lessons")
    s.add_argument("term")
    sub.add_parser("list", help="print the index")
    sub.add_parser("self-test", help="offline round-trip against a temporary root")
    return parser


def _read_body_file(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"sy_memory: body file not found: {path}")
    return path.read_text(encoding="utf-8")


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        raise SystemExit(f"sy_memory: title {title!r} yields an empty slug")
    return slug[:80].rstrip("-")


def _lesson_paths(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.md") if p.name != INDEX_NAME)


def _title_of(text: str, path: Path) -> str:
    match = re.search(r"^title:\s*(.+)$", text, re.M)
    return match.group(1).strip() if match else path.stem


def _frontmatter_value(text: str, key: str) -> str:
    match = re.search(rf"^{key}:\s*(.*)$", text, re.M)
    return match.group(1).strip() if match else ""


def _rebuild_index(directory: Path) -> None:
    lines = ["# Memory index", ""]
    for path in _lesson_paths(directory):
        text = path.read_text(encoding="utf-8")
        scope = _frontmatter_value(text, "scope")
        tags = _frontmatter_value(text, "tags")
        dated = _frontmatter_value(text, "date")
        detail = "; ".join(x for x in (f"scope: {scope}", f"tags: {tags}" if tags else "", dated) if x)
        lines.append(f"- [{path.stem}]({path.name}) — {_title_of(text, path)} ({detail})")
    if len(lines) == 2:
        lines.append("(no entries)")
    _atomic_write(directory / INDEX_NAME, "\n".join(lines) + "\n")


def _ensure_index() -> None:
    directory = root()
    index = directory / INDEX_NAME
    lessons = _lesson_paths(directory)
    if not lessons and not index.is_file():
        return
    entries = len(re.findall(r"^- \[", index.read_text(encoding="utf-8"), re.M)) if index.is_file() else -1
    if entries != len(lessons):
        _rebuild_index(directory)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _self_test() -> None:
    import tempfile

    # The root now comes from the resolver, so the test overrides the resolver rather than an env var.
    original = globals()["config_get"]
    with tempfile.TemporaryDirectory() as tmp:
        globals()["config_get"] = lambda key: tmp if key == "memory.dir" else original(key)
        try:
            first = add("Resume drops the model override", "agent dispatch", "resume,models", "Pass it explicitly.")
            again = add("Resume drops the model override", "agent dispatch", "resume,models", "Pass it explicitly.")
            assert first == again, "same-title re-add must land on the same file"
            add("Review bot login differs per API surface", "code review", "bots", "Match by author type.")
            lessons = _lesson_paths(Path(tmp))
            assert len(lessons) == 2, f"expected 2 lessons after idempotent re-add, got {len(lessons)}"
            hits = search("model override")
            assert len(hits) == 1 and "resume-drops-the-model-override" in hits[0], hits
            (Path(tmp) / INDEX_NAME).unlink()
            listing = index_text()
            assert listing.count("- [") == 2, "index must rebuild on read when missing"
            assert "Review bot login differs per API surface" in listing
            assert search("no-such-token-anywhere") == []
            for lesson in _lesson_paths(Path(tmp)):
                lesson.unlink()
            assert "(no entries)" in index_text(), "stale index must not serve ghost entries after hand-deletion"
            assert search("model override") == []
        finally:
            globals()["config_get"] = original


if __name__ == "__main__":
    raise SystemExit(main())
