"""The durable memory store's behaviour, and the three tools that serve it.

The `store` fixture replaces the resolver call the module makes, so nothing here reads or writes the
operator's real user-global memory. The lesson text is a placeholder: the store sees only Markdown, so
a real lesson would just make these tests look content-dependent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sy_tools import memory, server


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    """Point `memory.dir` at a throwaway root, for the module and the tools alike."""
    monkeypatch.setattr(memory, "config_get", lambda key: str(tmp_path) if key == "memory.dir" else None)
    return tmp_path


def _lessons(store: Path) -> list[Path]:
    """The lesson files on disk, read straight off the filesystem rather than through the index."""
    return sorted(p for p in store.glob("*.md") if p.name != memory.INDEX_NAME)


def test_a_same_title_re_add_replaces_the_one_file(store):
    """Idempotent by title is the store's core write contract: a re-add must not fork the lesson."""
    first = memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Pass it explicitly.")
    again = memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Pass it explicitly.")
    assert first == again, f"same-title re-add must land on the same file: {first} vs {again}"
    memory.add("Review bot login differs per API surface", "code review", "bots", "Match by author type.")
    lessons = _lessons(store)
    assert len(lessons) == 2, f"expected 2 lessons after the idempotent re-add, got {len(lessons)}: {lessons}"


def test_search_matches_body_text_and_names_the_lesson(store):
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Pass it explicitly.")
    memory.add("Review bot login differs per API surface", "code review", "bots", "Match by author type.")
    hits = memory.search("model override")
    assert len(hits) == 1, f"one lesson carries that text, so one hit is expected: {hits}"
    assert "resume-drops-the-model-override" in hits[0], f"a hit must name the lesson's file: {hits[0]}"
    assert memory.search("no-such-token-anywhere") == [], "a miss must return no matches"


def test_the_index_rebuilds_on_read_when_it_is_missing(store):
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Pass it explicitly.")
    memory.add("Review bot login differs per API surface", "code review", "bots", "Match by author type.")
    (store / memory.INDEX_NAME).unlink()
    listing = memory.index_text()
    assert listing.count("- [") == 2, f"the index must rebuild on read when missing: {listing!r}"
    assert "Review bot login differs per API surface" in listing, listing


def test_a_hand_emptied_store_serves_no_entries_rather_than_ghosts(store):
    """The stale-index case: lessons can be deleted with a file manager, and often are."""
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Pass it explicitly.")
    for lesson in _lessons(store):
        lesson.unlink()
    assert "(no entries)" in memory.index_text(), "a stale index must not serve ghost entries"
    assert memory.search("model override") == [], "a deleted lesson must not still be searchable"


@pytest.mark.parametrize(
    ("title", "scope", "body"),
    [("", "agent dispatch", "Pass it explicitly."), ("Resume drops it", "", "Pass it explicitly."),
     ("Resume drops it", "agent dispatch", "   ")],
)
def test_an_empty_field_is_refused(store, title, scope, body):
    with pytest.raises(ValueError, match="non-empty"):
        memory.add(title, scope, "", body)


def test_a_title_with_no_slug_is_refused(store):
    """A title of punctuation alone would otherwise be stored under a nameless file."""
    with pytest.raises(ValueError, match="empty slug"):
        memory.add("!!! ???", "agent dispatch", "", "Pass it explicitly.")


def test_an_empty_search_term_is_refused(store):
    with pytest.raises(ValueError, match="non-empty"):
        memory.search("  ")


def test_the_tools_round_trip_one_lesson_through_the_store(store):
    added = server.memory_add("Resume drops the model override", "agent dispatch", "Pass it explicitly.", "resume")
    assert added["path"] == str(store / "resume-drops-the-model-override.md"), added

    found = server.memory_search("model override")
    assert found["root"] == str(store), f"a zero-match answer is only diagnosable if the root is reported: {found}"
    assert len(found["matches"]) == 1, found

    listed = server.memory_list()
    assert "Resume drops the model override" in listed["index"], listed


@pytest.mark.parametrize(
    "call",
    [lambda: server.memory_add("", "agent dispatch", "Pass it explicitly."),
     lambda: server.memory_add("!!! ???", "agent dispatch", "Pass it explicitly."),
     lambda: server.memory_search("  ")],
)
def test_a_tool_surfaces_bad_input_as_a_tool_error(store, call):
    """A `ValueError` out of the library would reach the client as a protocol-level failure instead."""
    with pytest.raises(server.ToolError):
        call()
