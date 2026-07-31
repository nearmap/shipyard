"""Offline tests for in-process rich-text conversion: no network, no tracker.

The round-trip test is the load-bearing one. Lists and code blocks are exactly the node classes a
client-side Markdown parser is known to drop silently (atlassian/homebrew-acli#45), so it asserts
they exist as document nodes going out and that their content is still there coming back, rather
than asserting on the whole document — the converter's own cosmetic spacing choices are not a
contract, the surviving content is.
"""
from __future__ import annotations

import pytest

from .. import TrackerError
from . import adf

RICH_TEXT = """Intro paragraph.

- alpha
- beta

1. first
2. second

```python
def f(x):
    return x + 1
```
"""

CODE_BODY = "def f(x):\n    return x + 1"


def test_lists_and_code_blocks_survive_the_round_trip(capsys):
    doc = adf.markdown_to_adf(RICH_TEXT)

    types = [node.get("type") for node in doc["content"]]
    assert {"bulletList", "orderedList", "codeBlock"} <= set(types), f"nodes dropped on the way out: {types}"
    code = next(node for node in doc["content"] if node["type"] == "codeBlock")
    assert code["attrs"]["language"] == "python", f"the code block lost its language: {code.get('attrs')}"
    assert code["content"][0]["text"] == CODE_BODY, f"the code block body was altered: {code['content'][0]['text']!r}"

    markdown = adf.adf_to_markdown(doc)
    for item in ("alpha", "beta", "first", "second"):
        assert item in markdown, f"list item {item!r} was dropped coming back: {markdown!r}"
    assert CODE_BODY in markdown, f"the code block body was dropped coming back: {markdown!r}"
    assert "adf=" not in markdown, "the read path must not leak tracker-native markup into Markdown"
    assert capsys.readouterr().out == "", "nothing here may write to stdout: it carries JSON-RPC frames"


def test_a_second_pass_changes_nothing():
    """Comments get read, edited and written back, so conversion must reach a fixed point."""
    once = adf.adf_to_markdown(adf.markdown_to_adf(RICH_TEXT))
    twice = adf.adf_to_markdown(adf.markdown_to_adf(once))
    assert once == twice, f"conversion is not idempotent:\n{once!r}\n{twice!r}"


def test_empty_markdown_becomes_a_valid_empty_document():
    """Optional bodies pass straight through, so empty input is a document, not a failure."""
    for text in ("", "   \n\n"):
        assert adf.markdown_to_adf(text) == {"type": "doc", "version": 1, "content": []}, repr(text)


@pytest.mark.parametrize(
    "returned",
    ['{"type": "doc"}', {"type": "paragraph", "version": 1, "content": []}, {"type": "doc", "version": 1}],
    ids=["json-string", "not-a-doc-node", "no-content-list"],
)
def test_an_ill_shaped_conversion_is_refused(monkeypatch, returned):
    """The guard is only reachable by faking the converter, and it is what stops bad writes."""
    monkeypatch.setattr(adf, "to_adf", lambda md: returned)
    with pytest.raises(TrackerError, match="converter returned"):
        adf.markdown_to_adf("# heading")


def test_the_read_path_degrades_on_what_the_tracker_really_returns():
    assert adf.adf_to_markdown(None) == "", "a field with no body is empty text, not an error"
    assert adf.adf_to_markdown("legacy rendered body") == "legacy rendered body"
    with pytest.raises(TrackerError, match="got int"):
        adf.adf_to_markdown(42)


def test_a_document_the_converter_cannot_read_names_its_shape_only():
    broken = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": "sensitive body text"}]}
    with pytest.raises(TrackerError) as failure:
        adf.adf_to_markdown(broken)
    assert "sensitive body text" not in str(failure.value), "a failure message must not dump the document"
    assert "'doc'" in str(failure.value), f"the failure must name the shape it got: {failure.value}"
