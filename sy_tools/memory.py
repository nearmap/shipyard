"""Durable cross-session memory: tool/skill-level lessons that outlive one repo and one session.

The store is a directory of Markdown files under the user-global root the resolver reports for
`memory.dir` — one file per lesson, its name the lesson title as a kebab-slug, holding a short
frontmatter block (title, scope, tags, date, plus status once the lesson has been refuted) and the
body. Beside them sits a greppable `index.md`, regenerated on every write and rebuilt on read
whenever it disagrees with the lessons on disk. Writes are idempotent by title: re-adding or
refuting a lesson under a title already stored replaces that one file rather than adding a second
copy of it, except that a re-add over a title already refuted is refused rather than overwriting it. A
lesson later contradicted is corrected or tombstoned in place, never deleted, so the refuted claim stays
readable rather than being silently re-derived under the same title.

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
REFUTED_HEADING = "## Refuted claim"
TOMBSTONE_BODY = "Refuted: nothing in this lesson still holds. Do not act on it and do not re-derive it."

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def root() -> Path:
    """Resolve the storage root from `memory.dir`, which the resolver defaults per user."""
    return Path(str(config_get("memory.dir")))


def add(title: str, scope: str, tags: str, body: str) -> Path:
    """Store one lesson and regenerate the index; same-title re-adds replace a live entry.

    Raises `ValueError` for an empty `title`/`scope`/`body`, for an interior newline in
    `title`/`scope`/`tags`, and for a title whose stored lesson already carries a refutation.
    """
    if not title.strip() or not scope.strip() or not body.strip():
        raise ValueError("title, scope, and body must all be non-empty")
    _require_single_line(title=title, scope=scope, tags=tags)
    slug = _slug(title)
    directory = root()
    path = directory / f"{slug}.md"
    # The write is unrecoverable (os.replace over the old text), so replacing a live entry is the normal
    # update path but replacing a refutation would destroy its evidence with no signal and no way back.
    if path.is_file():
        stored = path.read_text(encoding="utf-8")
        refuted = _frontmatter_value(stored, "status")
        if refuted:
            # The stored title, not the caller's: two titles can truncate to one slug, so the argument
            # that reached here may not be the title whose refutation is being protected.
            stored_title = _frontmatter_value(stored, "title") or title.strip()
            raise ValueError(
                f"the lesson stored under title {stored_title!r} was refuted (status: {refuted}) and a "
                "plain add() would destroy that refutation and its evidence; this title is closed to "
                "add() for good — read the entry with memory_search, refute() the same title again to "
                "narrow the correction, and give a genuinely new lesson a different title"
            )
    directory.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\ntitle: {title.strip()}\nscope: {scope.strip()}\ntags: {tags.strip()}\n"
        f"date: {date.today().isoformat()}\n---\n\n{body.strip()}\n"
    )
    _atomic_write(path, text)
    _rebuild_index(directory)
    return path


def refute(title: str, evidence: str, correction: str = "") -> Path:
    """Correct or tombstone the stored lesson under `title`, in place; returns its unchanged path.

    A non-empty `correction` narrows the lesson to what still holds (`status: corrected`); an empty one
    tombstones it (`status: tombstoned`). The file is only ever rewritten, never removed, and repeat
    refutes converge on it without re-nesting the preserved pre-refutation claim. Raises `ValueError`
    for an empty `title`/`evidence`, an interior newline in `title`, or a title no lesson is stored under.
    """
    if not title.strip() or not evidence.strip():
        raise ValueError("title and evidence must both be non-empty")
    # Symmetry with add(): this title reaches frontmatter whenever the stored file carries no `---` block.
    _require_single_line(title=title)
    directory = root()
    path = directory / f"{_slug(title)}.md"
    if not path.is_file():
        raise ValueError(f"no lesson is stored under title {title.strip()!r}, so there is nothing to refute")
    text = path.read_text(encoding="utf-8")
    status = "corrected" if correction.strip() else "tombstoned"
    _atomic_write(
        path,
        f"---\ntitle: {_frontmatter_value(text, 'title') or title.strip()}\n"
        f"scope: {_frontmatter_value(text, 'scope')}\ntags: {_frontmatter_value(text, 'tags')}\n"
        f"date: {date.today().isoformat()}\nstatus: {status}\n---\n\n"
        f"{correction.strip() if status == 'corrected' else TOMBSTONE_BODY}\n\n"
        f"Evidence: {evidence.strip()}\n\n{REFUTED_HEADING}\n\n{_refuted_claim(text)}\n",
    )
    _rebuild_index(directory)
    return path


def search(term: str) -> list[str]:
    """Case-insensitive substring search over all lessons; returns `path: title` lines.

    A refuted lesson's line also carries ` (status: corrected|tombstoned)`, so a hit on a claim that no
    longer holds is never mistaken for a live one without opening the file.
    """
    if not term.strip():
        raise ValueError("search term must be non-empty")
    _ensure_index()
    needle = term.lower()
    matches: list[str] = []
    for path in _lesson_paths(root()):
        text = path.read_text(encoding="utf-8")
        if needle in text.lower() or needle in path.stem.lower():
            status = _frontmatter_value(text, "status")
            matches.append(f"{path}: {_title_of(text, path)}" + (f" (status: {status})" if status else ""))
    return matches


def index_text() -> str:
    """Return the greppable index, rebuilding it first when missing or stale."""
    _ensure_index()
    index = root() / INDEX_NAME
    return index.read_text(encoding="utf-8") if index.is_file() else "# Memory index\n\n(no entries)\n"


def _require_single_line(**fields: str) -> None:
    # An interior break truncates the value there and leaves its remainder as an orphan line inside the
    # frontmatter block (spilling into the body would take a literal `\n---\n`); a bare `\r` survives the
    # write verbatim and is translated to `\n` on read, so it corrupts identically. Stripped first, because
    # only the stripped value is ever written: a leading or trailing break is not a corruption to refuse.
    broken = [name for name, value in fields.items() if any(c in value.strip() for c in ("\n", "\r"))]
    if broken:
        raise ValueError(
            f"single-line frontmatter fields must contain no newline: {', '.join(broken)}"
        )


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        raise ValueError(f"title {title!r} yields an empty slug")
    return slug[:80].rstrip("-")


def _lesson_paths(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.md") if p.name != INDEX_NAME)


def _frontmatter(text: str) -> str:
    match = _FRONTMATTER.match(text)
    return match.group(1) if match else ""


def _body(text: str) -> str:
    match = _FRONTMATTER.match(text)
    return text[match.end():].strip() if match else text.strip()


def _refuted_claim(text: str) -> str:
    body = _body(text)
    # `status`, not the heading, decides whether this entry was already refuted: in an unrefuted body that
    # exact line is prose, and partitioning on it would drop everything above. On a repeat refute the first
    # occurrence is always the one refute() wrote, so a claim containing the heading comes back whole.
    if not _frontmatter_value(text, "status"):
        return body
    _, _, kept = body.partition(f"{REFUTED_HEADING}\n")
    return kept.strip() or body


def _title_of(text: str, path: Path) -> str:
    match = re.search(r"^title:[ \t]*(.+)$", _frontmatter(text), re.M)
    return match.group(1).strip() if match else path.stem


def _frontmatter_value(text: str, key: str) -> str:
    # Horizontal whitespace only: `\s*` would cross the newline after an empty `key:` and return the next key's line.
    match = re.search(rf"^{key}:[ \t]*(.*)$", _frontmatter(text), re.M)
    return match.group(1).strip() if match else ""


def _rebuild_index(directory: Path) -> None:
    lines = ["# Memory index", ""]
    for path in _lesson_paths(directory):
        text = path.read_text(encoding="utf-8")
        scope = _frontmatter_value(text, "scope")
        tags = _frontmatter_value(text, "tags")
        dated = _frontmatter_value(text, "date")
        status = _frontmatter_value(text, "status")
        detail = "; ".join(
            x
            for x in (f"scope: {scope}", f"tags: {tags}" if tags else "", dated, f"status: {status}" if status else "")
            if x
        )
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
    if not index.is_file():
        _rebuild_index(directory)
        return
    entries = len(re.findall(r"^- \[", index.read_text(encoding="utf-8"), re.M))
    # Hand-deleting or hand-editing a lesson is unsupported, but tolerating a vanished file, or one edited
    # to a newer mtime than the index, beats serving it back as a ghost or stale entry out of an index that
    # never rebuilds. Count alone would miss an edit in place, which changes the title, scope, tags, or
    # status the index shows; the mtime leg catches only an edit that left the file newer than the index,
    # so an edit that carries over an old or an explicitly backdated mtime (`cp -p`, `rsync -a`, `tar -x`,
    # a restored backup) stays invisible to both.
    if entries != len(lessons) or any(p.stat().st_mtime > index.stat().st_mtime for p in lessons):
        _rebuild_index(directory)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
