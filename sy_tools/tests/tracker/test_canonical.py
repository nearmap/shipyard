"""The canonical status/type vocabulary shared by every adapter, in `sy_tools/tracker/__init__.py`.

Worth its own tests because both adapters now depend on one table: a change here moves issues on
every board at once, and the case-insensitive match plus the deliberate pass-through of an unmapped
column are the two behaviours a caller silently relies on.
"""
from __future__ import annotations

import pytest

from sy_tools import tracker

COLUMNS = {
    "columns.backlog": "Created",
    "columns.ready": "Ready for Build",
    "columns.in_progress": "In Progress",
    "columns.in_review": "In Review",
    "columns.done": "Closed",
}


@pytest.fixture
def columns(monkeypatch) -> None:
    """Column names as a repo really sets them, including one with different casing to the token."""
    monkeypatch.setattr(tracker.config, "get", lambda key, *, default=None: COLUMNS.get(key, default))


def test_the_five_lifecycle_columns_are_the_whole_vocabulary():
    assert set(tracker.STATUS_CONFIG_KEYS) == {"backlog", "ready", "in-progress", "in-review", "done"}
    assert set(tracker.TYPE_NAMES) == {"epic", "task", "bug"}


def test_a_column_name_round_trips_through_its_canonical_token(columns):
    for canonical, name in COLUMNS.items():
        token = canonical.removeprefix("columns.").replace("_", "-")
        assert tracker.native_status(token) == name, token
        assert tracker.canonical_status(name) == token, name


def test_matching_ignores_case_and_surrounding_space(columns):
    assert tracker.canonical_status("in progress") == "in-progress"
    assert tracker.canonical_status("  IN PROGRESS  ") == "in-progress"
    assert tracker.canonical_type("EPIC") == "epic"


def test_an_unmapped_column_passes_through_rather_than_being_dropped(columns):
    assert tracker.canonical_status("On Hold") == "On Hold", (
        "an issue parked in an extra column must still report where it is"
    )
    assert tracker.canonical_status(None) is None


def test_an_unset_column_name_fails_loudly_rather_than_defaulting(monkeypatch):
    """Guessing a column name would move an issue on whichever board happens to have that label."""
    partial = {**COLUMNS, "columns.in_review": ""}
    monkeypatch.setattr(tracker.config, "get", lambda key, *, default=None: partial.get(key, default))
    with pytest.raises(tracker.TrackerError, match=r"columns\.in_review"):
        tracker.column_names()


@pytest.mark.parametrize(
    "shared",
    [{"columns.ready": "In Progress"}, {"columns.ready": "in progress"}],
    ids=["identical", "differing-only-in-case"],
)
def test_two_statuses_sharing_one_column_name_is_refused_rather_than_first_match_wins(monkeypatch, shared):
    """`canonical_status` returns its first hit, so a shared name made one status unreachable silently.

    An issue sitting in that column read back as whichever canonical token happened to be checked
    first, and the other one could never be reported at all. Case is covered too, because that is how
    the match itself compares names.
    """
    monkeypatch.setattr(
        tracker.config, "get", lambda key, *, default=None: {**COLUMNS, **shared}.get(key, default)
    )
    with pytest.raises(tracker.TrackerError) as failure:
        tracker.column_names()

    message = str(failure.value)
    assert "columns.ready" in message and "columns.in_progress" in message, (
        f"the failure must name both colliding canonical keys: {message}"
    )
    assert "In Progress" in message or "in progress" in message, f"and the name they share: {message}"


def test_a_collision_fails_every_caller_not_just_the_one_that_reads_the_column(monkeypatch):
    """Detection sits in `column_names`, so the whole vocabulary refuses to resolve, not one lookup."""
    monkeypatch.setattr(
        tracker.config, "get", lambda key, *, default=None: {**COLUMNS, "columns.done": "Created"}.get(key, default)
    )
    with pytest.raises(tracker.TrackerError, match=r"columns\.backlog"):
        tracker.canonical_status("Created")
    with pytest.raises(tracker.TrackerError, match=r"columns\.backlog"):
        tracker.native_status("done")


def test_every_adapter_implements_the_whole_protocol():
    """Every Protocol attribute is present on both adapters. Signatures are *not* checked here.

    `TrackerAdapter` is `@runtime_checkable`, and `isinstance` against a runtime-checkable Protocol
    tests attribute presence only — so this catches a missing verb at any commit, and would not
    catch a verb whose parameters have drifted from the Protocol's. Argument-level wiring is pinned
    separately by `WIRING` in `sy_tools/tests/test_server.py`.

    Living here rather than in `test_server.py` because the import lines below name the concrete
    adapters, and `test_tracker_seam.py` exempts only this directory for exactly that reason.

    The set is pinned exactly, not counted: a lower bound let four verbs be dropped from the Protocol
    without failing. Fifteen methods serve the contract's eighteen verbs because `create-child`,
    `post-log` and `link-pr` are the `create_issue` and `post_comment` writes under another name.
    """
    from sy_tools.tracker.github.adapter import GithubAdapter
    from sy_tools.tracker.jira.adapter import JiraAdapter

    verbs = {v for v in vars(tracker.TrackerAdapter) if not v.startswith("_")}
    assert verbs == {
        "create_issue", "get_issue", "update_issue", "find_issues", "set_status", "assign",
        "link_parent", "add_dependency", "add_label", "post_comment", "attach_artifact", "preflight",
        "type_convert", "attachment_download", "attachment_update",
    }, f"the Protocol's verb set moved: {sorted(verbs)}"
    for adapter in (GithubAdapter(), JiraAdapter()):
        assert isinstance(adapter, tracker.TrackerAdapter), f"{type(adapter).__name__} is missing a canonical verb"


def test_an_unknown_canonical_token_is_refused(columns):
    with pytest.raises(tracker.TrackerError, match="unknown canonical status"):
        tracker.native_status("blocked")
    with pytest.raises(tracker.TrackerError, match="unknown canonical type"):
        tracker.native_type("chore")
