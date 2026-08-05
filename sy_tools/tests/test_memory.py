"""The durable memory store's behaviour, and the four tools that serve it.

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
    """The stale-index case: hand-deleting is unsupported, so a vanished file is corruption to absorb."""
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


@pytest.mark.parametrize(
    ("title", "scope", "tags"),
    [("Resume\ndrops it", "agent dispatch", "resume"), ("Resume drops it", "agent\ndispatch", "resume"),
     ("Resume drops it", "agent dispatch", "resume\nmodels"),
     ("Resume\rdrops it", "agent dispatch", "resume"), ("Resume drops it", "agent\rdispatch", "resume"),
     ("Resume drops it", "agent dispatch", "resume\rmodels")],
)
def test_a_newline_in_a_frontmatter_field_is_refused(store, title, scope, tags):
    """Written verbatim onto one frontmatter line, either break truncates the value and orphans its remainder.

    A bare `\\r` corrupts identically: it survives the write, then reads back as `\\n`.
    """
    with pytest.raises(ValueError, match="no newline"):
        memory.add(title, scope, tags, "Pass it explicitly.")
    assert _lessons(store) == [], "a refused add must write nothing"


@pytest.mark.parametrize(
    ("title", "scope", "tags"),
    [("Resume drops it\n", "agent dispatch", "resume"), ("Resume drops it", "\nagent dispatch", "resume"),
     ("Resume drops it", "agent dispatch", "resume\r\n")],
)
def test_a_leading_or_trailing_newline_is_stripped_rather_than_refused(store, title, scope, tags):
    """Only the stripped value is ever written, so an edge break is not the corruption the guard is for."""
    path = memory.add(title, scope, tags, "Pass it explicitly.")
    assert "title: Resume drops it\nscope: agent dispatch\ntags: resume\n" in path.read_text(encoding="utf-8")


def test_a_newline_in_a_refuted_title_is_refused(store):
    """refute() writes the title to frontmatter too when the stored file carries none, so it guards it too.

    It guards `title` alone, so the refusal must not name the `scope`/`tags` a refute caller cannot pass.
    """
    memory.add("Resume drops it", "agent dispatch", "resume", "Pass it explicitly.")
    with pytest.raises(ValueError, match=r"no newline: title$"):
        memory.refute("Resume\ndrops it", "The 2.1 release fixed it.")
    assert memory.refute("Resume drops it\n", "The 2.1 release fixed it.").is_file(), "an edge break strips"


def test_a_title_with_no_slug_is_refused(store):
    """A title of punctuation alone would otherwise be stored under a nameless file."""
    with pytest.raises(ValueError, match="empty slug"):
        memory.add("!!! ???", "agent dispatch", "", "Pass it explicitly.")


def test_an_empty_search_term_is_refused(store):
    with pytest.raises(ValueError, match="non-empty"):
        memory.search("  ")


def test_a_correction_rewrites_the_body_and_keeps_the_refuted_claim_readable(store):
    """Correction mode is the preferred one: the condition under which the lesson failed is the payload."""
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Always pass it explicitly.")
    path = memory.refute(
        "Resume drops the model override",
        "sy_tools/server.py:966 shows the override surviving a resume.",
        correction="Only a nested Agent call drops it; a resumed top-level dispatch keeps it.",
    )
    text = path.read_text(encoding="utf-8")
    assert "\nstatus: corrected\n" in text, text
    assert "\nscope: agent dispatch\n" in text, "scope must survive a refutation"
    assert "\ntags: resume,models\n" in text, "tags must survive a refutation"
    assert "Only a nested Agent call drops it" in text, text
    assert "Always pass it explicitly." in text, "the refuted claim must stay readable, not be erased"


def test_an_empty_correction_tombstones_the_lesson_without_deleting_it(store):
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Always pass it explicitly.")
    path = memory.refute("Resume drops the model override", "Never reproduced on any surface.")
    assert path.is_file(), "a tombstone must rewrite the lesson file, never remove it"
    text = path.read_text(encoding="utf-8")
    assert "\nstatus: tombstoned\n" in text, text
    assert "Always pass it explicitly." in text, "a tombstone must still show what was refuted"


def test_refuting_the_same_title_twice_neither_forks_the_file_nor_renests_the_claim(store):
    """The idempotency bar `memory_add` sets: converge on the one file, and stay readable across repeats."""
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Always pass it explicitly.")
    first = memory.refute("Resume drops the model override", "First look.", correction="Nested calls only.")
    again = memory.refute("Resume drops the model override", "Second look.", correction="Top-level calls too.")
    assert first == again, f"a repeat refute must land on the same file: {first} vs {again}"
    assert len(_lessons(store)) == 1, f"a repeat refute must not fork the lesson: {_lessons(store)}"
    text = again.read_text(encoding="utf-8")
    assert text.count(memory.REFUTED_HEADING) == 1, f"the preserved claim must not re-nest per refute: {text!r}"
    assert "Always pass it explicitly." in text, "the original claim, not the last refutation, is preserved"
    assert "Nested calls only." not in text, "a superseded correction is replaced, not accumulated"


def test_a_body_that_already_contains_the_refuted_heading_survives_a_refute_intact(store):
    """The heading is only refute()'s own marker on an already-refuted entry; in a fresh body it is prose."""
    body = f"Always pass it explicitly.\n\n{memory.REFUTED_HEADING}\n\nAn earlier note quoted verbatim."
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", body)
    first = memory.refute("Resume drops the model override", "First look.", correction="Nested calls only.")
    text = first.read_text(encoding="utf-8")
    assert body in text, f"a pre-existing heading must not partition the claim away: {text!r}"

    memory.refute("Resume drops the model override", "First look.", correction="Nested calls only.")
    assert first.read_text(encoding="utf-8") == text, "refuting an already-refuted entry again must be a no-op"


def test_a_refuted_lesson_stays_visible_in_the_index_and_in_search(store):
    """The whole point of rewriting rather than deleting: the wrong conclusion cannot be silently redone."""
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Always pass it explicitly.")
    memory.refute("Resume drops the model override", "Never reproduced.")
    listing = memory.index_text()
    assert "Resume drops the model override" in listing, listing
    assert "status: tombstoned" in listing, f"the index must carry a refuted entry's status: {listing!r}"
    hits = memory.search("model override")
    assert len(hits) == 1, "a refuted lesson must remain a search hit"
    assert "status: tombstoned" in hits[0], f"a hit on a refuted lesson must not read as a live one: {hits[0]}"


def test_a_search_hit_carries_a_correction_status_and_an_unrefuted_one_carries_none(store):
    """A hit line is all a caller sees before deciding to open the file, so the status has to travel with it."""
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Always pass it explicitly.")
    memory.add("Review bot login differs per API surface", "code review", "bots", "Match by author type.")
    memory.refute("Resume drops the model override", "Seen surviving a resume.", correction="Nested calls only.")
    hits = {hit.split(": ", 1)[1] for hit in memory.search("e")}
    assert hits == {
        "Resume drops the model override (status: corrected)", "Review bot login differs per API surface",
    }, f"only the refuted hit carries a status: {hits}"


def test_an_unrefuted_lesson_carries_no_status_in_the_index(store):
    """Backward compatibility: a pre-refute lesson file lists exactly as it did before."""
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Always pass it explicitly.")
    assert "status:" not in memory.index_text(), memory.index_text()


def test_a_body_line_that_looks_like_frontmatter_is_not_read_as_frontmatter(store):
    """The helpers scan the `---` block only; otherwise a lesson *about* status would fake its own."""
    memory.add(
        "Refuted entries carry a status",
        "memory",
        "memory",
        "A refuted lesson gets\nstatus: tombstoned\nin its frontmatter, and\ntitle: something else\ndoes not apply.",
    )
    text = (store / "refuted-entries-carry-a-status.md").read_text(encoding="utf-8")
    assert memory._frontmatter_value(text, "status") == "", "a body line must never be read as a frontmatter value"
    assert memory._title_of(text, store / "x.md") == "Refuted entries carry a status", "the body must not shadow title"
    listing = memory.index_text()
    assert "status:" not in listing, f"the index must not pick a status out of a lesson body: {listing!r}"


def test_an_empty_frontmatter_value_does_not_read_the_next_key(store):
    """A lesson added without tags leaves `tags:` empty; reading it must not return the `date:` line.

    Refuting persists what this returns back into frontmatter, so the bleed would corrupt the entry.
    """
    memory.add("Resume drops the model override", "agent dispatch", "", "Always pass it explicitly.")
    text = (store / "resume-drops-the-model-override.md").read_text(encoding="utf-8")
    assert memory._frontmatter_value(text, "tags") == "", "an empty value must read as empty, not as the next key"
    assert "tags:" not in memory.index_text(), memory.index_text()
    refuted = memory.refute("Resume drops the model override", "Never reproduced.").read_text(encoding="utf-8")
    assert "tags: date:" not in refuted, f"a refutation must not persist a bled-over value: {refuted!r}"


@pytest.mark.parametrize(
    ("title", "evidence"),
    [("", "Observed directly."), ("Resume drops the model override", "   ")],
)
def test_a_refutation_without_a_title_or_evidence_is_refused(store, title, evidence):
    memory.add("Resume drops the model override", "agent dispatch", "resume,models", "Always pass it explicitly.")
    with pytest.raises(ValueError, match="non-empty"):
        memory.refute(title, evidence)


def test_refuting_a_title_that_is_not_stored_is_refused(store):
    """Refuting a title into existence would record the correction as an unevidenced fact."""
    with pytest.raises(ValueError, match="nothing to refute"):
        memory.refute("No lesson was ever stored here", "Observed directly.")


def test_the_tools_round_trip_one_lesson_through_the_store(store):
    added = server.memory_add("Resume drops the model override", "agent dispatch", "Pass it explicitly.", "resume")
    assert added["path"] == str(store / "resume-drops-the-model-override.md"), added

    found = server.memory_search("model override")
    assert found["root"] == str(store), f"a zero-match answer is only diagnosable if the root is reported: {found}"
    assert len(found["matches"]) == 1, found

    listed = server.memory_list()
    assert "Resume drops the model override" in listed["index"], listed

    refuted = server.memory_refute("Resume drops the model override", "Never reproduced.", "Nested calls only.")
    assert refuted["path"] == added["path"], f"a refutation must rewrite the lesson in place: {refuted} vs {added}"
    assert "status: corrected" in server.memory_list()["index"], server.memory_list()
    assert len(server.memory_search("model override")["matches"]) == 1, "a refuted lesson stays searchable"


@pytest.mark.parametrize(
    "call",
    [lambda: server.memory_add("", "agent dispatch", "Pass it explicitly."),
     lambda: server.memory_add("!!! ???", "agent dispatch", "Pass it explicitly."),
     lambda: server.memory_search("  "),
     lambda: server.memory_refute("No lesson was ever stored here", "Observed directly."),
     lambda: server.memory_refute("Resume drops the model override", "  ")],
)
def test_a_tool_surfaces_bad_input_as_a_tool_error(store, call):
    """A `ValueError` out of the library would reach the client as a protocol-level failure instead."""
    with pytest.raises(server.ToolError):
        call()
