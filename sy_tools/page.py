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
`javascript:` link becomes inert. The same `notes.md` always renders to the same bytes, and so does
`decisions.json`, the decision tree's rows and the Decision section's items as plain JSON for /sy:plan and /sy:spec.

The page polls only on `file://` and loopback. On a hosted copy with the `db` and `user` capabilities, a
comment widget stores one document per comment at `comments/<viewer id>/items/<id>`; `save_comments` turns an
ArtifactData listing of that store into `comments.json`.

`serve` runs one loopback HTTP server per root behind a token: GET and HEAD for files under the root, a virtual
`<slug>/board.html` Excalidraw editor, and one PUT that replaces an existing `<slug>/board.excalidraw` under
`board_lock`, the lock board_write and board_read also hold.
"""
from __future__ import annotations

import errno
import hashlib
import html
import http.server
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
from string import Template
import threading
from typing import Any, cast
import urllib.error
import urllib.parse
import urllib.request

import mistune
from mistune.core import BlockState
from mistune.renderers.html import HTMLRenderer

from . import board
from .atomic import atomic_write

NOTES_NAME = "notes.md"
PAGE_NAME = "page.html"
REV_NAME = "page.rev.js"
DECISIONS_NAME = "decisions.json"
COMMENTS_NAME = "comments.json"
COMMENTS_IN = ".comments-in"
TOKEN_NAME = ".serve-token"
EDITOR_NAME = "board.html"
MAX_BOARD_BYTES = 32 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost"}

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
#stopped { position: fixed; top: 0; left: 0; right: 0; margin: 0; padding: 0.5rem 1rem; text-align: center;
  background: #fff3bf; color: #1b1b1b; }
#stopped[hidden], .comments-orphans[hidden] { display: none; }
.comments { margin-top: 0.75rem; }
.comment { border-left: 3px solid var(--line); padding: 0.25rem 0.75rem; margin: 0.5rem 0; }
.comment-head, .comment-anchor, .comments-note { color: var(--muted); font-size: 0.875rem; }
.comment-text { white-space: pre-wrap; overflow-wrap: anywhere; }
.comments button { font: inherit; font-size: 0.875rem; margin: 0.25rem 0.5rem 0.25rem 0; }
.composer textarea { display: block; width: 100%; font: inherit; }
</style>
</head>
<body>
<main>
$body</main>
<p id="stopped" hidden>The page server stopped. Ask Claude to serve the page again.</p>
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
const LOCAL = location.protocol === "file:" || ["127.0.0.1", "localhost"].includes(location.hostname);
if (LOCAL) {
  const stopped = document.getElementById("stopped");
  let misses = 0;
  setInterval(() => {
    const s = document.createElement("script");
    s.src = "page.rev.js?" + Date.now();
    s.onload = () => {
      misses = 0;
      stopped.hidden = true;
      s.remove();
    };
    s.onerror = () => {
      misses += 1;
      if (misses >= 3) stopped.hidden = false;
      s.remove();
    };
    document.head.appendChild(s);
  }, 1000);
}
(async () => {
  if (!(window.claude && window.claude.use)) return;
  const [db, user] = await Promise.all([claude.use("db"), claude.use("user")]);
  if (!db || !user) return;
  const me = await user.id();
  let canWrite = me !== null;
  let writing = false;
  let marked = false;
  const DRAFT = "comment-draft:";
  const make = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const main = document.querySelector("main");
  const note = make("p", "comments-note");
  main.prepend(note);
  const lists = {};
  const anchors = [];
  for (const d of main.querySelectorAll(":scope > details[id]")) {
    const block = make("div", "comments");
    lists[d.id] = make("div");
    block.appendChild(lists[d.id]);
    d.appendChild(block);
    anchors.push({ section: d.id, decision: null, option: null, host: block });
  }
  const decisions = new Set();
  const options = new Set();
  const tree = document.getElementById("decision-tree");
  if (tree) {
    for (const tr of tree.querySelectorAll("tbody tr")) {
      const id = tr.cells[0] ? tr.cells[0].textContent.trim() : "";
      if (!/^D[0-9]+$$/.test(id)) continue;
      decisions.add(id);
      anchors.push({ section: tree.id, decision: id, option: null, host: tr.cells[0] });
    }
  }
  const opts = document.getElementById("options");
  if (opts) {
    for (const tr of opts.querySelectorAll("tbody tr")) {
      if (!tr.cells[0]) continue;
      const name = tr.cells[0].textContent.trim();
      options.add(name);
      anchors.push({ section: opts.id, decision: null, option: name, host: tr.cells[0] });
    }
  }
  const orphans = make("div", "comments comments-orphans");
  orphans.hidden = true;
  const orphanList = make("div");
  orphans.append(make("h2", null, "Comments on parts no longer on this page"), orphanList);
  main.appendChild(orphans);
  const draftKey = (a) => DRAFT + JSON.stringify([a.section, a.decision, a.option]);
  const composers = {};
  function readOnly() {
    canWrite = false;
    for (const e of main.querySelectorAll(".comment-button, .composer, .comment-delete")) e.remove();
    note.textContent = "You can read comments here but not add them.";
  }
  async function write(change, shown) {
    if (writing) {
      shown.textContent = "Wait for the last change to save.";
      return false;
    }
    writing = true;
    try {
      await change();
      return true;
    } catch (e) {
      if (e && e.code === "invalid_argument") readOnly();
      else shown.textContent = (e && e.message) || String(e);
      return false;
    } finally {
      writing = false;
    }
  }
  function compose(a) {
    const key = draftKey(a);
    if (composers[key]) return composers[key].querySelector("textarea").focus();
    const form = make("form", "composer");
    const on = a.decision || a.option;
    const box = make("textarea");
    box.maxLength = 4000;
    box.rows = 3;
    box.value = sessionStorage.getItem(key) || "";
    box.addEventListener("input", () => sessionStorage.setItem(key, box.value));
    const send = make("button", null, "Send");
    const cancel = make("button", null, "Cancel");
    cancel.type = "button";
    const shown = make("div", "comments-note");
    const title = make("div", "comments-note", on ? "Comment on " + on : "Comment on this section");
    form.append(title, box, send, cancel, shown);
    const close = () => {
      sessionStorage.removeItem(key);
      form.remove();
      delete composers[key];
    };
    cancel.onclick = close;
    form.onsubmit = async (event) => {
      event.preventDefault();
      const text = box.value.trim();
      if (!text) return;
      const done = await write(async () => {
        if (!marked && !commenters.has(me)) {
          await db.doc("comments/" + me).set({ at: new Date().toISOString() });
          marked = true;
        }
        const body = { section: a.section, decision: a.decision, option: a.option, text: text };
        body.at = new Date().toISOString();
        body.rev = REV;
        await db.doc("comments/" + me + "/items/" + crypto.randomUUID().slice(0, 8)).set(body);
      }, shown);
      if (done) close();
    };
    lists[a.section].parentNode.appendChild(form);
    composers[key] = form;
    box.focus();
  }
  if (canWrite) {
    for (const a of anchors) {
      const button = make("button", "comment-button", "Comment");
      button.type = "button";
      button.onclick = () => compose(a);
      a.host.appendChild(button);
      const draft = sessionStorage.getItem(draftKey(a));
      if (draft) compose(a);
    }
  }
  const commenters = new Set();
  const items = {};
  let sequence = 0;
  async function render() {
    const mine = ++sequence;
    const all = Object.values(items).flat();
    all.sort((x, y) => String(x.body.at).localeCompare(String(y.body.at)));
    const people = await user.profiles([...new Set(all.map((i) => i.author))]);
    if (mine !== sequence) return;
    for (const list of Object.values(lists)) list.replaceChildren();
    orphanList.replaceChildren();
    for (const i of all) {
      const b = i.body;
      const placed = Object.hasOwn(lists, b.section) && (b.decision == null || decisions.has(b.decision))
        && (b.option == null || options.has(b.option));
      const row = make("div", "comment");
      const head = make("div", "comment-head");
      const on = b.decision != null ? b.decision : b.option;
      if (on != null) head.appendChild(make("strong", null, String(on) + ": "));
      head.append(make("span", null, people[i.author].name || "Someone"));
      head.append(make("span", null, ", " + String(b.at).slice(0, 10)));
      if (canWrite && i.author === me) {
        const del = make("button", "comment-delete", "Delete");
        del.type = "button";
        del.onclick = () => write(() => db.doc("comments/" + me + "/items/" + i.id).delete(), note);
        head.appendChild(del);
      }
      const where = [b.section, b.decision, b.option].filter((x) => x != null).join(" / ");
      if (!placed) row.appendChild(make("div", "comment-anchor", "Was on: " + where));
      row.append(head, make("div", "comment-text", String(b.text)));
      (placed ? lists[b.section] : orphanList).appendChild(row);
    }
    orphans.hidden = !orphanList.childElementCount;
  }
  let delivered = false;
  const loadingTimer = setTimeout(() => {
    note.textContent = delivered ? "Comments may be out of date." : "Comments cannot be reached yet.";
  }, 10000);
  const failed = (e) => {
    clearTimeout(loadingTimer);
    note.textContent = "Comments are unavailable: " + ((e && e.message) || String(e));
  };
  const subscribed = new Set();
  db.collection("comments").onSnapshot((snap) => {
    delivered = true;
    if (!snap.metadata.fromCache) {
      clearTimeout(loadingTimer);
      if (note.textContent.startsWith("Comments ")) note.textContent = "";
    }
    for (const d of snap.docs) {
      commenters.add(d.id);
      if (subscribed.has(d.id)) continue;
      if (subscribed.size >= 60) {
        note.textContent = "There are more comments than this page can show.";
        continue;
      }
      subscribed.add(d.id);
      db.collection("comments/" + d.id + "/items").onSnapshot((s) => {
        items[d.id] = s.docs.map((x) => ({ id: x.id, author: d.id, body: x.data() }));
        render();
      }, failed);
    }
    render();
  }, failed);
})();
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
    """Render `directory`'s notes.md to decisions.json, page.html and its sidecar, creating folder and notes.md first.

    A missing notes.md is written as a minimal record titled with the folder's name. decisions.json is written
    first, then the page, then the sidecar, each atomically. Returns the folder, the notes.md, page.html and
    decisions.json paths, the section headings in page order, and `created`, true when this call made the folder.
    """
    created = not directory.is_dir()
    directory.mkdir(parents=True, exist_ok=True)
    notes = directory / NOTES_NAME
    if not notes.exists():
        atomic_write(notes, f"# {directory.name}\n\n## Where we are\n\nNothing yet.\n")
    markdown = notes.read_text(encoding="utf-8")
    decisions = directory / DECISIONS_NAME
    atomic_write(decisions, json.dumps(decision_log(markdown), indent=2, ensure_ascii=False) + "\n")
    page = directory / PAGE_NAME
    # Page before sidecar, so a sidecar never names a page that is not there yet.
    atomic_write(page, render(markdown))
    atomic_write(directory / REV_NAME, f'pageRev("{_revision(markdown)}");\n')
    return {
        "folder": str(directory),
        "notes": str(notes),
        "page": str(page),
        "decisions": str(decisions),
        "sections": [_plain(t["children"]) for t in _parse(markdown) if _is_section(t)],
        "created": created,
    }


def decision_log(markdown: str) -> dict[str, Any]:
    """The decision tree's rows and the Decision section's top-level items as plain text, for /sy:plan and /sy:spec."""
    tree = _first_table(_section(markdown, "Decision tree"))
    rows: list[dict[str, str]] = []
    if tree is not None:
        head, body = tree["children"]
        names = [_text(cell["children"]) for cell in head["children"]]
        rows = [
            dict(zip(names, (_text(cell["children"]) for cell in row["children"]), strict=True))
            for row in body["children"]
        ]
    items = [
        " ".join(_text(block["children"]) for block in item["children"] if block["type"] in ("block_text", "paragraph"))
        for token in _section(markdown, "Decision")
        if token["type"] == "list"
        for item in token["children"]
    ]
    return {"rev": _revision(markdown), "tree": rows, "decision": items}


def save_comments(directory: Path) -> dict[str, Any]:
    """Merge an ArtifactData listing of the hosted page's comment store into `directory`'s comments.json.

    Reads every `.comments-in/comments/<author>/items/<id>.json`, taking author and id from the path, writes
    them sorted by (at, author, id), then removes the staging folder. `pending` lists the comments whose
    `<author>/<id>` has no row in notes.md's `## Comments` table yet.
    """
    staging = directory / COMMENTS_IN
    if not staging.is_dir():
        raise FileNotFoundError(f"no comment listing at {staging}: run ArtifactData list with out_dir {staging} first")
    comments = []
    for path in sorted((staging / "comments").glob("*/items/*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"{path} is not a JSON comment: {exc}") from None
        if not isinstance(body, dict):
            raise ValueError(f"{path} is not a JSON object")
        comments.append({**body, "id": path.stem, "author": path.parent.parent.name})
    comments.sort(key=lambda c: (c["at"], c["author"], c["id"]))
    keys = [f"{c['author']}/{c['id']}" for c in comments]
    assert len(set(keys)) == len(keys), f"duplicate comment keys in {staging}"
    target = directory / COMMENTS_NAME
    atomic_write(target, json.dumps(comments, indent=2, ensure_ascii=False) + "\n")
    shutil.rmtree(staging)
    table = _first_table(_section((directory / NOTES_NAME).read_text(encoding="utf-8"), "Comments"))
    rows = table["children"][1]["children"] if table else []
    answered = {_text(row["children"][0]["children"]).strip() for row in rows}
    pending = [c for c, key in zip(comments, keys, strict=True) if key not in answered]
    return {"comments": str(target), "count": len(comments), "pending": pending}


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


def _text(tokens: list[dict[str, Any]]) -> str:
    """Like `_plain`, but a soft or hard line break reads as one space, so a wrapped line keeps its word gap."""
    return "".join(
        " " if t["type"] in ("softbreak", "linebreak") else t["raw"] if "raw" in t else _text(t.get("children", []))
        for t in tokens
    )


def _section(markdown: str, heading: str) -> list[dict[str, Any]]:
    """The top-level tokens under the first `## <heading>`, up to the next one; empty when there is none."""
    tokens = _parse(markdown)
    starts = [i for i, t in enumerate(tokens) if _is_section(t) and _plain(t["children"]) == heading]
    if not starts:
        return []
    body = tokens[starts[0] + 1:]
    end = next((i for i, t in enumerate(body) if _is_section(t)), len(body))
    return body[:end]


def _first_table(tokens: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((t for t in tokens if t["type"] == "table"), None)


def _slug(text: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
    slug, n = base, 1
    while slug in used:
        n += 1
        slug = f"{base}-{n}"
    used.add(slug)
    return slug


_SERVERS: dict[Path, tuple[dict[str, Any], http.server.ThreadingHTTPServer | None]] = {}
_SERVERS_LOCK = threading.Lock()
_BOARD_LOCKS: dict[Path, threading.Lock] = {}
_BOARD_LOCKS_LOCK = threading.Lock()


def board_lock(folder: Path) -> threading.Lock:
    """The lock every writer of `folder`'s board.excalidraw holds in this process: board_write, board_read, a PUT."""
    key = folder.resolve()
    with _BOARD_LOCKS_LOCK:
        return _BOARD_LOCKS.setdefault(key, threading.Lock())


def serve(root: Path, port: int | None, probe: str) -> dict[str, Any]:
    """Serve `root` on 127.0.0.1 behind a token, once per process; returns its port, base URL and ssh forward line.

    The token lives in `root/.serve-token` and the port derives from the root's path, so the URL survives a
    restart. When the port is taken by another process already serving this root with this token (checked
    by fetching the board-relative `probe` path), that server is reused.
    """
    root = root.resolve()
    with _SERVERS_LOCK:
        if root in _SERVERS:
            running = _SERVERS[root][0]
            if port is not None and port != running["port"]:
                raise ValueError(f"the page server for {root} already runs on port {running['port']}, not {port}")
            return running
        token_file = root / TOKEN_NAME
        if not token_file.exists():
            atomic_write(token_file, secrets.token_urlsafe(16))
            token_file.chmod(0o600)
        token = token_file.read_text(encoding="utf-8")
        if port is None:
            port = 20000 + int(hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8], 16) % 10000
        info = {
            "port": port,
            "base": f"http://127.0.0.1:{port}/{token}/",
            "forward": f"ssh -L {port}:127.0.0.1:{port} {socket.gethostname()}",
        }
        handler = type("_RootHandler", (_Handler,), {"root": root, "token": token})
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE and _answers(info["base"] + probe):
                _SERVERS[root] = (info, None)
                return info
            raise OSError(
                exc.errno, f"cannot serve on 127.0.0.1:{port} ({exc.strerror}); pass `port` to choose another"
            ) from None
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _SERVERS[root] = (info, httpd)
        return info


def _answers(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def _etag(data: bytes) -> str:
    return f'"{hashlib.sha256(data).hexdigest()[:16]}"'


class _Handler(http.server.SimpleHTTPRequestHandler):
    """GET and HEAD for files under the root behind `/<token>/`, the virtual board editor, and one guarded PUT."""

    root: Path
    token: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(self.root), **kwargs)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        pass

    def _target(self) -> Path | None:
        """The resolved file a request names, or None when it lies outside the token prefix or the root."""
        path = urllib.parse.urlsplit(self.path).path
        prefix = f"/{self.token}/"
        if not path.startswith(prefix):
            return None
        target = (self.root / urllib.parse.unquote(path[len(prefix):])).resolve()
        if not target.is_relative_to(self.root) or target == self.root or target.name == TOKEN_NAME:
            return None
        return target

    def _board(self, target: Path, name: str) -> bool:
        """True when `target` is `<slug>/<name>` for a board folder under the root holding a board.excalidraw."""
        folder = target.parent
        if target.name != name or folder.parent != self.root:
            return False
        try:
            board.folder(self.root, folder.name)
        except ValueError:
            return False
        return (folder / board.SCENE_NAME).is_file()

    def translate_path(self, path: str) -> str:
        target = self._target()
        assert target is not None, "do_GET and do_HEAD refuse a path outside the root before serving it"
        return str(target)

    def list_directory(self, path: str | Any) -> None:
        self.send_error(404)

    def do_GET(self) -> None:
        self._get(body=True)

    def do_HEAD(self) -> None:
        self._get(body=False)

    def _get(self, *, body: bool) -> None:
        target = self._target()
        if target is None or target.is_dir():
            self.send_error(404)
        elif self._board(target, EDITOR_NAME):
            self._send(200, _EDITOR.substitute(slug=target.parent.name).encode("utf-8"), "text/html; charset=utf-8",
                       body)
        elif self._board(target, board.SCENE_NAME):
            data = target.read_bytes()
            tag = _etag(data)
            if self.headers.get("If-None-Match") == tag:
                self.send_response(304)
                self.send_header("ETag", tag)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            else:
                self._send(200, data, "application/json", body, tag)
        elif body:
            super().do_GET()
        else:
            super().do_HEAD()

    def _send(self, code: int, data: bytes, kind: str, body: bool = True, tag: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if tag is not None:
            self.send_header("ETag", tag)
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_PUT(self) -> None:
        target = self._target()
        if target is None or not self._board(target, board.SCENE_NAME):
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if urllib.parse.urlsplit("//" + self.headers.get("Host", "")).hostname not in LOOPBACK or (
            origin is not None and urllib.parse.urlsplit(origin).hostname not in LOOPBACK
        ):
            self.send_error(403)
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self.send_error(415)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > MAX_BOARD_BYTES:
            self.send_error(413)
            return
        data = self.rfile.read(int(length))
        try:
            scene = json.loads(data)
        except ValueError:
            scene = None
        if not isinstance(scene, dict) or scene.get("type") != "excalidraw":
            self.send_error(400)
            return
        expected = self.headers.get("If-Match")
        with board_lock(target.parent):
            stale = expected is not None and expected != _etag(target.read_bytes())
            atomic_write(target, data.decode("utf-8"))
        tag = _etag(data)
        self._send(200, json.dumps({"etag": tag, "stale": stale}).encode("utf-8"), "application/json", tag=tag)


_EDITOR = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$slug board</title>
<link rel="stylesheet" href="https://esm.sh/@excalidraw/excalidraw@0.18.0/dist/prod/index.css">
<script>window.EXCALIDRAW_ASSET_PATH = "https://esm.sh/@excalidraw/excalidraw@0.18.0/dist/prod/";</script>
<style>
html, body { height: 100%; margin: 0; font: 14px/1.4 system-ui, sans-serif; }
#bar { position: fixed; top: 0; left: 0; right: 0; height: 2.5rem; display: flex; gap: 1rem; align-items: center;
  padding: 0 0.75rem; background: #f1f3f5; color: #1b1b1b; border-bottom: 1px solid #ced4da; }
#banner { background: #fff3bf; padding: 0.25rem 0.5rem; border-radius: 4px; }
#error { color: #c92a2a; }
#banner[hidden], #error[hidden] { display: none; }
#app { position: fixed; top: 2.5rem; left: 0; right: 0; bottom: 0; }
</style>
</head>
<body>
<div id="bar"><strong id="status">Loading</strong><span id="banner" hidden>Claude changed the board.
<button id="load" type="button">Load it (your unsaved edits are lost)</button>
<button id="keep" type="button">Keep editing (your next save is kept aside as a stale save)</button></span>
<span id="error" hidden></span></div>
<div id="app"></div>
<script type="module">
const FILE = "board.excalidraw";
const statusLine = document.getElementById("status");
const banner = document.getElementById("banner");
const errorLine = document.getElementById("error");
let React, ReactDOM, X;
try {
  [React, ReactDOM, X] = await Promise.all([
    import("https://esm.sh/react@18.3.1").then((m) => m.default),
    import("https://esm.sh/react-dom@18.3.1/client"),
    import("https://esm.sh/@excalidraw/excalidraw@0.18.0?deps=react@18.3.1,react-dom@18.3.1"),
  ]);
} catch (e) {
  statusLine.textContent = "Excalidraw could not load (offline?). Open board.excalidraw in VS Code instead.";
  throw e;
}
let api = null;
let etag = null;
let recorded = null;
let loading = true;
let saving = false;
let watching = false;
let keepAside = false;
let puts = 0;
let timer = null;
const version = () => X.getSceneVersion(api.getSceneElementsIncludingDeleted()) + "|"
  + Object.keys(api.getFiles()).sort().join(",");
const unsaved = () => !loading && version() !== recorded;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function settle() {
  await document.fonts.ready;
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  recorded = version();
  loading = false;
  statusLine.textContent = "Saved";
}
async function apply(tag, scene) {
  loading = true;
  clearTimeout(timer);
  timer = null;
  keepAside = false;
  banner.hidden = true;
  api.addFiles(Object.values(scene.files || {}));
  api.updateScene({ elements: X.restoreElements(scene.elements, null), captureUpdate: X.CaptureUpdateAction.NEVER });
  etag = tag;
  await settle();
}
async function loadLatest() {
  const r = await fetch(FILE, { cache: "no-store" });
  if (!r.ok) throw new Error("board.excalidraw answered " + r.status);
  await apply(r.headers.get("ETag"), await r.json());
}
function failed(message) {
  statusLine.textContent = "Save failed";
  errorLine.textContent = message;
  errorLine.hidden = false;
}
async function save() {
  clearTimeout(timer);
  timer = null;
  if (loading || saving || !banner.hidden || !unsaved()) return;
  saving = true;
  puts += 1;
  try {
    if (!keepAside) {
      const check = await fetch(FILE, { cache: "no-store", headers: { "If-None-Match": etag } });
      if (check.status === 200) {
        banner.hidden = false;
        return;
      }
      if (check.status !== 304) return failed("board.excalidraw answered " + check.status);
    }
    statusLine.textContent = "Saving";
    const aside = keepAside;
    const elements = api.getSceneElementsIncludingDeleted();
    const files = api.getFiles();
    const sent = X.getSceneVersion(elements) + "|" + Object.keys(files).sort().join(",");
    const body = X.serializeAsJSON(api.getSceneElements(), api.getAppState(), files, "local");
    let r;
    try {
      r = await fetch(FILE, { method: "PUT", headers: { "If-Match": etag, "Content-Type": "application/json" }, body });
    } catch (e) {
      return failed(String(e));
    }
    if (!r.ok) return failed("the save answered " + r.status + " " + (await r.text()));
    const result = await r.json();
    etag = result.etag;
    recorded = sent;
    keepAside = false;
    errorLine.hidden = true;
    if (result.stale && !aside) banner.hidden = false;
    statusLine.textContent = unsaved() ? "Unsaved" : "Saved";
  } finally {
    saving = false;
  }
  if (unsaved()) timer = setTimeout(save, 1000);
}
function changed() {
  if (loading || !api || !unsaved()) return;
  if (!saving) statusLine.textContent = "Unsaved";
  clearTimeout(timer);
  timer = setTimeout(save, 1000);
}
const first = await fetch(FILE, { cache: "no-store" });
if (!first.ok) throw new Error("board.excalidraw answered " + first.status);
etag = first.headers.get("ETag");
const scene = await first.json();
const appState = { ...scene.appState };
delete appState.collaborators;
ReactDOM.createRoot(document.getElementById("app")).render(React.createElement(X.Excalidraw, {
  initialData: {
    elements: X.restoreElements(scene.elements, null), appState, files: scene.files || {}, scrollToContent: true,
  },
  excalidrawAPI: (a) => { api = a; },
  onChange: changed,
  UIOptions: { canvasActions: { loadScene: false, saveToActiveFile: false, export: false, saveAsImage: false } },
}));
while (!api || api.getAppState().isLoading) await wait(50);
await settle();
document.getElementById("load").onclick = loadLatest;
document.getElementById("keep").onclick = () => {
  banner.hidden = true;
  keepAside = true;
  statusLine.textContent = "Unsaved";
  timer = setTimeout(save, 1000);
};
window.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    e.stopImmediatePropagation();
    save();
  }
}, true);
setInterval(async () => {
  if (loading || saving || watching) return;
  watching = true;
  const before = puts;
  try {
    const r = await fetch(FILE, { cache: "no-store", headers: { "If-None-Match": etag } });
    if (r.status !== 200 || saving || loading || puts !== before) return;
    const tag = r.headers.get("ETag");
    const latest = await r.json();
    if (!unsaved()) await apply(tag, latest);
    else if (!keepAside) banner.hidden = false;
  } finally {
    watching = false;
  }
}, 1000);
</script>
</body>
</html>
""")
