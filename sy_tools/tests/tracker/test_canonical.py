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


def test_column_collisions_reports_the_same_collision_without_reporting_a_missing_column(monkeypatch):
    """What a validator needs and `column_names()` cannot give it: the collision, reported not raised.

    Same sentence as the refusal, because both come from one grouping — a validator quoting a different
    wording than the failure a session actually hits is worse than not checking. And a column that is
    simply unset is not this function's finding: the required-key check already reports it, and having
    this report it too named one fault twice for every unconfigured repo.
    """
    shared = {**COLUMNS, "columns.done": "created"}
    monkeypatch.setattr(tracker.config, "get", lambda key, *, default=None: shared.get(key, default))
    with pytest.raises(tracker.TrackerError) as failure:
        tracker.column_names()
    assert tracker.column_collisions() == [str(failure.value)], tracker.column_collisions()

    monkeypatch.setattr(tracker.config, "get", lambda key, *, default=None: {}.get(key, default))
    assert tracker.column_collisions() == [], "an unset column is the required-key check's to report"


def test_column_collisions_lets_a_refusing_config_read_propagate(monkeypatch):
    """A config that will not answer at all must not read as "no collisions" to the validator."""
    def refuses(key, *, default=None):
        raise tracker.config.ConfigError(f"config key {key!r} could not be read")

    monkeypatch.setattr(tracker.config, "get", refuses)
    with pytest.raises(tracker.config.ConfigError):
        tracker.column_collisions()


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


@pytest.mark.parametrize(
    "drift",
    ["field", "entry", "wrapper", "value"],
    ids=[
        "whole-field-of-the-wrong-shape", "one-malformed-entry",
        "a-relation-wrapper-neither-adapter-unwraps", "an-entry-whose-name-is-not-a-string",
    ],
)
@pytest.mark.parametrize("field", ["labels", "comments"])
def test_neither_adapter_shortens_a_labels_or_comments_field_it_cannot_read(field, drift):
    """`labels` and `comments` are read from both trackers, so one adapter's refusal is both adapters'.

    The two answer a single protocol, and the caller cannot see which tracker replied: `labels` is what
    decides whether an issue is already decomposed or already shipped, and `comments` is a thread a read
    reports as whole. A shape one side refuses and the other quietly drops is worse than either
    behaviour alone, because the same drift then produces a loud failure or a short list depending only
    on which tracker the repo happens to use. Both the whole field and one entry inside it are covered,
    since each was fixed on one adapter first and left on the other.

    The field readers are called directly rather than through the verbs: both transports are faked
    per-adapter in `test_github.py` and `test_jira.py`, where the end-to-end cases live, and duplicating
    either fake here would test the fake. What has to be asserted in one place is that the refusal is
    common to both.

    Four drifts, because field-and-entry shape was not the whole of the asymmetry. `{"nodes": [...]}` is
    the wrapper `gh` uses for its own relation lists, and a field-level guard that admitted any `dict`
    handed it to a parser that answers `[]` for a wrapper it cannot address — so the most plausible drift
    of all reported "no labels"/"no comments" through the guard written to make that impossible. And the
    two adapters disagreed on a non-string *value* in opposite directions: github coerced a label whose
    `name` was `3` into `"3"` while jira refused it, and github refused a string-shaped comment author
    while jira reported it as an absent one. Both are aligned on refusing, which is the direction the rest
    of these readers already take; a comment carries no name of its own, so the author is the field that
    drift is asserted on there.
    """
    from sy_tools.tracker.github import adapter as github
    from sy_tools.tracker.jira import adapter as jira

    base = "https://example.atlassian.net"
    gh_value, jira_value = {
        ("labels", "field"): ("not-a-list", "not-a-list"),
        ("labels", "entry"): ([{"name": "shipyard"}, 7], ["shipyard", 7]),
        ("labels", "wrapper"): ({"nodes": [{"name": "shipyard"}]}, {"nodes": ["shipyard"]}),
        ("labels", "value"): ([{"name": 3}], ["shipyard", 3]),
        ("comments", "field"): ("not-a-list", "not-a-list"),
        ("comments", "entry"): ([{"id": "1", "body": "x"}, 7], [{"id": "1"}, 7]),
        ("comments", "wrapper"): ({"nodes": [{"id": "1"}]}, {"nodes": [{"id": "1"}]}),
        ("comments", "value"): ([{"id": "1", "author": "alice"}], [{"id": "1", "author": "alice"}]),
    }[(field, drift)]
    readers = {
        ("labels", "github"): lambda: github._labels({"labels": gh_value}),
        ("labels", "jira"): lambda: jira._summary(base, "AM-1", {"labels": jira_value}),
        ("comments", "github"): lambda: github._comments({"comments": gh_value}),
        ("comments", "jira"): lambda: jira._comments("AM-1", {"comments": jira_value}),
    }
    for name in ("github", "jira"):
        with pytest.raises(tracker.TrackerError) as failure:
            readers[(field, name)]()
        assert field.removesuffix("s") in str(failure.value), (
            f"{name}'s refusal must name what it could not read: {failure.value}"
        )


def test_neither_adapter_drops_a_related_issue_it_cannot_name():
    """`dependencies` and `children` are relational lists on both sides, so one refusal is both refusals.

    github's `_refs` filtered a malformed entry out and returned the rest, with nothing saying one was
    dropped and — on the bare-list shape — no `totalCount` to cross-check the length against, while jira's
    `_keys` raised on the equivalent entry. A caller reads these to decide whether an issue is blocked or
    already decomposed, and cannot see which tracker replied, so a list that is quietly one issue short is
    the same fault the field-level guards either side already refuse.
    """
    from sy_tools.tracker.github import adapter as github
    from sy_tools.tracker.jira import adapter as jira

    with pytest.raises(tracker.TrackerError, match="entry 1"):
        github._refs([{"url": "https://github.com/o/r/issues/1"}, "junk"])
    with pytest.raises(tracker.TrackerError, match="entry 1"):
        jira._keys([{"key": "AM-1"}, "junk"], "subtasks")
    assert github._refs([{"url": "https://github.com/o/r/issues/1"}]) == ["https://github.com/o/r/issues/1"]
    assert jira._keys([{"key": "AM-1"}], "subtasks") == ["AM-1"], "and a well-formed list still reads"


def test_an_unknown_canonical_token_is_refused(columns):
    with pytest.raises(tracker.TrackerError, match="unknown canonical status"):
        tracker.native_status("blocked")
    with pytest.raises(tracker.TrackerError, match="unknown canonical type"):
        tracker.native_type("chore")
