"""The whiteboard's decision page: the readable decision record rendered from `notes.md` beside the board.

The board's folder holds three files for it. `notes.md` is the record Claude writes and the user may edit:
what is decided, what is still open and why. `page.html` is that record rendered for reading in a browser,
one collapsible section per top-level `## ` heading, the first one open. `page.rev.js` is a one-line
sidecar, `pageRev("<hash>");`, naming the revision `page.html` was rendered from.

The page refreshes itself. Every second it loads `page.rev.js` again through a fresh `<script>` tag, which
works over `file://` where a fetch would not. When the named revision differs from the one embedded in
the page, the page saves its scroll position and open sections to `sessionStorage` and navigates to
itself with `?rev=<hash>` through `location.replace`, then restores them.

Rendering escapes raw HTML: a `<script>` or an `<img onerror>` in `notes.md` shows as text, and a
`javascript:` link becomes inert. The same `notes.md` always renders to the same bytes.
"""
from __future__ import annotations

import hashlib
import html
from pathlib import Path
import re
from string import Template
from typing import Any, cast

import mistune
from mistune.core import BlockState
from mistune.renderers.html import HTMLRenderer

from .atomic import atomic_write

NOTES_NAME = "notes.md"
PAGE_NAME = "page.html"
REV_NAME = "page.rev.js"

_PLUGINS = ["table", "strikethrough"]
_AST = mistune.create_markdown(renderer="ast", plugins=_PLUGINS)
_HTML = cast(HTMLRenderer, mistune.create_markdown(escape=True, plugins=_PLUGINS).renderer)

_SHELL = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>$title</title>
<style>
:root { --fg: #1b1b1b; --bg: #ffffff; --muted: #4a4a4a; --line: #d0d0d0; --code-bg: #f3f3f3; --link: #0b57d0; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e8e8e8; --bg: #121212; --muted: #b8b8b8; --line: #3a3a3a; --code-bg: #1f1f1f; --link: #8ab4f8; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.6 system-ui, sans-serif; }
main { max-width: 52rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; counter-reset: section; }
h1 { font-size: 1.75rem; line-height: 1.25; margin: 0 0 1rem; }
details { counter-increment: section; border-top: 1px solid var(--line); padding: 0.5rem 0; }
summary { cursor: pointer; list-style: none; font-size: 1.25rem; font-weight: 600; padding: 0.25rem 0; }
summary::-webkit-details-marker { display: none; }
summary::before { content: counter(section) ". "; color: var(--muted); }
summary::after { content: " +"; color: var(--muted); }
details[open] > summary::after { content: " -"; }
summary:focus-visible, a:focus-visible { outline: 3px solid var(--link); outline-offset: 2px; }
a { color: var(--link); }
ul, ol { padding-left: 1.25rem; }
blockquote { margin: 1rem 0; padding-left: 1rem; border-left: 3px solid var(--line); color: var(--muted); }
code, pre { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 0.9em; background: var(--code-bg); }
code { padding: 0.1em 0.3em; border-radius: 3px; overflow-wrap: anywhere; }
pre { padding: 0.75rem 1rem; border-radius: 4px; overflow-x: auto; }
pre code { padding: 0; }
.table-wrap { overflow-x: auto; margin: 1rem 0; }
table { border-collapse: collapse; }
th, td { border: 1px solid var(--line); padding: 0.35rem 0.6rem; text-align: left; vertical-align: top; }
main img { max-width: 100%; height: auto; }
</style>
</head>
<body>
<main>
$body</main>
<script>
const REV = "$rev";
const KEY = "page-state";
function openFromHash() {
  const id = decodeURIComponent(location.hash.slice(1));
  const target = id && document.getElementById(id);
  if (target && target.tagName === "DETAILS") {
    target.open = true;
    target.scrollIntoView();
  }
}
const saved = sessionStorage.getItem(KEY);
if (saved) {
  sessionStorage.removeItem(KEY);
  const state = JSON.parse(saved);
  for (const d of document.querySelectorAll("details")) d.open = state.open.includes(d.id);
  window.scrollTo(0, state.y);
} else {
  openFromHash();
}
window.addEventListener("hashchange", openFromHash);
window.pageRev = function (h) {
  if (h === REV) return;
  sessionStorage.setItem(KEY, JSON.stringify({
    y: window.scrollY,
    open: [...document.querySelectorAll("details[open]")].map((d) => d.id),
  }));
  // Never location.reload(): a same-second rewrite is a 304 over http, so it would show the old page and loop.
  location.replace(location.pathname + "?rev=" + h);
};
setInterval(() => {
  const s = document.createElement("script");
  s.src = "page.rev.js?" + Date.now();
  s.onload = s.onerror = () => s.remove();
  document.head.appendChild(s);
}, 1000);
</script>
</body>
</html>
""")


def render(markdown: str) -> str:
    """The page for `markdown`: deterministic, raw HTML escaped, each top-level `## ` a collapsible section."""
    tokens = _parse(markdown)
    head: list[dict[str, Any]] = []
    sections: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for token in tokens:
        if _is_section(token):
            sections.append((token, []))
        elif sections:
            sections[-1][1].append(token)
        else:
            head.append(token)
    parts = [_HTML(head, BlockState())]
    used: set[str] = set()
    for index, (heading, body) in enumerate(sections):
        summary = _HTML.render_tokens(heading["children"], BlockState())
        opened = " open" if index == 0 else ""
        parts.append(f'<details id="{_slug(_plain(heading["children"]), used)}"{opened}><summary>{summary}</summary>\n')
        parts.append(_HTML(body, BlockState()) + "</details>\n")
    # escape=True means every literal "<table>" here came from the renderer, never from notes.md.
    body_html = "".join(parts).replace("<table>", '<div class="table-wrap"><table>')
    body_html = body_html.replace("</table>", "</table></div>")
    titles = [_plain(t["children"]) for t in tokens if t["type"] == "heading" and t["attrs"]["level"] == 1]
    title = titles[0] if titles else "Decision page"
    return _SHELL.substitute(title=html.escape(title), body=body_html, rev=_revision(markdown))


def write(directory: Path) -> dict[str, Any]:
    """Render `directory`'s notes.md to page.html and its refresh sidecar, creating both folder and notes.md first.

    A missing notes.md is written as a minimal record titled with the folder's name. The page is written
    before the sidecar, each atomically. Returns the folder, the notes.md and page.html paths, the section
    headings in page order, and `created`, true when this call made the folder.
    """
    created = not directory.is_dir()
    directory.mkdir(parents=True, exist_ok=True)
    notes = directory / NOTES_NAME
    if not notes.exists():
        atomic_write(notes, f"# {directory.name}\n\n## Where we are\n\nNothing yet.\n")
    markdown = notes.read_text(encoding="utf-8")
    page = directory / PAGE_NAME
    # Page before sidecar, so a sidecar never names a page that is not there yet.
    atomic_write(page, render(markdown))
    atomic_write(directory / REV_NAME, f'pageRev("{_revision(markdown)}");\n')
    return {
        "folder": str(directory),
        "notes": str(notes),
        "page": str(page),
        "sections": [_plain(t["children"]) for t in _parse(markdown) if _is_section(t)],
        "created": created,
    }


def _parse(markdown: str) -> list[dict[str, Any]]:
    tokens, _ = _AST.parse(markdown)
    assert isinstance(tokens, list)
    return tokens


def _is_section(token: dict[str, Any]) -> bool:
    return token["type"] == "heading" and token["attrs"]["level"] == 2


def _revision(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]


def _plain(tokens: list[dict[str, Any]]) -> str:
    return "".join(t["raw"] if "raw" in t else _plain(t.get("children", [])) for t in tokens)


def _slug(text: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
    slug, n = base, 1
    while slug in used:
        n += 1
        slug = f"{base}-{n}"
    used.add(slug)
    return slug
