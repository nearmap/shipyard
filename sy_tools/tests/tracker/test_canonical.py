"""The canonical status/type vocabulary shared by every adapter, in `sy_tools/tracker/__init__.py`.

One table drives both adapters, so a change here moves issues on every board at once.
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
    # a case-differing name is a distinct case: the collision check compares names case-insensitively
    [{"columns.ready": "In Progress"}, {"columns.ready": "in progress"}],
    ids=["identical", "differing-only-in-case"],
)
def test_two_statuses_sharing_one_column_name_is_refused_rather_than_first_match_wins(monkeypatch, shared):
    """`canonical_status` returns its first hit, so a shared name made one status unreachable silently."""
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
    """What a validator needs and `column_names()` cannot give it: the collision, reported not raised."""
    shared = {**COLUMNS, "columns.done": "created"}
    monkeypatch.setattr(tracker.config, "get", lambda key, *, default=None: shared.get(key, default))
    with pytest.raises(tracker.TrackerError) as failure:
        tracker.column_names()
    # one grouping feeds both: a validator quoting different wording to the session's own failure is
    # worse than not checking at all.
    assert tracker.column_collisions() == [str(failure.value)], tracker.column_collisions()

    monkeypatch.setattr(tracker.config, "get", lambda key, *, default=None: {}.get(key, default))
    # reporting an unset column here too would name one fault twice for every unconfigured repo
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

    `isinstance` against a runtime-checkable Protocol tests attribute presence only, so a verb whose
    parameters drifted still passes; argument wiring is pinned by `WIRING` in `sy_tools/tests/test_server.py`.
    """
    # imported in-function, not at module scope: naming a concrete adapter is legal only in this directory
    from sy_tools.tracker.github.adapter import GithubAdapter
    from sy_tools.tracker.jira.adapter import JiraAdapter

    verbs = {v for v in vars(tracker.TrackerAdapter) if not v.startswith("_")}
    # pinned exactly, not counted: a lower bound let four verbs be dropped. Fifteen methods serve eighteen
    # canonical verbs — `create-child`, `post-log` and `link-pr` are `create_issue`/`post_comment` renamed.
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

    The caller cannot see which tracker replied, so a shape one side refuses and the other quietly drops
    turns one drift into a loud failure or a short list depending only on which tracker a repo uses.
    """
    from sy_tools.tracker.github import adapter as github
    from sy_tools.tracker.jira import adapter as jira

    base = "https://example.atlassian.net"
    gh_value, jira_value = {
        ("labels", "field"): ("not-a-list", "not-a-list"),
        ("labels", "entry"): ([{"name": "shipyard"}, 7], ["shipyard", 7]),
        # `{"nodes": [...]}` is `gh`'s own relation wrapper, so it is the most plausible drift of all: a
        # guard admitting any `dict` handed it to a parser that answers `[]` for a wrapper it cannot address.
        ("labels", "wrapper"): ({"nodes": [{"name": "shipyard"}]}, {"nodes": ["shipyard"]}),
        # measured: github coerced a `name` of `3` into `"3"` where jira refused it — aligned on refusing
        ("labels", "value"): ([{"name": 3}], ["shipyard", 3]),
        ("comments", "field"): ("not-a-list", "not-a-list"),
        ("comments", "entry"): ([{"id": "1", "body": "x"}, 7], [{"id": "1"}, 7]),
        ("comments", "wrapper"): ({"nodes": [{"id": "1"}]}, {"nodes": [{"id": "1"}]}),
        # a comment carries no name, so the author is where a non-string value shows: measured, github
        # refused a string-shaped author while jira reported it as an absent one.
        ("comments", "value"): ([{"id": "1", "author": "alice"}], [{"id": "1", "author": "alice"}]),
    }[(field, drift)]
    # the field readers are called directly: both transports are faked per-adapter in the two adapter
    # modules, and duplicating either fake here would test the fake.
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

    Catches one side dropping a malformed entry and returning the rest: a list quietly one issue short
    reads, to a caller who cannot see which tracker replied, as "not blocked" or "not decomposed".
    """
    from sy_tools.tracker.github import adapter as github
    from sy_tools.tracker.jira import adapter as jira

    # the countless shape on purpose: where github's relation carries a `totalCount`, `_refs` skips the
    # entry and `_relation` reports the shortfall as truncated instead (pinned in `test_github.py`).
    with pytest.raises(tracker.TrackerError, match="entry 1"):
        github._refs([{"url": "https://github.com/o/r/issues/1"}, "junk"])
    with pytest.raises(tracker.TrackerError, match="entry 1"):
        jira._keys([{"key": "AM-1"}, "junk"], "subtasks")
    assert github._refs([{"url": "https://github.com/o/r/issues/1"}]) == (["https://github.com/o/r/issues/1"], 0)
    assert jira._keys([{"key": "AM-1"}], "subtasks") == ["AM-1"], "and a well-formed list still reads"


def test_an_unknown_canonical_token_is_refused(columns):
    with pytest.raises(tracker.TrackerError, match="unknown canonical status"):
        tracker.native_status("blocked")
    with pytest.raises(tracker.TrackerError, match="unknown canonical type"):
        tracker.native_type("chore")
