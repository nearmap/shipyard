"""Durable cross-session memory: tool/skill-level lessons that outlive one repo and one session.

The store is a directory of Markdown files under the user-global root the resolver reports for
`memory.dir` — one file per lesson, its name the lesson title as a kebab-slug, holding a short
frontmatter block (title, scope, tags, date) and the body. Beside them sits a greppable `index.md`,
regenerated on every write and rebuilt on read whenever it disagrees with the lessons on disk, so a
lesson deleted by hand never reads back as a ghost entry. Writes are idempotent by title: re-adding
a lesson under a title already stored replaces that one file rather than adding a second copy of it.

The write/read discipline (when a lesson is worth storing, who reads it back) lives in
skills/shared/references/memory.md.
"""
from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import re

from .config import get as config_get

INDEX_NAME = "index.md"


def root() -> Path:
    """Resolve the storage root from `memory.dir`, which the resolver defaults per user."""
    return Path(str(config_get("memory.dir")))


def add(title: str, scope: str, tags: str, body: str) -> Path:
    """Store one lesson and regenerate the index; same-title re-adds replace the entry."""
    if not title.strip() or not scope.strip() or not body.strip():
        raise ValueError("title, scope, and body must all be non-empty")
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
        raise ValueError("search term must be non-empty")
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


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        raise ValueError(f"title {title!r} yields an empty slug")
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
