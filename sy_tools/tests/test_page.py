"""The decision page rendered from a whiteboard's notes.md: escaping, sections, determinism, the decision and
comment logs, and the local server with its one guarded board save."""
from __future__ import annotations

import json
from pathlib import Path
import re
import stat
import threading
import time
import urllib.error
import urllib.request

import pytest

from sy_tools import board, config, page, server

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

NOTES = """# Pricing idea

Intro above the sections.

## Where we are

- ## not a section in a list

> ## not a section in a quote

```
## not a section in a fence
```

## Options

| a | b |
|---|---|
| 1 | 2 |

## Options
"""


def test_raw_html_and_javascript_links_in_notes_render_inert():
    rendered = page.render(
        "## Risky\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[click](javascript:alert(1))\n"
    )
    body = rendered.split("<main>", 1)[1].split("</main>", 1)[0]
    assert "<script" not in body and "<img" not in body, body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body, body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body, body
    assert '<a href="#harmful-link">click</a>' in body, body


def test_each_top_level_section_is_a_details_with_a_unique_id_and_only_the_first_open():
    rendered = page.render(NOTES)
    details = re.findall(r"<details id=\"([^\"]+)\"( open)?>", rendered)
    assert details == [("where-we-are", " open"), ("options", ""), ("options-2", "")], details
    assert rendered.count("<h2>") == 2, "the list and quote headings stay headings inside their section"
    assert "## not a section in a fence" in rendered
    assert '<div class="table-wrap"><table>' in rendered
    assert "<title>Pricing idea</title>" in rendered


def test_the_same_notes_render_byte_identical_and_the_sidecar_names_the_embedded_hash(tmp_path):
    (tmp_path / page.NOTES_NAME).write_text(NOTES, encoding="utf-8")
    page.write(tmp_path)
    first = (tmp_path / page.PAGE_NAME).read_bytes()
    page.write(tmp_path)
    assert (tmp_path / page.PAGE_NAME).read_bytes() == first
    decisions = (tmp_path / page.DECISIONS_NAME).read_bytes()
    page.write(tmp_path)
    assert (tmp_path / page.DECISIONS_NAME).read_bytes() == decisions
    embedded = re.search(r'const REV = "([0-9a-f]{16})";', first.decode("utf-8"))
    assert embedded, "the page embeds the revision it was rendered from"
    assert (tmp_path / page.REV_NAME).read_text(encoding="utf-8") == f'pageRev("{embedded.group(1)}");\n'


def test_the_tool_creates_the_folder_and_notes_then_renders_later_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repo_scratch_dir", lambda: tmp_path)
    folder = tmp_path / "whiteboards" / "pricing-idea"
    result = server.page_render("pricing-idea")
    assert result["sections"] == ["Where we are"], result
    assert result["created"] is True, "the first render makes the folder"
    assert (folder / page.NOTES_NAME).read_text(encoding="utf-8").startswith("# pricing-idea\n")
    assert "Nothing yet." in (folder / page.PAGE_NAME).read_text(encoding="utf-8")
    (folder / page.NOTES_NAME).write_text(NOTES, encoding="utf-8")
    result = server.page_render("pricing-idea")
    assert result["sections"] == ["Where we are", "Options", "Options"], result
    assert result["created"] is False, "a later render finds the folder already there"
    assert "Intro above the sections." in (folder / page.PAGE_NAME).read_text(encoding="utf-8")


def test_an_id_outside_the_whiteboards_folder_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repo_scratch_dir", lambda: tmp_path)
    with pytest.raises(server.ToolError, match="board id"):
        server.page_render("../elsewhere")
    assert list(tmp_path.iterdir()) == []


def test_decisions_json_holds_the_tree_rows_and_the_decision_items_as_plain_text(tmp_path):
    notes = (
        "# Idea\n\n## Decision tree\n\n| Id | Decision | Status |\n|---|---|---|\n| D1 | Store `raw` data | open |\n"
        "| D2 | Batch *or* stream | settled |\n\n## Decision\n\n- Use the batch path,\n  wrapped onto two lines.\n"
        "- Keep the raw store.\n  - nested detail\n"
    )
    (tmp_path / page.NOTES_NAME).write_text(notes, encoding="utf-8")
    result = page.write(tmp_path)
    log = json.loads(Path(result["decisions"]).read_text(encoding="utf-8"))
    assert log["tree"] == [
        {"Id": "D1", "Decision": "Store raw data", "Status": "open"},
        {"Id": "D2", "Decision": "Batch or stream", "Status": "settled"},
    ], log
    assert log["decision"] == ["Use the batch path, wrapped onto two lines.", "Keep the raw store."], log
    assert re.fullmatch(r"[0-9a-f]{16}", log["rev"]), log
    assert page.decision_log("# Idea\n\n## Decision tree\n\nNo table yet.\n")["tree"] == []
    assert page.decision_log("# Idea\n")["decision"] == []
    with pytest.raises(ValueError, match="Decision tree"):
        page.decision_log("# Idea\n\n## Decision tree\n\n| Id | Decision |\n|---|---|\n| D1 | a |\n\n| D2 | b |\n")
    with pytest.raises(ValueError, match="Decision tree"):
        page.decision_log("# Idea\n\n## Decision tree\n\n| Id | Decision |\n|---|---|\n| D1 |\n| D2 | x | y |\n")
    with pytest.raises(ValueError, match="Decision tree"):
        page.decision_log(
            "# Idea\n\n## Decision tree\n\n| Id | Decision |\n|---|---|\n| D1 | a |\n\n### More\n\n"
            "| Id | Decision |\n|---|---|\n| D2 | b |\n"
        )
    head = "# Idea\n\n## Decision tree\n\n| Id | Decision |\n|---|---|\n| D1 | a |\n"
    with pytest.raises(ValueError, match="Decision tree"):
        page.decision_log(head + "| Id | Decision |\n|---|---|\n")
    for broken in ("| D2 | b |\n---\n", "- note A\n\n  | D2 | b |\n\n  note B\n"):
        with pytest.raises(ValueError, match="Decision tree"):
            page.decision_log(head + "\n" + broken)
    prose = (
        "# Idea\n\n## Decision tree\n\n| Id | Decision |\n|---|---|\n| D1 | a |\n\n"
        "Chose A for cost \\| latency, `x | y`.\n"
    )
    assert [row["Id"] for row in page.decision_log(prose)["tree"]] == ["D1"]


def _stage(folder: Path, author: str, cid: str, body: dict) -> None:
    (folder / page.COMMENTS_IN / "comments" / f"{author}.json").parent.mkdir(parents=True, exist_ok=True)
    (folder / page.COMMENTS_IN / "comments" / f"{author}.json").write_text('{"at": "x"}', encoding="utf-8")
    item = folder / page.COMMENTS_IN / "comments" / author / "items" / f"{cid}.json"
    item.parent.mkdir(parents=True, exist_ok=True)
    item.write_text(json.dumps(body), encoding="utf-8")


def test_comments_json_takes_author_and_id_from_the_store_paths_and_stays_pending_until_answered(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "repo_scratch_dir", lambda: tmp_path)
    server.page_render("idea")
    folder = tmp_path / "whiteboards" / "idea"
    with pytest.raises(server.ToolError, match="ArtifactData list"):
        server.page_comments_save("idea")
    comment = {"section": "options", "decision": None, "option": "Batch", "rev": "r"}
    _stage(folder, "u_b", "c1", {**comment, "text": "second", "at": "2026-11-04T10:00:00Z"})
    _stage(folder, "u_b", "c2", {**comment, "text": "third", "at": "2026-11-04T11:00:00Z"})
    _stage(folder, "u_a", "c1", {**comment, "text": "first", "at": "2026-11-04T10:00:00Z"})
    result = server.page_comments_save("idea")
    saved = json.loads((folder / page.COMMENTS_NAME).read_text(encoding="utf-8"))
    assert [(c["author"], c["id"], c["text"]) for c in saved] == [
        ("u_a", "c1", "first"), ("u_b", "c1", "second"), ("u_b", "c2", "third")
    ], saved
    assert result["count"] == 3 and [c["text"] for c in result["pending"]] == ["first", "second", "third"], result
    assert not (folder / page.COMMENTS_IN).exists(), "the staging folder goes, so a deleted comment drops next sync"
    notes = folder / page.NOTES_NAME
    notes.write_text(
        notes.read_text(encoding="utf-8")
        + "\n## Comments\n\n| Comment | Where | Point | Disposition |\n|---|---|---|---|\n"
        "| `u_a/c1` | options | x | noted |\n| `u_b/c2` | options | x | noted |\n",
        encoding="utf-8",
    )
    _stage(folder, "u_b", "c1", {**comment, "text": "second", "at": "2026-11-04T10:00:00Z"})
    _stage(folder, "u_b", "c2", {**comment, "text": "third", "at": "2026-11-04T11:00:00Z"})
    _stage(folder, "u_a", "c1", {**comment, "text": "first", "at": "2026-11-04T10:00:00Z"})
    page.write(folder)
    html = (folder / page.PAGE_NAME).read_text(encoding="utf-8")
    assert "u_a/c1" not in html and "<th>Comment</th>" not in html
    assert "<th>Where</th>" in html and "<td>options</td>" in html
    assert [c["text"] for c in server.page_comments_save("idea")["pending"]] == ["second"]
    before = (folder / page.COMMENTS_NAME).read_bytes()
    notes.write_text("# Idea\n\n## Comments\n\n| Comment | Where |\n|---|---|\n| `a|b` | x |\n", encoding="utf-8")
    _stage(folder, "u_a", "c9", {**comment, "text": "new", "at": "2026-11-04T12:00:00Z"})
    with pytest.raises(server.ToolError, match="Comments"):
        server.page_comments_save("idea")
    two_tables = (
        "# Idea\n\n## Comments\n\n| Comment | Where |\n|---|---|\n| u | x |\n\n### More\n\n| a | b |\n|---|---|\n"
    )
    notes.write_text(two_tables, encoding="utf-8")
    with pytest.raises(server.ToolError, match="Comments"):
        server.page_comments_save("idea")
    split_row = "# Idea\n\n## Comments\n\n| Comment | Where |\n|---|---|\n| u | x |\n\n| v | y |\n"
    notes.write_text(split_row, encoding="utf-8")
    with pytest.raises(server.ToolError, match="Comments"):
        server.page_comments_save("idea")
    assert (folder / page.COMMENTS_IN).exists() and (folder / page.COMMENTS_NAME).read_bytes() == before


def _call(url: str, method: str = "GET", data: bytes | None = None, **headers: str) -> tuple[int, bytes, dict]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(request, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repo_scratch_dir", lambda: tmp_path)
    server.page_render("idea")
    yield server.page_serve("idea")
    for _, httpd in page._SERVERS.values():
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
    page._SERVERS.clear()


def test_the_server_answers_only_behind_its_token_inside_the_root_and_survives_a_fresh_process(served, tmp_path):
    folder = tmp_path / "whiteboards" / "idea"
    status, body, _ = _call(served["url"])
    assert (status, body) == (200, (folder / page.PAGE_NAME).read_bytes())
    base = served["url"].removesuffix("idea/page.html")
    origin = base.split("/", 3)[:3]
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    (folder / "link.txt").symlink_to(tmp_path / "outside.txt")
    refused = ["/".join(origin) + "/idea/page.html", base, base + ".serve-token", base + "../x", base + "%2e%2e/x",
               base + "idea/link.txt", base + "idea/"]
    assert [_call(url)[0] for url in refused] == [404] * len(refused)
    root = (tmp_path / "whiteboards").resolve()
    first = page._SERVERS[root][1]
    assert first is not None and first.server_address[0] == "127.0.0.1"
    assert stat.S_IMODE((root / page.TOKEN_NAME).stat().st_mode) == 0o600
    again = server.page_serve("idea")
    assert again == served and page._SERVERS[root][1] is first, "a second call while owned returns the same server"
    page._SERVERS.clear()
    try:
        assert server.page_serve("idea") == served, "token on disk, derived port, the running server answers"
        assert page._SERVERS[root][1] is None
    finally:
        first.shutdown()
        first.server_close()
    assert server.page_serve("idea") == served, "a borrowed server that is gone is bound here"
    rebound = page._SERVERS[root][1]
    assert rebound is not None and _call(served["url"])[0] == 200
    rebound.shutdown()
    rebound.server_close()
    page._SERVERS.clear()
    (root / page.TOKEN_NAME).unlink()
    (root / f"{page.TOKEN_NAME}.tmp").write_text("stale", encoding="utf-8")
    assert server.page_serve("idea")["url"] != served["url"], "a stale staging file does not block a new token"


def test_serving_before_any_render_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "repo_scratch_dir", lambda: tmp_path)
    with pytest.raises(server.ToolError, match="page_render first"):
        server.page_serve("idea")


def test_the_board_editor_saves_only_a_guarded_put_and_board_read_sees_it_as_a_user_edit(served, tmp_path):
    folder = tmp_path / "whiteboards" / "idea"
    server.board_write("idea", {
        "sections": [{"id": "api", "name": "System design"}],
        "notes": [{"id": "server", "section": "api", "kind": "component", "text": "API server", "at": [0, 0]}],
        "links": [],
    })
    status, html_body, _ = _call(served["board_url"])
    pinned = b"https://esm.sh/@excalidraw/excalidraw@0.18.0?deps=react@18.3.1,react-dom@18.3.1"
    assert status == 200 and pinned in html_body and b"https://esm.sh/react@18.3.1" in html_body
    scene_url = served["board_url"].removesuffix(page.EDITOR_NAME) + board.SCENE_NAME
    status, _, headers = _call(scene_url)
    tag = headers["ETag"]
    assert status == 200 and _call(scene_url, **{"If-None-Match": tag})[0] == 304
    scene = json.loads((folder / board.SCENE_NAME).read_text(encoding="utf-8"))
    next(e for e in scene["elements"] if e["id"] == "server:text").update(text="Gateway", originalText="Gateway")
    edited = json.dumps(scene).encode("utf-8")
    json_type = {"Content-Type": "application/json"}
    status, body, _ = _call(scene_url, "PUT", edited, **json_type, **{"If-Match": tag})
    assert (status, json.loads(body)["stale"]) == (200, False) and (folder / board.SCENE_NAME).read_bytes() == edited
    note = next(n for n in server.board_read("idea")["notes"] if n["id"] == "server")
    assert note["user_changed"] == ["text"], note
    before = (folder / board.SCENE_NAME).read_bytes()
    base = served["url"].removesuffix("idea/page.html")
    port = served["port"]
    refused = [
        (base.replace(base.split("/")[3] + "/", "") + "idea/board.excalidraw", edited, json_type),
        (base + "idea/page.html", edited, json_type),
        (base + "nope/board.excalidraw", edited, json_type),
        (scene_url, edited, {**json_type, "Host": f"evil.test:{port}"}),
        (scene_url, edited, {**json_type, "Origin": "http://evil.test"}),
        (scene_url, edited, {"Content-Type": "text/plain"}),
        (scene_url, b'{"type": "other"}', json_type),
        (base + "%2e%2e/x/board.excalidraw", edited, json_type),
    ]
    assert [_call(url, "PUT", data, **h)[0] for url, data, h in refused] == [404, 404, 404, 403, 403, 415, 400, 404]
    assert (folder / board.SCENE_NAME).read_bytes() == before, "a refused PUT writes nothing"
    assert _call(scene_url, "PUT", edited, **json_type, Host=f"localhost:{port + 1}")[0] == 200
    status, body, _ = _call(scene_url, "PUT", edited, **json_type, **{"If-Match": '"0000000000000000"'})
    assert (status, json.loads(body)["stale"]) == (200, True), "a stale save is still written; board_read keeps it"
    later = edited.replace(b"Gateway", b"Gateway 2")
    done = threading.Event()
    with page.board_lock(folder):
        threading.Thread(target=lambda: (_call(scene_url, "PUT", later, **json_type), done.set()), daemon=True).start()
        time.sleep(0.3)
        assert not done.is_set() and (folder / board.SCENE_NAME).read_bytes() == edited, "the PUT waits for the lock"
    assert done.wait(5) and (folder / board.SCENE_NAME).read_bytes() == later
