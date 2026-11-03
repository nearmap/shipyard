"""The decision page rendered from a whiteboard's notes.md: escaping, sections, determinism and the tool."""
from __future__ import annotations

import re

import pytest

from sy_tools import config, page, server

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
