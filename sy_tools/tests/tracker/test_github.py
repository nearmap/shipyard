"""Offline tests for the GitHub adapter: the `gh` transport is replaced, never invoked.

The verbs are awaited, but the seam under test is unchanged: the sync helpers still call
`subprocess.run`, so monkeypatching `adapter.subprocess` still intercepts every `gh` call — now
from the worker thread the verb offloads to.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import threading

import pytest

from sy_tools.tracker import TIMEOUT_SECONDS, TrackerError
from sy_tools.tracker.github import adapter

GIST_URL = "https://gist.github.com/octocat/abc123"
COMMENT_URL = "https://github.com/octocat/repo/issues/7#issuecomment-1"
REPO = "octocat/repo"
PROJECT = "@me/3"
ISSUE_URL = "https://github.com/octocat/repo/issues/7"
PARENT_URL = "https://github.com/octocat/repo/issues/5"
CHILD_URL = "https://github.com/octocat/repo/issues/9"
BLOCKER_URL = "https://github.com/octocat/repo/issues/4"
TITLE = "Do the thing"
COLUMNS = {
    "columns.backlog": "Backlog",
    "columns.ready": "Ready",
    "columns.in_progress": "In Progress",
    "columns.in_review": "In review",
    "columns.done": "Done",
}
PROJECT_VIEW = {"id": "PVT_1", "number": 3, "title": "Shipyard"}
REPO_ARGS = ["--repo", REPO]
OWNER_ARGS = ["3", "--owner", "@me", "--format", "json"]


@pytest.fixture
def anyio_backend() -> str:
    """The single event loop implementation these tests run on; required by anyio's plugin."""
    return "asyncio"


class _FakeSubprocess:
    """Stands in for the `subprocess` module inside the adapter: records argv, replays results."""

    def __init__(self, *results: tuple[int, str, str]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []
        self.threads: list[int] = []
        self.PIPE = subprocess.PIPE
        self.TimeoutExpired = subprocess.TimeoutExpired

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.kwargs.append(dict(kwargs))
        self.threads.append(threading.get_ident())
        assert self.results, f"unexpected extra gh call: {argv}"
        code, out, err = self.results.pop(0)
        return subprocess.CompletedProcess(argv, code, out, err)


def _install(monkeypatch: pytest.MonkeyPatch, *results: tuple[int, str, str]) -> _FakeSubprocess:
    fake = _FakeSubprocess(*results)
    monkeypatch.setattr(adapter, "subprocess", fake)
    return fake


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> None:
    """The config every board-touching verb reads: this repo's column names, the board and the repo.

    One fixture rather than per-test patching, because the `gh` call sequence a verb makes depends
    on these values and a test that quietly disagrees with another about them proves nothing.
    """
    values = {**COLUMNS, "tracker_config.project": PROJECT, "tracker_config.repo": REPO}
    original = adapter.config.get

    def fake_get(path: str, **kwargs: object) -> object:
        return values[path] if path in values else original(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter.config, "get", fake_get)


def _json(payload: object) -> tuple[int, str, str]:
    """One queued `gh` result whose stdout is `payload` as JSON."""
    return (0, json.dumps(payload), "")


def _fields(*, status_options: tuple[str, ...] = ("Backlog", "In progress", "Done")) -> dict:
    """A `field-list` payload: two single-selects plus a text field that must be ignored."""
    return {
        "fields": [
            {
                "id": "F_status",
                "name": "Status",
                "options": [{"id": f"o_{name.split()[-1].lower()}", "name": name} for name in status_options],
            },
            {
                "id": "F_type",
                "name": "Type",
                "options": [{"id": "o_epic", "name": "Epic"}, {"id": "o_task", "name": "Task"}],
            },
            {"id": "F_title", "name": "Title", "type": "ProjectV2Field"},
        ]
    }


def _items(*, status: str = "Backlog", issue_type: str = "Task", present: bool = True) -> dict:
    """An `item-list` payload holding the issue under test, or an empty board."""
    if not present:
        return {"items": [], "totalCount": 0}
    item = {
        "id": "ITEM_1",
        "status": status,
        "type": issue_type,
        "content": {"number": 7, "title": TITLE, "url": ISSUE_URL},
    }
    return {"items": [item], "totalCount": 1}


def _issue_view() -> dict:
    """A `gh issue view --json` payload with every relation the canonical read reports."""
    return {
        "number": 7,
        "title": TITLE,
        "body": "## Context\n\nMarkdown in, Markdown out.\n",
        "url": ISSUE_URL,
        "labels": [{"name": "shipyard"}, {"name": "bug"}],
        "parent": {"number": 5, "url": PARENT_URL, "title": "The epic"},
        "subIssues": {"nodes": [{"number": 9, "url": CHILD_URL}], "totalCount": 1},
        "blockedBy": {"nodes": [{"number": 4, "url": BLOCKER_URL}], "totalCount": 1},
        "comments": [
            {"id": "IC_1", "author": {"login": "octocat"}, "createdAt": "2026-07-31T00:00:00Z", "body": "a note"}
        ],
    }


def _list_row(number: int = 7, url: str = ISSUE_URL) -> dict:
    """The same issue as `gh issue list` reports it: the shared keys only."""
    return {
        "number": number,
        "title": TITLE,
        "url": url,
        "labels": [{"name": "shipyard"}, {"name": "bug"}],
        "parent": {"number": 5, "url": PARENT_URL},
    }


def _artifact(tmp_path: Path) -> Path:
    path = tmp_path / "AM-1-transcript.txt"
    path.write_text("already sanitised\n", encoding="utf-8")
    return path


def _happy_path(secret: bool = True) -> tuple[tuple[int, str, str], ...]:
    return (
        (0, f"{GIST_URL}\n", ""),
        (0, json.dumps({"id": "abc123", "public": not secret}), ""),
        (0, f"{COMMENT_URL}\n", ""),
    )


@pytest.mark.anyio
async def test_attach_artifact_gists_the_file_then_links_it_from_a_comment(tmp_path, monkeypatch):
    fake = _install(monkeypatch, *_happy_path())
    path = _artifact(tmp_path)

    evidence = await adapter.GithubAdapter().attach_artifact("AM-1", path)

    create, verify, comment = fake.calls
    assert create[:3] == ["gh", "gist", "create"], create
    assert str(path) in create, "the gist must be created from the artifact file itself"
    assert verify[:3] == ["gh", "api", "gists/abc123"], "privacy must be re-read, not assumed"
    assert comment[:4] == ["gh", "issue", "comment", "AM-1"], comment
    body = comment[comment.index("--body") + 1]
    assert GIST_URL in body, "the comment must carry the gist URL, or the artifact is undiscoverable"
    assert evidence["gist_url"] == GIST_URL, "evidence must report the URL the transport produced"
    assert evidence["comment_url"] == COMMENT_URL, "evidence must report the comment the write returned"


@pytest.mark.anyio
async def test_the_blocking_gh_work_runs_off_the_event_loop_thread(tmp_path, monkeypatch):
    """The offload must be real: `gh` blocks, so it may not block the loop that serves other calls."""
    fake = _install(monkeypatch, *_happy_path())

    await adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    loop_thread = threading.get_ident()
    assert fake.threads, "no gh call was recorded, so nothing was proved about where it ran"
    assert all(ident != loop_thread for ident in fake.threads), (
        f"gh ran on the event loop thread ({loop_thread}); the verb is async in name only and a "
        f"slow attachment would block every other tool call. Observed: {fake.threads}"
    )


@pytest.mark.anyio
async def test_the_gist_is_never_created_public(tmp_path, monkeypatch):
    fake = _install(monkeypatch, *_happy_path())

    await adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert "--public" not in fake.calls[0], (
        "gh gists are secret by default; passing --public would publish the transcript irrevocably"
    )


@pytest.mark.anyio
async def test_a_gist_that_reads_back_public_is_refused_before_any_comment(tmp_path, monkeypatch):
    fake = _install(monkeypatch, *_happy_path(secret=False))

    with pytest.raises(TrackerError, match="public"):
        await adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert len(fake.calls) == 2, "a public gist must not be linked from the work item"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "reason"),
    [((0, "", ""), "empty output"), ((1, "", "HTTP 422"), "non-zero exit")],
)
async def test_a_failed_gist_call_is_never_a_silent_success(tmp_path, monkeypatch, result, reason):
    fake = _install(monkeypatch, result)

    with pytest.raises(TrackerError):
        await adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert len(fake.calls) == 1, f"{reason} must stop the attachment, not fall through to a comment"


@pytest.mark.anyio
async def test_a_credential_in_command_output_never_reaches_the_error_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIPYARD_TEST_TOKEN", "s3cr3t-value-not-for-logs")
    _install(monkeypatch, (1, "", "bad credentials: s3cr3t-value-not-for-logs"))

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert "s3cr3t-value-not-for-logs" not in str(raised.value), "a held credential leaked into an error"


@pytest.mark.anyio
async def test_extra_redaction_words_apply_to_error_messages(tmp_path, monkeypatch):
    """`redaction.extra_words` must redact command output here exactly as on the attach path."""
    monkeypatch.setenv("NM_BEARER", "org-secret-value-9f8e7d6c")
    monkeypatch.setattr(adapter.config, "extra_secret_words", lambda: frozenset({"BEARER"}))
    _install(monkeypatch, (1, "", "bad credentials: org-secret-value-9f8e7d6c"))

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert "org-secret-value-9f8e7d6c" not in str(raised.value), "an org-named credential leaked"


@pytest.mark.anyio
async def test_preflight_reports_facts_without_the_token(monkeypatch):
    status = (
        "github.com\n  ✓ Logged in to github.com account octocat (keyring)\n"
        "  - Token: gho_exampletokenvalue\n  - Token scopes: 'gist', 'project', 'repo'\n"
    )
    _install(monkeypatch, (0, "gh version 2.94.0 (2025-01-01)\n", ""), (0, status, ""))

    facts = await adapter.GithubAdapter().preflight()

    assert facts["account"] == "octocat" and facts["scopes"] == ["gist", "project", "repo"], facts
    assert "gho_exampletokenvalue" not in json.dumps(facts), "preflight must never return a token"


@pytest.mark.anyio
async def test_a_hung_gh_is_bounded_and_becomes_an_actionable_failure(tmp_path, monkeypatch):
    """A `gh` that never returns must not wedge a server that has other calls to serve."""
    fake = _install(monkeypatch, *_happy_path())

    await adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert all(call["timeout"] == TIMEOUT_SECONDS for call in fake.kwargs), fake.kwargs

    class _Hangs(_FakeSubprocess):
        def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, TIMEOUT_SECONDS)

    monkeypatch.setattr(adapter, "subprocess", _Hangs())
    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().preflight()
    assert str(TIMEOUT_SECONDS) in str(raised.value), "the failure must name the bound it hit"


@pytest.mark.anyio
async def test_missing_gh_is_an_actionable_preflight_failure(monkeypatch):
    class _Missing:
        PIPE = subprocess.PIPE

        def run(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(argv[0])

    monkeypatch.setattr(adapter, "subprocess", _Missing())

    with pytest.raises(TrackerError, match="not installed"):
        await adapter.GithubAdapter().preflight()


@pytest.mark.anyio
async def test_set_status_resolves_the_board_case_insensitively_and_reads_the_move_back(monkeypatch, board):
    """The board spells the column `In progress`; this repo's config spells it `In Progress`."""
    fake = _install(
        monkeypatch,
        _json({"url": ISSUE_URL}),
        _json(PROJECT_VIEW),
        _json(_fields()),
        _json(_items()),
        (0, "", ""),
        _json(_items(status="in progress")),
    )

    moved = await adapter.GithubAdapter().set_status("7", "in-progress")

    assert [call[1:] for call in fake.calls] == [
        ["issue", "view", "7", *REPO_ARGS, "--json", "url"],
        ["project", "view", *OWNER_ARGS],
        ["project", "field-list", *OWNER_ARGS],
        ["project", "item-list", *OWNER_ARGS, "--limit", "10000"],
        ["project", "item-edit", "--id", "ITEM_1", "--project-id", "PVT_1",
         "--field-id", "F_status", "--single-select-option-id", "o_progress"],
        ["project", "item-list", *OWNER_ARGS, "--limit", "10000"],
    ], fake.calls
    assert moved == {"id": ISSUE_URL, "status": "in-progress", "native": "In Progress"}, moved


@pytest.mark.anyio
async def test_a_board_write_is_re_read_until_the_eventually_consistent_list_catches_up(monkeypatch, board):
    """Found live: the item list can miss a card that was just added, then show it a second later.

    Failing on the first read reported a write that had landed as a failure. The first two reads here
    are what a lagging board really returns — the card absent entirely, then present but unset — and
    the verb must still succeed on the third.
    """
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    fake = _install(
        monkeypatch,
        _json({"url": ISSUE_URL}),
        _json(PROJECT_VIEW),
        _json(_fields()),
        _json(_items()),
        (0, "", ""),
        _json(_items(present=False)),
        _json({"items": [{"id": "ITEM_1", "content": {"number": 7, "url": ISSUE_URL}}], "totalCount": 1}),
        _json(_items(status="In progress")),
    )

    moved = await adapter.GithubAdapter().set_status("7", "in-progress")

    assert moved == {"id": ISSUE_URL, "status": "in-progress", "native": "In Progress"}, moved
    reads = [call for call in fake.calls if call[1:3] == ["project", "item-list"]]
    assert len(reads) == 4, f"one read to find the card plus three verifying reads: {fake.calls}"


@pytest.mark.anyio
async def test_a_field_that_never_reads_back_is_a_bounded_failure_not_a_hang(monkeypatch, board):
    """The retry must not swallow a write that genuinely did not take."""
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    fake = _install(
        monkeypatch,
        _json({"url": ISSUE_URL}),
        _json(PROJECT_VIEW),
        _json(_fields()),
        _json(_items()),
        (0, "", ""),
        *([_json(_items(status="Backlog"))] * adapter.VERIFY_ATTEMPTS),
    )

    with pytest.raises(TrackerError, match="treat the board update as failed"):
        await adapter.GithubAdapter().set_status("7", "in-progress")

    verifying = [call for call in fake.calls if call[1:3] == ["project", "item-list"]]
    assert len(verifying) == 1 + adapter.VERIFY_ATTEMPTS, f"the retry must be bounded: {len(verifying)} reads"


@pytest.mark.anyio
async def test_an_option_missing_from_the_cached_board_is_re_resolved_before_it_is_a_failure(monkeypatch, board):
    """A column added after the board was resolved must work without restarting the server."""
    stale = _fields(status_options=("Backlog", "Done"))
    fake = _install(
        monkeypatch,
        _json({"url": ISSUE_URL}),
        _json(PROJECT_VIEW),
        _json(stale),
        _json(PROJECT_VIEW),
        _json(_fields()),
        _json(_items()),
        (0, "", ""),
        _json(_items(status="In progress")),
    )

    await adapter.GithubAdapter().set_status("7", "in-progress")

    field_lists = [call for call in fake.calls if call[1:3] == ["project", "field-list"]]
    assert len(field_lists) == 2, (
        f"a lookup miss must re-resolve the board once before failing; saw {len(field_lists)} field-list calls"
    )


@pytest.mark.anyio
async def test_an_unmatched_board_option_names_the_available_options_and_the_config_key(monkeypatch, board):
    stale = _fields(status_options=("Backlog", "Done"))
    fake = _install(
        monkeypatch,
        _json({"url": ISSUE_URL}),
        _json(PROJECT_VIEW),
        _json(stale),
        _json(PROJECT_VIEW),
        _json(stale),
    )

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().set_status("7", "in-progress")

    message = str(raised.value)
    assert "['Backlog', 'Done']" in message, f"the failure must list what the board does offer: {message}"
    assert "columns.*" in message and "docs/github-setup.md" in message, message
    assert len(fake.calls) == 5, "a board with nowhere to move the card must not be edited"


@pytest.mark.anyio
@pytest.mark.parametrize("already_on_the_board", [True, False])
async def test_an_issue_is_added_to_the_board_only_when_it_is_not_on_it(monkeypatch, board, already_on_the_board):
    results = [
        _json({"url": ISSUE_URL}),
        _json(PROJECT_VIEW),
        _json(_fields()),
        _json(_items(present=already_on_the_board)),
    ]
    if not already_on_the_board:
        results.append(_json({"id": "ITEM_9"}))
    results += [(0, "", ""), _json(_items(status="In progress"))]
    fake = _install(monkeypatch, *results)

    await adapter.GithubAdapter().set_status("7", "in-progress")

    adds = [call for call in fake.calls if call[1:3] == ["project", "item-add"]]
    assert bool(adds) is not already_on_the_board, (
        "an issue already on the board must not be added twice, and one absent from it must be added"
    )
    if adds:
        assert adds[0][1:] == ["project", "item-add", "3", "--owner", "@me", "--url", ISSUE_URL, "--format", "json"]
    edit = next(call for call in fake.calls if call[1:3] == ["project", "item-edit"])
    assert edit[edit.index("--id") + 1] == ("ITEM_1" if already_on_the_board else "ITEM_9"), edit


@pytest.mark.anyio
async def test_create_issue_sets_the_board_type_then_links_the_parent(monkeypatch, board):
    fake = _install(
        monkeypatch,
        (0, f"{ISSUE_URL}\n", ""),
        _json(PROJECT_VIEW),
        _json(_fields()),
        _json(_items(present=False)),
        _json({"id": "ITEM_9"}),
        (0, "", ""),
        _json(_items(issue_type="Task")),
        (0, f"{ISSUE_URL}\n", ""),
    )

    created = await adapter.GithubAdapter().create_issue("task", TITLE, "body", parent="5")

    assert fake.calls[0][1:] == ["issue", "create", *REPO_ARGS, "--title", TITLE, "--body", "body"], fake.calls[0]
    edit = next(call for call in fake.calls if call[1:3] == ["project", "item-edit"])
    assert edit[edit.index("--field-id") + 1] == "F_type", "type is a board single-select, not a native field"
    assert edit[edit.index("--single-select-option-id") + 1] == "o_task", edit
    assert fake.calls[-1][1:] == ["issue", "edit", ISSUE_URL, *REPO_ARGS, "--parent", "5"], (
        "the parent link must use gh's native --parent flag"
    )
    assert created == {"id": ISSUE_URL, "url": ISSUE_URL, "type": "task", "title": TITLE, "parent": "5"}, created


@pytest.mark.anyio
async def test_an_unknown_canonical_type_is_refused_before_anything_is_created(monkeypatch, board):
    fake = _install(monkeypatch)

    with pytest.raises(TrackerError, match="unknown canonical type"):
        await adapter.GithubAdapter().create_issue("saga", TITLE)

    assert fake.calls == [], "an unmappable type must not leave an issue created with no type on it"


@pytest.mark.anyio
async def test_get_issue_reports_board_values_relations_labels_and_comments(monkeypatch, board):
    fake = _install(monkeypatch, _json(_issue_view()), _json(_items(status="In review", issue_type="Epic")))

    issue = await adapter.GithubAdapter().get_issue("7")

    assert fake.calls[0][1:] == ["issue", "view", "7", *REPO_ARGS, "--json", adapter.ISSUE_FIELDS], fake.calls[0]
    assert (issue["status"], issue["type"]) == ("in-review", "epic"), "board values must arrive canonicalised"
    assert (issue["parent"], issue["children"]) == (PARENT_URL, [CHILD_URL]), issue
    assert issue["dependencies"] == [BLOCKER_URL], "blocked-by is the inbound dependency relation"
    assert issue["labels"] == ["shipyard", "bug"], issue["labels"]
    assert issue["comments"] == [
        {"id": "IC_1", "author": "octocat", "created": "2026-07-31T00:00:00Z", "body": "a note"}
    ], issue["comments"]
    assert issue["body"].startswith("## Context"), "this tracker is native Markdown; nothing may convert the body"


@pytest.mark.anyio
async def test_find_issues_filters_on_board_values_and_reports_is_last_honestly(monkeypatch, board):
    rows = [_list_row(), _list_row(number=8, url="https://github.com/octocat/repo/issues/8")]
    fake = _install(monkeypatch, _json(rows), _json(_items(status="In Progress")))

    found = await adapter.GithubAdapter().find_issues(status="in-progress", text="widget")

    assert fake.calls[0][1:] == [
        "issue", "list", *REPO_ARGS, "--state", "all", "--limit", "50", "--json", adapter.SUMMARY_FIELDS,
        "--search", "widget",
    ], fake.calls[0]
    assert [item["url"] for item in found["issues"]] == [ISSUE_URL], "an issue off the board has no status to match"
    assert (found["count"], found["is_last"], found["next_page_token"]) == (1, True, None), found

    _install(monkeypatch, _json(rows), _json(_items()))
    full_page = await adapter.GithubAdapter().find_issues(limit=2)
    assert full_page["is_last"] is False, "a full page must not claim to be the last one"


@pytest.mark.anyio
async def test_get_issue_and_find_issues_agree_on_every_shared_key(monkeypatch, board):
    """A caller that filters a search result then reads the issue must see one spelling, not two."""
    items = _items(status="In review", issue_type="Epic")
    _install(monkeypatch, _json(_issue_view()), _json(items))
    full = await adapter.GithubAdapter().get_issue("7")
    _install(monkeypatch, _json([_list_row()]), _json(items))
    summary = (await adapter.GithubAdapter().find_issues())["issues"][0]

    assert set(summary) < set(full), f"a search result must carry a subset of get_issue's keys: {set(summary)}"
    differing = {key: (summary[key], full[key]) for key in summary if summary[key] != full[key]}
    assert not differing, f"the two verbs canonicalise shared keys differently: {differing}"


@pytest.mark.anyio
async def test_assign_uses_the_native_add_assignee_flag_and_reports_the_resolved_account(monkeypatch, board):
    """`@me` is the request; the resolved login is the evidence, and it is what the caller gets.

    Both adapters report a resolved account here, so one shape covers both. Reading it back is also
    the only way to know the write landed on the identity the caller expected.
    """
    fake = _install(
        monkeypatch,
        (0, f"{ISSUE_URL}\n", ""),
        (0, json.dumps({"assignees": [{"login": "octocat"}]}), ""),
    )

    assigned = await adapter.GithubAdapter().assign("7")

    assert fake.calls[0][1:] == ["issue", "edit", "7", *REPO_ARGS, "--add-assignee", "@me"], fake.calls[0]
    assert assigned == {"id": ISSUE_URL, "assignee": "octocat"}, assigned


@pytest.mark.anyio
async def test_an_assignment_that_reads_back_empty_is_a_failure(monkeypatch, board):
    fake = _install(monkeypatch, (0, f"{ISSUE_URL}\n", ""), (0, json.dumps({"assignees": []}), ""))

    with pytest.raises(TrackerError, match="unconfirmed"):
        await adapter.GithubAdapter().assign("7")

    assert len(fake.calls) == 2, "the read-back must happen before the result is trusted"


@pytest.mark.anyio
async def test_assign_refuses_anyone_but_me_before_calling_gh(monkeypatch, board):
    fake = _install(monkeypatch)

    with pytest.raises(TrackerError, match="only self-assignment"):
        await adapter.GithubAdapter().assign("7", "octocat")

    assert fake.calls == [], "refusing must be a refusal, not a silent self-assignment"


@pytest.mark.anyio
async def test_link_parent_uses_the_native_parent_flag(monkeypatch, board):
    fake = _install(monkeypatch, (0, f"{ISSUE_URL}\n", ""))

    linked = await adapter.GithubAdapter().link_parent("7", PARENT_URL)

    assert fake.calls[0][1:] == ["issue", "edit", "7", *REPO_ARGS, "--parent", PARENT_URL], fake.calls[0]
    assert linked == {"id": ISSUE_URL, "parent": PARENT_URL}, linked


@pytest.mark.anyio
async def test_add_dependency_uses_add_blocked_by_and_verifies_the_relation(monkeypatch, board):
    fake = _install(
        monkeypatch,
        (0, f"{ISSUE_URL}\n", ""),
        _json({"blockedBy": {"nodes": [{"number": 4, "url": BLOCKER_URL}], "totalCount": 1}}),
    )

    added = await adapter.GithubAdapter().add_dependency("7", "4")

    assert fake.calls[0][1:] == ["issue", "edit", "7", *REPO_ARGS, "--add-blocked-by", "4"], fake.calls[0]
    assert fake.calls[1][1:] == ["issue", "view", ISSUE_URL, *REPO_ARGS, "--json", "blockedBy"], fake.calls[1]
    assert added == {"id": ISSUE_URL, "blocked_by": "4", "verified": True}, added


@pytest.mark.anyio
async def test_add_dependency_fails_when_the_relation_does_not_read_back(monkeypatch, board):
    """`verified: True` has to mean it: an empty re-read is a failure, not a warning."""
    _install(monkeypatch, (0, f"{ISSUE_URL}\n", ""), _json({"blockedBy": {"nodes": [], "totalCount": 0}}))

    with pytest.raises(TrackerError, match="does not read back"):
        await adapter.GithubAdapter().add_dependency("7", "4")


@pytest.mark.anyio
async def test_add_label_returns_every_label_the_issue_still_carries(monkeypatch, board):
    fake = _install(monkeypatch, (0, f"{ISSUE_URL}\n", ""), _json({"labels": [{"name": "shipyard"}, {"name": "bug"}]}))

    labelled = await adapter.GithubAdapter().add_label("7", "bug")

    assert fake.calls[0][1:] == ["issue", "edit", "7", *REPO_ARGS, "--add-label", "bug"], fake.calls[0]
    assert labelled == {"id": ISSUE_URL, "labels": ["shipyard", "bug"]}, (
        f"the labels already on the issue must survive the write: {labelled}"
    )


@pytest.mark.anyio
async def test_add_label_fails_when_the_label_is_not_there_afterwards(monkeypatch, board):
    _install(monkeypatch, (0, f"{ISSUE_URL}\n", ""), _json({"labels": [{"name": "shipyard"}]}))

    with pytest.raises(TrackerError, match="is not on"):
        await adapter.GithubAdapter().add_label("7", "bug")


@pytest.mark.anyio
async def test_update_issue_and_post_comment_report_what_the_write_returned(monkeypatch, board):
    fake = _install(monkeypatch, (0, f"{ISSUE_URL}\n", ""), (0, f"{COMMENT_URL}\n", ""))

    updated = await adapter.GithubAdapter().update_issue("7", "new body")
    posted = await adapter.GithubAdapter().post_comment("7", "hello")

    assert fake.calls[0][1:] == ["issue", "edit", "7", *REPO_ARGS, "--body", "new body"], fake.calls[0]
    assert fake.calls[1][1:] == ["issue", "comment", "7", *REPO_ARGS, "--body", "hello"], fake.calls[1]
    assert updated == {"id": ISSUE_URL, "updated": True, "url": ISSUE_URL}, updated
    assert posted == {"id": ISSUE_URL, "comment_id": "1", "url": COMMENT_URL}, posted


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "reason"),
    [((0, "", ""), "printing nothing"), ((1, "", "HTTP 404"), "exiting non-zero")],
)
async def test_a_write_gh_did_not_confirm_is_never_a_silent_success(monkeypatch, board, result, reason):
    _install(monkeypatch, result)

    with pytest.raises(TrackerError, match=r"gh issue edit"):
        await adapter.GithubAdapter().update_issue("7", f"new body ({reason})")


@pytest.mark.anyio
async def test_a_new_verbs_blocking_gh_work_also_runs_off_the_event_loop_thread(monkeypatch, board):
    fake = _install(monkeypatch, (0, f"{ISSUE_URL}\n", ""), _json({"labels": [{"name": "bug"}]}))

    await adapter.GithubAdapter().add_label("7", "bug")

    loop_thread = threading.get_ident()
    assert fake.threads, "no gh call was recorded, so nothing was proved about where it ran"
    assert all(ident != loop_thread for ident in fake.threads), (
        f"gh ran on the event loop thread ({loop_thread}); the new verbs are async in name only. "
        f"Observed: {fake.threads}"
    )


@pytest.mark.anyio
async def test_a_credential_in_a_new_verbs_command_output_never_reaches_its_error(monkeypatch, board):
    monkeypatch.setenv("SHIPYARD_TEST_TOKEN", "s3cr3t-value-not-for-logs")
    _install(monkeypatch, (1, "", "bad credentials: s3cr3t-value-not-for-logs"))

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().set_status("7", "done")

    assert "s3cr3t-value-not-for-logs" not in str(raised.value), "a held credential leaked into an error"


@pytest.mark.anyio
async def test_no_new_verb_writes_to_stdout(monkeypatch, board, capsys):
    _install(monkeypatch, _json(_issue_view()), _json(_items()))
    await adapter.GithubAdapter().get_issue("7")
    _install(monkeypatch, (1, "", "boom"))
    with pytest.raises(TrackerError):
        await adapter.GithubAdapter().add_label("7", "bug")

    assert capsys.readouterr().out == "", (
        "stdout carries JSON-RPC frames; one stray line desynchronises the client"
    )


class _Router(_FakeSubprocess):
    """Answers by argv instead of from a queue.

    The reference test must feed both implementations byte-identical payloads while their call
    sequences are what is under comparison, so a fixed queue cannot serve it.
    """

    def __init__(self, reply) -> None:
        super().__init__()
        self._reply = reply

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.kwargs.append(dict(kwargs))
        self.threads.append(threading.get_ident())
        return subprocess.CompletedProcess(argv, 0, json.dumps(self._reply(list(argv[1:]))), "")


def _reference_reply(fields: dict, items: dict):
    """One board, answered by argv: the single fixture both implementations are driven over."""

    def reply(args: list[str]) -> object:
        head = args[:2]
        if head == ["project", "view"]:
            return PROJECT_VIEW
        if head == ["project", "field-list"]:
            return fields
        if head == ["project", "item-list"]:
            return items
        if head == ["project", "item-edit"]:
            return {}
        raise AssertionError(f"the shared fixture was asked for an unexpected gh call: {args}")

    return reply


@pytest.fixture
def cli_helper(monkeypatch, tmp_path, board):
    """The shipped CLI helper, stubbed the way its own `_self_test` stubs it: module globals swapped.

    Loaded by path because `skills/` is not an importable package, and its disk cache is redirected
    into tmp so the CLI deployment's real cache is neither read nor written by a test.
    """
    path = Path(__file__).resolve().parents[3] / "skills" / "tracker" / "github" / "gh_project.py"
    spec = importlib.util.spec_from_file_location("gh_project_reference", path)
    assert spec and spec.loader, f"could not load the CLI helper from {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(module, "config_get", lambda key: COLUMNS.get(key, ""))
    return module


def _drive_both(monkeypatch, cli_helper, fields: dict, items: dict) -> tuple[_Router, list[list[str]]]:
    """Point both implementations at one fixture and record the `gh` calls each one makes."""
    reply = _reference_reply(fields, items)
    router = _Router(reply)
    monkeypatch.setattr(adapter, "subprocess", router)
    theirs: list[list[str]] = []

    def their_gh_json(args: list[str]) -> object:
        theirs.append(list(args))
        return reply(list(args))

    monkeypatch.setattr(cli_helper, "_gh_json", their_gh_json)
    return router, theirs


def _assert_same_calls(mine: list[list[str]], theirs: list[list[str]]) -> None:
    """The two must issue the same `gh` calls, bar the adapter's documented extra read-back."""
    stripped = [call[1:] for call in mine]
    assert stripped[: len(theirs)] == theirs, (
        f"the ported write diverges from the CLI helper's gh calls.\nported: {stripped}\nhelper: {theirs}"
    )
    assert stripped[len(theirs) :] == [call for call in theirs if call[:2] == ["project", "item-list"]], (
        "the adapter's only extra call must be one more item-list: it re-reads the field it just wrote "
        "(CONTRIBUTING's write discipline), which the CLI helper does not do"
    )


def test_the_ported_board_resolution_matches_the_cli_helper(monkeypatch, cli_helper):
    """Same fixture through both resolvers: ids, single-select filtering and canonicalisation."""
    router, theirs_calls = _drive_both(monkeypatch, cli_helper, _fields(), _items())

    mine = adapter.GithubAdapter()._resolve("@me", "3", refresh=True)
    theirs = cli_helper._resolve("@me", "3", refresh=True)

    assert mine == theirs, f"the ported resolver disagrees with the CLI helper.\nported: {mine}\nhelper: {theirs}"
    assert [call[1:] for call in router.calls] == theirs_calls, "the two resolvers issue different gh calls"
    assert set(mine["fields"]) == {"Status", "Type"}, f"both must keep only the single-selects: {set(mine['fields'])}"
    for option in ("In progress", "IN PROGRESS", " in progress ", "On hold"):
        assert adapter._option_id(mine, "Status", option) == cli_helper._option_id(theirs, "Status", option), option
    for raw in (_items()["items"][0], {"type": None, "status": "Icebox", "content": {"number": 1, "url": ISSUE_URL}}):
        assert adapter._normalize_item(raw) == cli_helper._normalize_item(raw), (
            f"the ported item mapping disagrees with the CLI helper's for {raw}"
        )


def test_the_ported_field_write_issues_the_same_gh_calls_as_the_cli_helper(monkeypatch, cli_helper):
    """Status and type, written by both implementations over one fixture, compared call for call."""
    items = _items(status="In progress", issue_type="Task")
    router, theirs_calls = _drive_both(monkeypatch, cli_helper, _fields(), items)
    mine = adapter.GithubAdapter()

    cli_helper.set_status(PROJECT, ISSUE_URL, "in-progress")
    mine._set_field(ISSUE_URL, adapter.STATUS_FIELD, adapter.native_status("in-progress"))
    _assert_same_calls(router.calls, theirs_calls)

    mine_seen, theirs_seen = len(router.calls), len(theirs_calls)
    cli_helper.set_type(PROJECT, ISSUE_URL, "task")
    mine._set_field(ISSUE_URL, adapter.TYPE_FIELD, adapter.native_type("task"))
    _assert_same_calls(router.calls[mine_seen:], theirs_calls[theirs_seen:])
    assert not any(call[:2] == ["project", "view"] for call in theirs_calls[theirs_seen:]), (
        "both caches must spare the second write a re-resolve, or the sequences are not comparable"
    )


def test_the_ported_unmatched_option_failure_matches_the_cli_helper(monkeypatch, cli_helper):
    router, theirs_calls = _drive_both(monkeypatch, cli_helper, _fields(status_options=("Backlog", "Done")), _items())

    with pytest.raises(SystemExit) as theirs:
        cli_helper.set_field(PROJECT, ISSUE_URL, "Status", "On hold")
    with pytest.raises(TrackerError) as mine:
        adapter.GithubAdapter()._set_field(ISSUE_URL, adapter.STATUS_FIELD, "On hold")

    assert str(mine.value) == str(theirs.value.code), (
        f"the two failures must say the same thing.\nported: {mine.value}\nhelper: {theirs.value.code}"
    )
    assert [call[1:] for call in router.calls] == theirs_calls, "both must re-resolve once before failing"
    assert isinstance(theirs.value, SystemExit) and not isinstance(mine.value, SystemExit), (
        "deliberate divergence: the CLI helper exits the process, the adapter raises TrackerError, because "
        "this process has other tool calls to serve after a bad one"
    )
