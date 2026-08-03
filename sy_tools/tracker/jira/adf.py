"""Markdown in, Jira rich text out — converted in this process.

The conversion is two function calls in the server's own process, with no staging file and no
converter to provision: `marklas` is a declared dependency of this package and is locked in
`pixi.lock`. It replaced a shipped CLI helper that re-executed itself inside a hash-locked virtual
environment on every comment, because a skill script could not assume the converter was importable —
a subprocess hop that bought nothing here and cost an interpreter start per write.

Every failure is a `TrackerError`, never a `SystemExit`, which would end a server process that still
has other calls to serve. Nothing here writes to stdout — stdout carries JSON-RPC frames.

The read path converts with `plain=True`, dropping the `adf="…"` attributes `marklas` uses to make
its own round-trip lossless. `skills/tracker/CONTRACT.md` says rich text crossing this seam is
Markdown, so tracker-native markup must not ride out on it.
"""
from __future__ import annotations

from typing import Any, cast

from marklas import to_adf, to_md

from .. import TrackerError


def markdown_to_adf(text: str) -> dict:
    """`text` as a Jira Atlassian Document Format document, proven well-shaped before it is sent.

    Empty or whitespace-only `text` converts to a valid empty document rather than failing:
    callers pass optional bodies straight through here.
    """
    try:
        doc = to_adf(text)
    except Exception as exc:
        raise TrackerError(f"Markdown could not be converted for the tracker ({type(exc).__name__}: {exc})") from exc
    if not isinstance(doc, dict):
        raise TrackerError(f"the Markdown converter returned {type(doc).__name__}, not a rich-text document object")
    if doc.get("type") != "doc" or doc.get("version") != 1 or not isinstance(doc.get("content"), list):
        raise TrackerError(
            "the Markdown converter returned an object that is not a top-level document node: "
            f"type={doc.get('type')!r}, version={doc.get('version')!r}"
        )
    return doc


def adf_to_markdown(doc: object, *, plain: bool = True) -> str:
    """A rich-text document from Jira as Markdown, tolerating the shapes Jira actually returns.

    A field with no body reads back as `None` and some rendered reads come back as a plain string;
    both degrade to a string instead of failing, because an empty description is not an error.
    `plain=False` keeps the converter's round-trip attributes, which no caller above this seam wants.
    """
    if doc is None:
        return ""
    if isinstance(doc, str):
        return doc
    if not isinstance(doc, dict):
        raise TrackerError(
            f"tracker rich text could not be read: expected a document object, got {type(doc).__name__}"
        )
    try:
        text = to_md(cast(dict[str, Any], doc), plain=plain)
    except Exception as exc:
        raise TrackerError(
            f"tracker rich text could not be read: the converter failed on a "
            f"{str(doc.get('type', 'untyped'))[:40]!r} node ({type(exc).__name__}: {exc})"
        ) from exc
    if not isinstance(text, str):
        raise TrackerError(f"the rich-text converter returned {type(text).__name__}, not Markdown")
    return text
