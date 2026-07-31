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
from typing import Any

import pytest

from sy_tools.tracker import TIMEOUT_SECONDS, TrackerError
from sy_tools.tracker.github import adapter

GIST_URL = "https://gist.github.com/octocat/abc123"
COMMENT_URL = "https://github.com/octocat/repo/issues/7#issuecomment-1"
REPO = "octocat/repo"
HOST = "github.com"
PROJECT = "@me/3"
CANARY = "canary-value-not-for-logs"
"""A planted value that must never appear in a message, kept out of this process's environment.

`_safe` redacts what this process holds, so a canary in the environment proves nothing about a value
that arrives from configuration: the userinfo strip could be deleted and the assertion would still pass.
"""
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
REPO_VIEW_ARGS = ["--json", "nameWithOwner", "-q", ".nameWithOwner"]


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


class _BoardReads(_FakeSubprocess):
    """A fake that answers a board-filtered search: the board, then one `issue view` per candidate.

    `gh issue list` is refused outright, because that call is what used to bound the candidate set and
    both of its bounds dropped board items invisibly: its own `--limit` hid anything older than the
    newest rows, and behind `--search` the Search API caps at 1,000 rows with nothing in the `--json`
    output to say so.

    `resolved_repo` is what `gh repo view` answers: the search's repository is resolved by `gh` for a
    configured value as well as for an unset one, because only `gh` knows every spelling its own `--repo`
    accepts. A slug is a reference `gh` resolved, `""` is one it refused — an unresolvable working
    directory, or a value that is not a repository reference at all.

    The board's fields are answered too, because the filter is now checked against the board's real
    `Status` and `Type` options before any card is matched by one; `fields` is how a test drifts them.
    """

    def __init__(
        self, items: dict, views: dict[str, dict], *, resolved_repo: str = REPO, fields: dict | None = None
    ) -> None:
        super().__init__()
        self._items = items
        self._views = views
        self._resolved_repo = resolved_repo
        self._fields = fields if fields is not None else _board_fields()

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.kwargs.append(dict(kwargs))
        self.threads.append(threading.get_ident())
        args = argv[1:]
        if args[:2] == ["repo", "view"]:
            if not self._resolved_repo:
                return subprocess.CompletedProcess(argv, 1, "", "argument error: invalid path")
            return subprocess.CompletedProcess(argv, 0, f"{self._resolved_repo}\n", "")
        if args[:2] == ["project", "item-list"]:
            payload: object = self._items
        elif args[:2] == ["project", "view"]:
            payload = PROJECT_VIEW
        elif args[:2] == ["project", "field-list"]:
            payload = self._fields
        elif args[:2] == ["issue", "view"]:
            payload = self._views[args[2]]
        else:
            raise AssertionError(f"a board filter may read only the board, its fields and its items: {argv}")
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def _install(monkeypatch: pytest.MonkeyPatch, *results: tuple[int, str, str]) -> _FakeSubprocess:
    fake = _FakeSubprocess(*results)
    monkeypatch.setattr(adapter, "subprocess", fake)
    return fake


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Patch the config every board-touching verb reads, overriding named keys.

    Keyword names are the config paths with dots replaced by nothing usable, so callers pass them as
    `**{"tracker_config.repo": ...}`; the overrides exist because the repo value's spelling — and its
    absence — is itself under test.
    """
    values: dict[str, object] = {
        **COLUMNS, "tracker_config.project": PROJECT, "tracker_config.repo": REPO, **overrides
    }
    original = adapter.config.get

    def fake_get(path: str, **kwargs: object) -> object:
        return values[path] if path in values else original(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter.config, "get", fake_get)


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> None:
    """The config every board-touching verb reads: this repo's column names, the board and the repo.

    One fixture rather than per-test patching, because the `gh` call sequence a verb makes depends
    on these values and a test that quietly disagrees with another about them proves nothing.
    """
    _configure(monkeypatch)


def _json(payload: object) -> tuple[int, str, str]:
    """One queued `gh` result whose stdout is `payload` as JSON."""
    return (0, json.dumps(payload), "")


def _repo_view(slug: str = REPO) -> tuple[int, str, str]:
    """The queued `gh repo view` answer a board-filtered search resolves its repository through."""
    return (0, f"{slug}\n", "")


def _fields(
    *,
    status_options: tuple[str, ...] = ("Backlog", "In progress", "Done"),
    type_options: tuple[str, ...] = ("Epic", "Task"),
) -> dict:
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
                "options": [{"id": f"o_{name.lower()}", "name": name} for name in type_options],
            },
            {"id": "F_title", "name": "Title", "type": "ProjectV2Field"},
        ]
    }


def _board_fields(**overrides: tuple[str, ...]) -> dict:
    """The `field-list` a correctly configured board answers with: a column per configured name.

    A board-filtered search now checks the column and type name it is about to match cards by against the
    board's real options, so every such search reads the fields as a write does. `overrides` is how a test
    drifts one of them away from the config.
    """
    return _fields(**{"status_options": tuple(COLUMNS.values()), **overrides})


def _resolution() -> tuple[tuple[int, str, str], ...]:
    """The two queued `gh` results a board resolution costs: the project, then its fields."""
    return (_json(PROJECT_VIEW), _json(_board_fields()))


def _content(number: int, url: str, kind: str | None) -> dict:
    """A card's `content`, carrying the `Issue`/`PullRequest`/`DraftIssue` type `gh` reports.

    `kind=None` omits the key, which is the malformed case: a card `gh` reports with a URL but no
    content type cannot be classified, and must not be guessed at in either direction.
    """
    content: dict[str, object] = {"number": number, "title": TITLE, "url": url}
    return content if kind is None else {**content, "type": kind}


def _items(
    *, status: str = "Backlog", issue_type: str = "Task", present: bool = True, kind: str | None = "Issue"
) -> dict:
    """An `item-list` payload holding the issue under test, or an empty board."""
    if not present:
        return {"items": [], "totalCount": 0}
    item = {"id": "ITEM_1", "status": status, "type": issue_type, "content": _content(7, ISSUE_URL, kind)}
    return {"items": [item], "totalCount": 1}


def _board_items(
    urls: list[str], *, status: str = "Ready", issue_type: str = "Task", kinds: list[str | None] | None = None
) -> dict:
    """A board holding one item per URL, all in one column: what a queue query is really asked about."""
    return {
        "items": [
            {
                "id": f"ITEM_{number}",
                "status": status,
                "type": issue_type,
                "content": _content(number, url, kind),
            }
            for number, (url, kind) in enumerate(
                zip(urls, kinds if kinds is not None else ["Issue"] * len(urls), strict=True), start=1
            )
        ],
        "totalCount": len(urls),
    }


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

    evidence = await adapter.GithubAdapter().attach_artifact("1", path)

    create, verify, comment = fake.calls
    assert create[:3] == ["gh", "gist", "create"], create
    assert str(path) in create, "the gist must be created from the artifact file itself"
    assert verify[:3] == ["gh", "api", "gists/abc123"], "privacy must be re-read, not assumed"
    assert comment[:4] == ["gh", "issue", "comment", "1"], comment
    body = comment[comment.index("--body") + 1]
    assert GIST_URL in body, "the comment must carry the gist URL, or the artifact is undiscoverable"
    assert evidence["gist_url"] == GIST_URL, "evidence must report the URL the transport produced"
    assert evidence["comment_url"] == COMMENT_URL, "evidence must report the comment the write returned"


@pytest.mark.anyio
async def test_the_blocking_gh_work_runs_off_the_event_loop_thread(tmp_path, monkeypatch):
    """The offload must be real: `gh` blocks, so it may not block the loop that serves other calls."""
    fake = _install(monkeypatch, *_happy_path())

    await adapter.GithubAdapter().attach_artifact("1", _artifact(tmp_path))

    loop_thread = threading.get_ident()
    assert fake.threads, "no gh call was recorded, so nothing was proved about where it ran"
    assert all(ident != loop_thread for ident in fake.threads), (
        f"gh ran on the event loop thread ({loop_thread}); the verb is async in name only and a "
        f"slow attachment would block every other tool call. Observed: {fake.threads}"
    )


@pytest.mark.anyio
async def test_the_gist_is_never_created_public(tmp_path, monkeypatch):
    fake = _install(monkeypatch, *_happy_path())

    await adapter.GithubAdapter().attach_artifact("1", _artifact(tmp_path))

    assert "--public" not in fake.calls[0], (
        "gh gists are secret by default; passing --public would publish the transcript irrevocably"
    )


@pytest.mark.anyio
async def test_a_gist_that_reads_back_public_is_refused_before_any_comment(tmp_path, monkeypatch):
    fake = _install(monkeypatch, *_happy_path(secret=False))

    with pytest.raises(TrackerError, match="public"):
        await adapter.GithubAdapter().attach_artifact("1", _artifact(tmp_path))

    assert len(fake.calls) == 2, "a public gist must not be linked from the work item"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "reason"),
    [((0, "", ""), "empty output"), ((1, "", "HTTP 422"), "non-zero exit")],
)
async def test_a_failed_gist_call_is_never_a_silent_success(tmp_path, monkeypatch, result, reason):
    fake = _install(monkeypatch, result)

    with pytest.raises(TrackerError):
        await adapter.GithubAdapter().attach_artifact("1", _artifact(tmp_path))

    assert len(fake.calls) == 1, f"{reason} must stop the attachment, not fall through to a comment"


@pytest.mark.anyio
async def test_a_credential_in_command_output_never_reaches_the_error_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIPYARD_TEST_TOKEN", "s3cr3t-value-not-for-logs")
    _install(monkeypatch, (1, "", "bad credentials: s3cr3t-value-not-for-logs"))

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().attach_artifact("1", _artifact(tmp_path))

    assert "s3cr3t-value-not-for-logs" not in str(raised.value), "a held credential leaked into an error"


@pytest.mark.anyio
async def test_a_credential_in_the_command_arguments_never_reaches_the_error_message(monkeypatch, board):
    """The argv is interpolated into failures too, and `--body` carries whatever the caller wrote."""
    monkeypatch.setenv("SHIPYARD_TEST_TOKEN", "s3cr3t-value-not-for-logs")
    _install(monkeypatch, (1, "", "HTTP 422"))

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().update_issue("7", "token: s3cr3t-value-not-for-logs")

    assert "s3cr3t-value-not-for-logs" not in str(raised.value), "a credential in the argv leaked into an error"


@pytest.mark.anyio
async def test_extra_redaction_words_apply_to_error_messages(tmp_path, monkeypatch):
    """`redaction.extra_words` must redact command output here exactly as on the attach path."""
    monkeypatch.setenv("NM_BEARER", "org-secret-value-9f8e7d6c")
    monkeypatch.setattr(adapter.config, "extra_secret_words", lambda: frozenset({"BEARER"}))
    _install(monkeypatch, (1, "", "bad credentials: org-secret-value-9f8e7d6c"))

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().attach_artifact("1", _artifact(tmp_path))

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


def _auth_status(scopes_line: str = "") -> str:
    """`gh auth status` output, with or without the `Token scopes:` line only some tokens produce."""
    return f"github.com\n  ✓ Logged in to github.com account octocat (keyring)\n{scopes_line}"


@pytest.mark.anyio
async def test_preflight_fails_a_token_whose_scopes_lack_project(monkeypatch):
    """A `repo`-only token authenticates and then dies on the first `set-status`.

    That is the half-finished workflow preflight exists to prevent, so the scope it names as required
    is the scope it checks, whenever the scopes are there to check.
    """
    fake = _install(
        monkeypatch,
        (0, "gh version 2.94.0 (2025-01-01)\n", ""),
        (0, _auth_status("  - Token scopes: 'gist', 'repo'\n"), ""),
    )

    with pytest.raises(TrackerError, match="'project' scope") as raised:
        await adapter.GithubAdapter().preflight()

    assert "gh auth" in str(raised.value), f"the failure must say how to fix it: {raised.value}"
    assert len(fake.calls) == 2, "scopes that answer the question must not cost a board read"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("board_read", "reason"),
    [((0, json.dumps(_items()), ""), "readable"), ((1, "", "HTTP 403 Resource not accessible"), "refused")],
)
async def test_preflight_confirms_a_scopeless_token_by_reading_the_board(monkeypatch, board, board_read, reason):
    """`gh auth status` prints no scopes line for a fine-grained PAT or an App token.

    Those are fully able to write Projects v2, so an absent line must not fail a working setup — but a
    green preflight still has to mean something was checked, so the board is read instead and its
    answer decides. `scopes: None` says which check ran; `[]` would read as "a token with no scopes".
    """
    fake = _install(monkeypatch, (0, "gh version 2.96.0 (2025-01-01)\n", ""), (0, _auth_status(), ""), board_read)

    if reason == "refused":
        with pytest.raises(TrackerError, match="unconfirmed") as raised:
            await adapter.GithubAdapter().preflight()
        assert "gh auth" in str(raised.value), f"the failure must say how to fix it: {raised.value}"
    else:
        facts = await adapter.GithubAdapter().preflight()
        assert facts["authenticated"] is True and facts["account"] == "octocat", facts
        assert facts["scopes"] is None, (
            f"a fabricated or empty scope list hides which check confirmed capability: {facts}"
        )

    assert fake.calls[-1][1:3] == ["project", "item-list"], (
        f"an absent scopes line must be answered by a positive board read: {fake.calls}"
    )


@pytest.mark.anyio
async def test_a_hung_gh_is_bounded_and_becomes_an_actionable_failure(tmp_path, monkeypatch):
    """A `gh` that never returns must not wedge a server that has other calls to serve."""
    fake = _install(monkeypatch, *_happy_path())

    await adapter.GithubAdapter().attach_artifact("1", _artifact(tmp_path))

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
    """The board spells the column `In progress`; this repo's config spells it `In Progress`.

    The argv is asserted whole because both board reads must ask for `--limit 10000`: `field-list`
    defaults to 30 fields, and a board wide enough to push `Status` past that would resolve without it.
    """
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
        ["project", "field-list", *OWNER_ARGS, "--limit", "10000"],
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
async def test_an_issue_id_gh_would_read_as_a_flag_is_refused_before_any_call(monkeypatch, board, tmp_path):
    """An id crosses the tool boundary opaque and lands in `gh`'s argv, where a leading dash is a flag.

    With `tracker_config.repo` unset there is no `--repo` ahead of it, so `-Rowner/repo` would retarget
    the write to a repository the caller named.
    """
    fake = _install(monkeypatch)

    with pytest.raises(TrackerError, match="not an issue reference"):
        await adapter.GithubAdapter().update_issue("-Rattacker/repo", "new body")
    with pytest.raises(TrackerError, match="not an issue reference"):
        await adapter.GithubAdapter().get_issue("--json")
    with pytest.raises(TrackerError, match="not an issue reference"):
        await adapter.GithubAdapter().post_comment("-Rattacker/repo", "hello")
    with pytest.raises(TrackerError, match="not an issue reference"):
        await adapter.GithubAdapter().attach_artifact("-Rattacker/repo", tmp_path / "missing.txt")

    assert fake.calls == [], "an id gh would parse as a flag must never reach its argv"


@pytest.mark.anyio
async def test_a_relation_of_the_wrong_shape_is_never_reported_as_no_dependencies(monkeypatch, board):
    """`dependencies: []` is what a caller reads as "not blocked", so an unparseable relation must raise.

    A genuinely absent relation still means no dependencies: that distinction is the whole point.
    """
    malformed = {**_issue_view(), "blockedBy": {"nodes": {"4": {"url": BLOCKER_URL}}}}
    _install(monkeypatch, _json(malformed), _json(_items()))

    with pytest.raises(TrackerError, match="not a list of issues"):
        await adapter.GithubAdapter().get_issue("7")

    absent = {key: value for key, value in _issue_view().items() if key != "blockedBy"}
    _install(monkeypatch, _json(absent), _json(_items()))
    assert (await adapter.GithubAdapter().get_issue("7"))["dependencies"] == [], (
        "an issue gh reports no blockedBy for really has no dependencies"
    )


@pytest.mark.anyio
async def test_find_issues_filters_on_board_values_and_reports_is_last_honestly(monkeypatch, board):
    fake = _install(
        monkeypatch,
        _repo_view(),
        _json(_items(status="In Progress")),
        *_resolution(),
        _json({**_list_row(), "body": "a widget"}),
    )

    found = await adapter.GithubAdapter().find_issues(status="in-progress", text="widget")

    assert [call[1:3] for call in fake.calls] == [
        ["repo", "view"], ["project", "item-list"], ["project", "view"], ["project", "field-list"], ["issue", "view"]
    ], f"the repo, the board, the board's fields, then one read per surviving candidate: {fake.calls}"
    assert fake.calls[0][1:] == ["repo", "view", REPO, *REPO_VIEW_ARGS], fake.calls[0]
    assert fake.calls[1][1:] == ["project", "item-list", *OWNER_ARGS, "--limit", adapter.ITEM_LIMIT], fake.calls[1]
    assert fake.calls[-1][1:] == [
        "issue", "view", ISSUE_URL, *REPO_ARGS, "--json", f"{adapter.SUMMARY_FIELDS},body"
    ], "the labels, parent and body a card does not carry are read per surviving candidate"
    assert not any(call[1:3] == ["issue", "list"] for call in fake.calls), (
        "a board filter must not go through issue list: --search caps at 1,000 rows invisibly"
    )
    assert [item["url"] for item in found["issues"]] == [ISSUE_URL], found
    assert (found["count"], found["is_last"], found["next_page_token"]) == (1, True, None), found

    rows = [_list_row(), _list_row(number=8, url="https://github.com/octocat/repo/issues/8")]
    unfiltered = _install(monkeypatch, _json(rows), _json(_items()))
    full_page = await adapter.GithubAdapter().find_issues(limit=2)
    assert full_page["is_last"] is False, "a full page must not claim to be the last one"
    assert unfiltered.calls[0][unfiltered.calls[0].index("--limit") + 1] == "2", (
        "with no board-value filter the list call is the page, so it may still page at `limit`"
    )


@pytest.mark.anyio
async def test_a_board_issue_outside_any_issue_list_window_is_still_found(monkeypatch, board):
    """Found by review, twice: the candidate set must be bounded by the board, not by a repo read.

    `--limit limit` on `gh issue list` hid the wanted issue behind newer ones and reported `count: 0`
    from a board that has work on it; widening that read to the repository put the candidate set behind
    the Search API's silent 1,000-row cap instead, so items were dropped with `is_last: true`. Neither
    bound can bite once the board itself is the candidate set — which is why the fake refuses to answer
    `issue list` at all.
    """
    fake = _BoardReads(_items(status="Ready"), {ISSUE_URL: _list_row()})
    monkeypatch.setattr(adapter, "subprocess", fake)

    found = await adapter.GithubAdapter().find_issues(status="ready", limit=2)

    assert [item["url"] for item in found["issues"]] == [ISSUE_URL], f"the ready issue went missing: {found}"
    assert (found["count"], found["is_last"]) == (1, True), found
    assert [call[1:3] for call in fake.calls] == [
        ["repo", "view"], ["project", "item-list"], ["project", "view"], ["project", "field-list"], ["issue", "view"]
    ], f"one repo resolution, one board read, one field check, then one read per candidate: {fake.calls}"


@pytest.mark.anyio
async def test_a_filtered_result_longer_than_the_limit_is_paged_not_dropped(monkeypatch, board):
    """`limit` bounds the page and `is_last` reports the rest honestly, at one read past the page.

    The third candidate is deliberately unanswerable: knowing a further match exists is all `is_last`
    needs, so reading the remainder of the board would only cost a caller time.
    """
    other = "https://github.com/octocat/repo/issues/8"
    third = "https://github.com/octocat/repo/issues/9"
    fake = _install(
        monkeypatch,
        _repo_view(),
        _json(_board_items([ISSUE_URL, other, third])),
        *_resolution(),
        _json(_list_row()),
        _json(_list_row(number=8, url=other)),
    )

    found = await adapter.GithubAdapter().find_issues(status="ready", limit=1)

    assert (found["count"], found["is_last"]) == (1, False), f"a truncated page must say so: {found}"
    reads = [call for call in fake.calls if call[1:3] == ["issue", "view"]]
    assert len(reads) == 2, f"reads must stop one match past the page: {fake.calls}"


@pytest.mark.anyio
async def test_text_with_a_board_filter_matches_the_title_or_body_of_board_candidates(monkeypatch, board):
    """Client-side by design: server-side ranking is not reproduced, completeness for the board is."""
    other = "https://github.com/octocat/repo/issues/8"
    views = {
        ISSUE_URL: {**_list_row(), "body": "the WIDGET is broken"},
        other: {**_list_row(number=8, url=other), "title": "Ship the widget", "body": "unrelated"},
    }
    fake = _BoardReads(_board_items([ISSUE_URL, other]), views)
    monkeypatch.setattr(adapter, "subprocess", fake)

    body_match = await adapter.GithubAdapter().find_issues(status="ready", text="widget")

    assert [item["url"] for item in body_match["issues"]] == [ISSUE_URL, other], (
        f"a case-insensitive substring of title or body must match: {body_match}"
    )
    assert not any("--search" in call for call in fake.calls), "text must be applied over board candidates"

    only_titles = _BoardReads(_board_items([ISSUE_URL, other]), views)
    monkeypatch.setattr(adapter, "subprocess", only_titles)
    titled = await adapter.GithubAdapter().find_issues(status="ready", text="ship the")

    assert [item["url"] for item in titled["issues"]] == [other], f"the title must be searched too: {titled}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "configured",
    [
        REPO,
        "OctoCat/Repo",
        f"github.com/{REPO}",
        f"https://github.com/{REPO}",
        f"https://github.com/{REPO}/",
        f"https://github.com/{REPO}.git",
        f"git@github.com:{REPO}.git",
    ],
)
async def test_every_repo_spelling_gh_accepts_finds_the_same_board_cards(monkeypatch, configured):
    """Found by review, twice: a repo filter this file parses by hand is always a spelling short.

    Every spelling here is one `gh --repo` takes and one `_repo_args()` already hands it for every other
    verb, so each names the repository the board cards live in. Comparing raw strings emptied the page for
    most of them; a hand-written normaliser then still missed the `.git` suffix and the scp-like SSH form —
    each miss a correctly configured repo reported as an empty queue, the one wrong answer a caller cannot
    tell from the truth. So the value is handed to `gh repo view` verbatim and `gh`'s own answer is what
    the cards are matched against: no spelling can be missed that `--repo` would have accepted.
    """
    _configure(monkeypatch, **{"tracker_config.repo": configured})
    fake = _BoardReads(_board_items([ISSUE_URL]), {ISSUE_URL: _list_row()})
    monkeypatch.setattr(adapter, "subprocess", fake)

    found = await adapter.GithubAdapter().find_issues(status="ready")

    assert fake.calls[0][1:] == ["repo", "view", configured, *REPO_VIEW_ARGS], (
        f"the configured value must reach gh unparsed: {fake.calls[0]}"
    )
    assert [item["url"] for item in found["issues"]] == [ISSUE_URL], (
        f"{configured!r} names the repo the card is in, but the card went missing: {found}"
    )
    assert (found["count"], found["is_last"]) == (1, True), found


@pytest.mark.anyio
@pytest.mark.parametrize("configured", [ISSUE_URL, "repo"])
async def test_a_repo_value_gh_refuses_fails_before_the_board_is_read(monkeypatch, configured):
    """A value `gh` will not resolve is a misconfiguration, not something to normalise into a match.

    An issue URL is the case a hand-written parser got wrong in the direction that hides: taking its last
    two path segments made `tracker_config.repo` read as the repository `issues/7`, which matches no card,
    so a misconfigured repo answered `count: 0, is_last: true`. `gh repo view` refuses an issue path
    outright, and refusing is the whole point — the caller is told the query has no repository rather than
    told the queue is empty.
    """
    _configure(monkeypatch, **{"tracker_config.repo": configured})
    fake = _BoardReads(_board_items([ISSUE_URL]), {ISSUE_URL: _list_row()}, resolved_repo="")
    monkeypatch.setattr(adapter, "subprocess", fake)

    with pytest.raises(TrackerError, match="could not resolve it to a repository") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    assert "tracker_config.repo" in str(raised.value), f"the failure must say how to fix it: {raised.value}"
    assert [call[1:3] for call in fake.calls] == [["repo", "view"]], (
        f"an unusable repo value must fail before the board is read: {fake.calls}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("configured", ["-Rowner/repo", "--json"])
async def test_a_repo_value_gh_would_read_as_a_flag_is_refused_before_gh_is_called(monkeypatch, configured):
    """The configured value is now a bare positional in `gh repo view`'s argv, so it can be a flag.

    Exactly the hazard `_checked_ref` guards on the issue-reference side, one path further out: a value
    starting with `-` is parsed by `gh` as an option rather than as the repository to resolve.
    """
    _configure(monkeypatch, **{"tracker_config.repo": configured})
    fake = _install(monkeypatch)

    with pytest.raises(TrackerError, match="read as a flag") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    assert "tracker_config.repo" in str(raised.value), f"the failure must say what to fix: {raised.value}"
    assert fake.calls == [], f"a value gh would read as a flag must never reach its argv: {fake.calls}"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (REPO, REPO),
        ("@me", "@me"),
        ("@me/abc", "@me/abc"),
        ("someone@example.com: see the board", "someone@example.com: see the board"),
        (f"https://{HOST}/{REPO}.git", f"https://{HOST}/{REPO}.git"),
        (f"git@{HOST}:{REPO}.git", f"{HOST}:{REPO}.git"),
        (f"https://x-access-token:{CANARY}@{HOST}/{REPO}", f"https://{HOST}/{REPO}"),
        (f"https://user:pa/ss@{CANARY}@{HOST}/{REPO}", f"https://{HOST}/{REPO}"),
        (f"https://x-access-token:{CANARY}@{HOST}:8080", f"https://{HOST}:8080"),
        (f"//x-access-token:{CANARY}@{HOST}/{REPO}", f"//{HOST}/{REPO}"),
        (f"x-access-token:{CANARY}@{HOST}:{REPO}.git", f"{HOST}:{REPO}.git"),
        ("a note about a@b.example and x: y", "a note about a@b.example and x: y"),
        (f"x-access-token:{CANARY}@{HOST}/{REPO}", f"{HOST}/{REPO}"),
        (
            f"see https://{HOST}/{REPO} and https://x-access-token:{CANARY}@{HOST}/octocat/other",
            f"see https://{HOST}/{REPO} and https://{HOST}/octocat/other",
        ),
        (
            f"GraphQL: no repo\nhttps://x:{CANARY}@{HOST}/{REPO}\nhttps://{HOST}/{REPO}",
            f"GraphQL: no repo\nhttps://{HOST}/{REPO}\nhttps://{HOST}/{REPO}",
        ),
        (f"https://x-access-token:tok%40{CANARY}@ghe.example.com:8443/{REPO}", f"https://ghe.example.com:8443/{REPO}"),
        (
            f"Could not resolve to a Repository with the name 'x-access-token:{CANARY}@{HOST}:{REPO}.git'. (repo)",
            f"Could not resolve to a Repository with the name {HOST}:{REPO}.git'. (repo)",
        ),
        (
            f"Could not resolve to a Repository with the name 'x-access-token:{CANARY}@{HOST}/{REPO}'. (repo)",
            f"Could not resolve to a Repository with the name {HOST}/{REPO}'. (repo)",
        ),
        (f"ping x-access-token:{CANARY}@{HOST}/{REPO} done", f"ping {HOST}/{REPO} done"),
    ],
)
def test_stripping_credentials_survives_a_userinfo_that_holds_a_slash_or_a_second_at(value, expected):
    """The round-5 regex assumed the userinfo held neither `/` nor `@`, and stopped at either.

    So a hostile or merely hand-mangled value walked the strip out of its own bounds and the remainder
    printed. The authority component is bounded before the credential is looked for, and a value whose
    authority is then not a host at all is over-stripped rather than trusted — better a value the
    operator has to squint at than a password with a slash in it in a log.

    Two shapes were still carried through whole afterwards, and both are reachable: a value or an output
    line holding more than one `//` authority had only its first stripped, so a clean URL followed by a
    credentialed one printed the second in cleartext; and a schemeless `userinfo@host/path` was not the
    scp form the second branch matched, which wants a colon after the host.

    Both schemeless spellings were then matched against the *whole* value, which is not the shape they
    arrive in: `gh` quotes the reference it refused inside a sentence, and a `--body` wraps prose around
    whatever the caller wrote, so neither ever fullmatched and both printed the token. Stripping is now per
    whitespace-separated token, and the last three rows are those two prose shapes plus a body — the
    opening quote goes with the userinfo, which is the cosmetic half of a trade for removing the secret.

    The controls: a bare `OWNER/REPO`, `@me` and `@me/abc` (a legitimate self-reference, not an empty
    userinfo to strip), an address followed by a colon in prose, and the clean URL beside the credentialed
    one, all left exactly as they were.
    """
    assert adapter._stripped_of_credentials(value) == expected, "a credential-bearing shape was mis-stripped"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "configured",
    [
        f"https://x-access-token:{CANARY}@{HOST}/{REPO}.git",
        f"https://user:pa/ss@{CANARY}@{HOST}/{REPO}",
        f"x-access-token:{CANARY}@{HOST}:{REPO}.git",
    ],
)
async def test_a_credential_in_the_configured_repo_never_reaches_the_error_message(monkeypatch, configured):
    """`gh --repo` accepts an https remote, and an https remote can carry userinfo.

    So a misconfigured `tracker_config.repo` can hold a token, and the failure that names the bad value
    printed it verbatim. What is left is still what the operator has to fix.

    The canary is deliberately absent from this process's environment: planting it there proved only that
    `_safe` redacts what this process holds, which is not what a token in a config value is, and the
    URL-userinfo strip this covers could have been removed with the test still passing.
    """
    _configure(monkeypatch, **{"tracker_config.repo": configured})
    monkeypatch.setattr(adapter, "subprocess", _BoardReads(_board_items([ISSUE_URL]), {}, resolved_repo=""))

    with pytest.raises(TrackerError, match="could not resolve it to a repository") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    message = str(raised.value)
    assert CANARY not in message, "a credential in the config leaked into an error"
    assert "tracker_config.repo" in message, f"the failure must still say what to fix: {message}"
    assert adapter._stripped_of_credentials(configured) in message, (
        f"the failure must still show the sanitised value the operator has to go and fix: {message}"
    )


@pytest.mark.anyio
async def test_a_credential_in_the_configured_repo_never_reaches_another_verbs_failure(monkeypatch):
    """Found by review: the strip covered the two messages about the value, not the argv it rides in.

    `_repo_args()` hands `tracker_config.repo` to *every* verb, and every `gh` failure and timeout in
    this adapter renders its own argv, so a 404 on a read printed the token the repo-resolution failure
    no longer did. The strip therefore belongs on the argv, which is where all of those messages meet.
    """
    _configure(monkeypatch, **{"tracker_config.repo": f"https://x-access-token:{CANARY}@{HOST}/{REPO}.git"})
    _install(monkeypatch, (1, "", "gh: Not Found (HTTP 404)"))

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().get_issue("7")

    assert CANARY not in str(raised.value), f"the configured repo leaked through a verb's own argv: {raised.value}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "echoed",
    [
        "GraphQL: Could not resolve to a Repository with the name '{value}'.",
        "gh: Not Found (HTTP 404) requesting {value}",
        "argument error: {value} is not a valid path",
    ],
)
async def test_a_credential_gh_echoes_back_in_its_own_stderr_never_reaches_the_error(monkeypatch, echoed):
    """Found by review: the argv strip cannot cover the text `gh` writes about the argv.

    Real `gh` (2.96.0) quotes a credentialed `--repo` value back in its own error output for several
    reachable shapes — a GraphQL resolution failure, an HTTP error naming the URL, an argument error on a
    malformed one — and that stream went through scrubbing only, which redacts the credentials this
    process holds in its environment and can know nothing about one that only ever existed in
    `tracker_config.repo`. Every verb carries that value in its argv, so every verb's stderr could echo it.
    """
    configured = f"https://x-access-token:{CANARY}@{HOST}/{REPO}.git"
    _configure(monkeypatch, **{"tracker_config.repo": configured})
    _install(monkeypatch, (1, "", echoed.format(value=configured)))

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().get_issue("7")

    message = str(raised.value)
    assert CANARY not in message, f"gh's own stderr echoed the configured credential back: {message}"
    assert f"{HOST}/{REPO}" in message, f"the failure must still say which repository gh refused: {message}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "configured", [f"x-access-token:{CANARY}@{HOST}:{REPO}.git", f"x-access-token:{CANARY}@{HOST}/{REPO}"]
)
async def test_a_credential_gh_quotes_back_inside_a_sentence_never_reaches_the_error(monkeypatch, configured):
    """Found by review: `gh` does not echo the refused reference alone, it quotes it inside a sentence.

    Real `gh` (2.96.0) answers a credentialed `--repo` with `Could not resolve to a Repository with the
    name '<value>'. (repository)`, and the two schemeless spellings a git remote can take — scp
    `user:pass@host:path` and `user:pass@host/path` — were recognised only when they matched the whole
    string, which surrounding prose and `gh`'s own quotes and trailing period make impossible. Both are
    values `tracker_config.repo` accepts, so both reached this message with the token in them.
    """
    _configure(monkeypatch, **{"tracker_config.repo": configured})
    _install(monkeypatch, (1, "", f"GraphQL: Could not resolve to a Repository with the name '{configured}'. (repo)"))

    with pytest.raises(TrackerError, match="could not resolve it to a repository") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    message = str(raised.value)
    assert CANARY not in message, f"gh's own sentence carried the configured credential through: {message}"
    assert HOST in message, f"the failure must still name the repository gh refused: {message}"


@pytest.mark.anyio
async def test_a_credential_embedded_in_a_comment_body_never_reaches_the_error(monkeypatch, board):
    """A `--body` is free-form caller text, so a credentialed reference in it sits mid-sentence.

    Same gap as `gh`'s own prose, from the other direction: the body reaches every `gh` failure through
    `_shown`, and a reference pasted into the middle of one never matched a whole-value pattern. The text
    around it survives, because the failure is only useful if it still shows which comment failed.
    """
    _install(monkeypatch, (1, "", "gh: Not Found (HTTP 404)"))

    with pytest.raises(TrackerError) as raised:
        await adapter.GithubAdapter().post_comment("7", f"ping x-access-token:{CANARY}@{HOST}/{REPO} done")

    message = str(raised.value)
    assert CANARY not in message, f"a credential in a comment body leaked through the argv: {message}"
    assert "ping" in message and "done" in message, f"the rest of the body must still be shown: {message}"


@pytest.mark.anyio
async def test_a_project_value_holding_the_self_reference_syntax_is_reported_verbatim(monkeypatch):
    """`@me` is legitimate syntax, and an empty userinfo is not a credential to strip.

    The schemeless pattern allowed zero characters before the `@`, so `@me/abc` was reported back to
    whoever has to fix it as `me/abc` — a value they never set, in the one message whose whole job is to
    name the value that is wrong.
    """
    _configure(monkeypatch, **{"tracker_config.project": "@me/abc"})
    fake = _install(monkeypatch)

    with pytest.raises(TrackerError, match=r"tracker_config\.project must be") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    assert "'@me/abc'" in str(raised.value), f"a value with no credential in it must be shown as set: {raised.value}"
    assert fake.calls == [], f"an unusable project value must be refused before any gh call: {fake.calls}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "configured",
    [
        f"https://x-access-token:{CANARY}@{HOST}/orgs/octocat/projects",
        f"https://x-access-token:{CANARY}@{HOST}/orgs/octocat/projects/3",
    ],
)
async def test_a_credential_in_the_configured_project_never_reaches_the_error_message(monkeypatch, configured):
    """`tracker_config.project` is the other configured value, and it reaches more messages than one.

    The first spelling is refused as malformed, the second parses as `<owner>/<number>` with the whole URL
    as the owner — and the owner is named by the failure of every board read, write and verification here,
    none of which could recognise a credential inside it. So the owner is checked to be shaped like a login
    (or `@me`), which is all `gh --owner` takes, and the one message that prints the configured value goes
    through the same strip `tracker_config.repo`'s failures do.
    """
    _configure(monkeypatch, **{"tracker_config.project": configured})
    fake = _install(monkeypatch)

    with pytest.raises(TrackerError, match=r"tracker_config\.project must be") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    message = str(raised.value)
    assert CANARY not in message, f"a credential in the configured project leaked into an error: {message}"
    assert adapter._stripped_of_credentials(configured) in message, (
        f"the failure must still show the sanitised value the operator has to go and fix: {message}"
    )
    assert fake.calls == [], f"an unusable project value must be refused before any gh call: {fake.calls}"


@pytest.mark.anyio
async def test_only_issue_cards_answer_an_issue_search(monkeypatch):
    """`find_issues` owes the caller issues, and a board column holds PR and draft cards too.

    `gh issue view` reads a pull request URL without complaint, so a PR card used to come back as an
    issue — which the duplicate-work checks in the plan and spec skills read as prior work. The PR here
    has no `issue view` answer at all, so a read of it fails the test rather than passing quietly.
    """
    _configure(monkeypatch)
    pull = "https://github.com/octocat/repo/pull/8"
    items = {
        "items": [
            {"id": "ITEM_1", "status": "Ready", "type": "Task", "content": _content(7, ISSUE_URL, "Issue")},
            {"id": "ITEM_2", "status": "Ready", "type": "Task", "content": _content(8, pull, "PullRequest")},
            {"id": "ITEM_3", "status": "Ready", "type": "Task", "title": "a draft", "content": {"type": "DraftIssue"}},
        ],
        "totalCount": 3,
    }
    fake = _BoardReads(items, {ISSUE_URL: _list_row()})
    monkeypatch.setattr(adapter, "subprocess", fake)

    found = await adapter.GithubAdapter().find_issues(status="ready")

    assert [item["url"] for item in found["issues"]] == [ISSUE_URL], f"a non-issue card leaked in: {found}"
    assert (found["count"], found["is_last"]) == (1, True), found
    reads = [call for call in fake.calls if call[1:3] == ["issue", "view"]]
    assert [call[3] for call in reads] == [ISSUE_URL], (
        f"a pull request card must be excluded before it costs a read: {fake.calls}"
    )


@pytest.mark.anyio
async def test_a_card_with_no_content_type_is_a_failure_rather_than_a_guess(monkeypatch):
    """Guessing "issue" publishes a PR as one; guessing "not an issue" drops a real issue silently."""
    _configure(monkeypatch)
    fake = _BoardReads(_board_items([ISSUE_URL], kinds=[None]), {ISSUE_URL: _list_row()})
    monkeypatch.setattr(adapter, "subprocess", fake)

    with pytest.raises(TrackerError, match="no content type"):
        await adapter.GithubAdapter().find_issues(status="ready")

    assert not any(call[1:3] == ["issue", "view"] for call in fake.calls), (
        f"an unclassifiable card must not be read as an issue: {fake.calls}"
    )


@pytest.mark.anyio
async def test_a_card_with_neither_a_content_type_nor_a_url_is_a_failure_not_a_silent_drop(monkeypatch):
    """Found by review: the url-less drop swallowed the malformed card before the type check saw it.

    A `DraftIssue` legitimately has no URL and is excluded silently, as it must be — nothing here can read
    one. A card with no URL *and* no content type is not a draft, it is a shape this adapter cannot read,
    and dropping it as though it were one takes a card off a page that still reports itself complete.
    """
    _configure(monkeypatch)
    draft = {"id": "ITEM_1", "status": "Ready", "type": "Task", "content": {"type": "DraftIssue"}}
    malformed = {"id": "ITEM_2", "status": "Ready", "type": "Task", "content": {}}
    issue = {"id": "ITEM_3", "status": "Ready", "type": "Task", "content": _content(7, ISSUE_URL, "Issue")}

    drafts_only = _BoardReads({"items": [draft, issue], "totalCount": 2}, {ISSUE_URL: _list_row()})
    monkeypatch.setattr(adapter, "subprocess", drafts_only)
    found = await adapter.GithubAdapter().find_issues(status="ready")
    assert [item["url"] for item in found["issues"]] == [ISSUE_URL], f"a draft card must not answer: {found}"

    monkeypatch.setattr(
        adapter, "subprocess", _BoardReads({"items": [malformed, issue], "totalCount": 2}, {ISSUE_URL: _list_row()})
    )
    with pytest.raises(TrackerError, match="no content type") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    assert "ITEM_2" in str(raised.value), f"the failure must name the card it cannot read: {raised.value}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("card", "reason"),
    [
        ({"id": "ITEM_1", "status": "Ready", "type": "Task", "content": None}, "REDACTED"),
        ({"id": "ITEM_1", "status": "Ready", "type": "Task"}, "no content key at all"),
        ({"id": "ITEM_1", "status": "Ready", "type": "Task", "content": "REDACTED"}, "content that is not an object"),
    ],
)
async def test_a_card_the_token_may_not_view_is_skipped_rather_than_failing_every_read(monkeypatch, card, reason):
    """Found by review: the url-less guard could not tell a documented board state from a broken payload.

    Projects v2 returns an item the credential may not view as `REDACTED`, and `gh` renders that card's
    `content` as `null` — no type, no URL, nothing any verb here could address. Raising on it failed the
    whole board, so one invisible card broke `find_issues` and, because the index is built for the entire
    board, `get_issue` on every other issue too. A `content` object that is present but empty is still the
    unreadable shape the sibling test above covers; only "no content object at all" is skipped — including
    a `content` that is not an object, which must not reach the board index as an `AttributeError` either.
    """
    _configure(monkeypatch)
    issue = {"id": "ITEM_2", "status": "Ready", "type": "Task", "content": _content(7, ISSUE_URL, "Issue")}
    items = {"items": [card, issue], "totalCount": 2}

    monkeypatch.setattr(adapter, "subprocess", _BoardReads(items, {ISSUE_URL: _list_row()}))
    found = await adapter.GithubAdapter().find_issues(status="ready")

    assert [item["url"] for item in found["issues"]] == [ISSUE_URL], (
        f"a card the token cannot view ({reason}) must not take the rest of the board with it: {found}"
    )
    assert (found["count"], found["is_last"]) == (1, True), found

    _install(monkeypatch, _json(_issue_view()), _json(items))
    read = await adapter.GithubAdapter().get_issue("7")

    assert (read["url"], read["status"]) == (ISSUE_URL, "ready"), (
        f"one unviewable card poisoned the board index every other read goes through: {read}"
    )


@pytest.mark.anyio
async def test_an_unset_repo_scopes_the_search_to_the_repo_gh_resolves_not_the_whole_board(monkeypatch):
    """Unset means "the repo `gh` resolves here", exactly as `_repo_args()` means it for every write.

    A board may span repositories, so treating unset as "no repo filter" answered a queue query with
    another repository's work — work no other verb in this adapter would have touched.
    """
    _configure(monkeypatch, **{"tracker_config.repo": ""})
    elsewhere = "https://github.com/octocat/other-repo/issues/3"
    fake = _BoardReads(
        _board_items([ISSUE_URL, elsewhere]), {ISSUE_URL: _list_row()}, resolved_repo="OctoCat/Repo"
    )
    monkeypatch.setattr(adapter, "subprocess", fake)

    found = await adapter.GithubAdapter().find_issues(status="ready")

    assert [item["url"] for item in found["issues"]] == [ISSUE_URL], (
        f"another repository's card answered this repository's queue: {found}"
    )
    assert fake.calls[0][1:] == ["repo", "view", *REPO_VIEW_ARGS], (
        f"an unset repo names no reference, so gh resolves the working directory's: {fake.calls[0]}"
    )
    reads = [call for call in fake.calls if call[1:3] == ["issue", "view"]]
    assert [call[3] for call in reads] == [ISSUE_URL], (
        f"the resolved repo must scope the candidates before any of them is read: {fake.calls}"
    )


@pytest.mark.anyio
async def test_a_repo_gh_cannot_resolve_is_a_failure_not_a_board_wide_search(monkeypatch):
    """No repository to scope to must fail loudly, not widen the search to every repo on the board."""
    _configure(monkeypatch, **{"tracker_config.repo": ""})
    fake = _BoardReads(_board_items([ISSUE_URL]), {ISSUE_URL: _list_row()}, resolved_repo="")
    monkeypatch.setattr(adapter, "subprocess", fake)

    with pytest.raises(TrackerError, match="working directory") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    assert "tracker_config.repo" in str(raised.value), f"the failure must say how to fix it: {raised.value}"
    assert [call[1:3] for call in fake.calls] == [["repo", "view"]], (
        f"an unresolved repo must not fall back to reading the whole board: {fake.calls}"
    )


@pytest.mark.anyio
async def test_a_filter_that_matches_nothing_stops_at_the_read_bound_instead_of_scanning_the_column(monkeypatch):
    """`limit` cannot bound the reads when `text` rejects every candidate after it has been read.

    Each rejected candidate is one `gh issue view` subprocess, individually bounded by `TIMEOUT_SECONDS`
    and in aggregate by nothing, so a wide column turned a query into minutes and then reported nothing
    — indistinguishable from an empty board. The bound fails instead, saying what to narrow.
    """
    _configure(monkeypatch)
    monkeypatch.setattr(adapter, "MAX_BOARD_READS", 3)
    urls = [f"https://github.com/octocat/repo/issues/{number}" for number in range(1, 7)]
    views = {
        url: {**_list_row(number=number, url=url), "body": "nothing a needle matches"}
        for number, url in enumerate(urls, start=1)
    }
    fake = _BoardReads(_board_items(urls), views)
    monkeypatch.setattr(adapter, "subprocess", fake)

    with pytest.raises(TrackerError, match="narrow") as raised:
        await adapter.GithubAdapter().find_issues(status="ready", text="widget")

    assert "3" in str(raised.value), f"the failure must name the bound it hit: {raised.value}"
    reads = [call for call in fake.calls if call[1:3] == ["issue", "view"]]
    assert len(reads) == adapter.MAX_BOARD_READS, (
        f"the bound must stop the reads rather than report after them all: {len(reads)} reads"
    )


@pytest.mark.anyio
async def test_a_column_wider_than_the_read_bound_still_answers_a_plain_page(monkeypatch):
    """The bound is on reads, not on the column: with no text or parent filter, `limit` bounds them.

    Refusing a query that costs `limit + 1` reads because the column behind it is large would break the
    queue query this verb exists for, on exactly the busy board where it matters most.
    """
    _configure(monkeypatch)
    monkeypatch.setattr(adapter, "MAX_BOARD_READS", 3)
    urls = [f"https://github.com/octocat/repo/issues/{number}" for number in range(1, 7)]
    views = {url: _list_row(number=number, url=url) for number, url in enumerate(urls, start=1)}
    fake = _BoardReads(_board_items(urls), views)
    monkeypatch.setattr(adapter, "subprocess", fake)

    found = await adapter.GithubAdapter().find_issues(status="ready", limit=2)

    assert (found["count"], found["is_last"]) == (2, False), f"a page of a wide column must still come back: {found}"
    reads = [call for call in fake.calls if call[1:3] == ["issue", "view"]]
    assert len(reads) == 3, f"one read past the page is all a bounded page costs: {len(reads)} reads"


@pytest.mark.anyio
async def test_the_read_bound_reached_with_a_full_page_returns_it_as_a_truncated_page(monkeypatch):
    """A page the caller asked for is not worth discarding, and must not be called complete either.

    Two matches fill the page of two, then the bound stops the read that would have decided whether a
    third exists. Raising here would throw away results that are correct; `is_last: true` would claim
    the board holds nothing else, which is the false-completeness bug this whole path keeps growing.
    """
    _configure(monkeypatch)
    monkeypatch.setattr(adapter, "MAX_BOARD_READS", 3)
    urls = [f"https://github.com/octocat/repo/issues/{number}" for number in range(1, 7)]
    views = {
        url: {**_list_row(number=number, url=url), "body": "the widget" if number <= 2 else "unrelated"}
        for number, url in enumerate(urls, start=1)
    }
    fake = _BoardReads(_board_items(urls), views)
    monkeypatch.setattr(adapter, "subprocess", fake)

    found = await adapter.GithubAdapter().find_issues(status="ready", text="widget", limit=2)

    assert [item["url"] for item in found["issues"]] == urls[:2], f"the matches read must survive: {found}"
    assert (found["count"], found["is_last"]) == (2, False), (
        f"a page the bound cut short must report itself unfinished: {found}"
    )
    reads = [call for call in fake.calls if call[1:3] == ["issue", "view"]]
    assert len(reads) == 3, f"the bound must still bound the reads: {len(reads)}"


@pytest.mark.anyio
async def test_a_candidate_read_that_returns_no_issue_is_a_failure_not_a_result(monkeypatch):
    """A zero-exit read with no url is a failed read, as `get_issue` already treats it.

    Without the check the card became a result whose `id` and `url` were empty strings: a caller handed
    an issue it cannot then read, inside a page reporting itself complete.
    """
    _configure(monkeypatch)
    fake = _BoardReads(_board_items([ISSUE_URL]), {ISSUE_URL: {}})
    monkeypatch.setattr(adapter, "subprocess", fake)

    with pytest.raises(TrackerError, match="returned no issue"):
        await adapter.GithubAdapter().find_issues(status="ready")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("filters", "expected"),
    [({"status": "in_progress"}, "unknown canonical status"), ({"issue_type": "story"}, "unknown canonical type")],
)
async def test_an_unrecognised_status_or_type_token_is_refused_not_answered_with_an_empty_page(
    monkeypatch, board, filters, expected
):
    """A token that matches no card is a bad query, and this was the only verb here answering it as data.

    `in_progress` for `in-progress` matched nothing and came back `count: 0, is_last: true`, which the
    duplicate-work checks in the plan and spec skills read as "no prior work on this". Every other verb
    already refuses an unknown canonical token through `native_status`/`native_type`; so does this one now.
    """
    fake = _install(monkeypatch)

    with pytest.raises(TrackerError, match=expected):
        await adapter.GithubAdapter().find_issues(**filters)

    assert fake.calls == [], f"a token no board value can match must be refused before any gh call: {fake.calls}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("filters", "drift", "field", "requested"),
    [
        ({"status": "ready"}, {"status_options": ("Backlog", "Up Next", "Done")}, "Status", "Ready"),
        ({"issue_type": "task"}, {"type_options": ("Epic", "Chore")}, "Type", "Task"),
    ],
)
async def test_a_board_name_the_config_does_not_match_fails_instead_of_emptying_the_page(
    monkeypatch, filters, drift, field, requested
):
    """Found by review: only the write path noticed board/config drift; the read path went quiet.

    A canonical token maps to a native board name through the `columns.*` config, and the write path checks
    that name against the board's real field options before writing. The read path compared canonical
    tokens instead — and `canonical_status`/`canonical_type` pass a board value no configured column names
    through unchanged, so a renamed column or a `Type` option outside `Epic`/`Task`/`Bug` matched no card
    and `find_issues` answered `count: 0, is_last: true` from a board with work on it. The card here is
    deliberately sitting in the drifted column: it is the work the caller would have been told was absent.
    """
    _configure(monkeypatch)
    fake = _BoardReads(
        _board_items([ISSUE_URL], status="Up Next", issue_type="Chore"),
        {ISSUE_URL: _list_row()},
        fields=_board_fields(**drift),
    )
    monkeypatch.setattr(adapter, "subprocess", fake)

    with pytest.raises(TrackerError, match="has no option matching") as raised:
        await adapter.GithubAdapter().find_issues(**filters)

    message = str(raised.value)
    assert f"{field!r}" in message and f"{requested!r}" in message, (
        f"the failure must name the field and the option the config asked for: {message}"
    )
    assert all(option in message for option in drift[f"{field.lower()}_options"]), (
        f"the failure must name what the board actually offers, as the write path's does: {message}"
    )
    assert "empty page" in message, f"the failure must say the search was refused, not answered: {message}"
    assert not any(call[1:3] == ["issue", "view"] for call in fake.calls), (
        f"a filter no board value can match must not cost a candidate read: {fake.calls}"
    )


@pytest.mark.anyio
async def test_a_card_whose_url_holds_no_repository_is_a_failure_not_a_silent_drop(monkeypatch):
    """Found by review, sweeping for the pattern: an unreadable URL was dropped like another repo's card.

    The repository comparison runs before the issue-card check and treated "no pair in this URL" as "not
    this repository", so such a card left the candidate set without a word and the page still reported
    itself complete — and the unclassifiable-card failure never saw it either.
    """
    _configure(monkeypatch)
    fake = _BoardReads(_board_items(["https://github.com/issues"]), {})
    monkeypatch.setattr(adapter, "subprocess", fake)

    with pytest.raises(TrackerError, match="no owner/repo") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    assert REPO in str(raised.value), f"the failure must name the repository it could not compare: {raised.value}"


@pytest.mark.anyio
async def test_a_board_whose_names_match_the_config_still_answers_with_its_matches(monkeypatch):
    """The control for the check above: nothing drifted, so the page must come back, one resolve deep."""
    _configure(monkeypatch)
    fake = _BoardReads(_board_items([ISSUE_URL], status="Ready", issue_type="Task"), {ISSUE_URL: _list_row()})
    monkeypatch.setattr(adapter, "subprocess", fake)

    found = await adapter.GithubAdapter().find_issues(status="ready", issue_type="task")

    assert [item["url"] for item in found["issues"]] == [ISSUE_URL], f"a matching board lost its card: {found}"
    assert (found["count"], found["is_last"]) == (1, True), found
    assert len([call for call in fake.calls if call[1:3] == ["project", "view"]]) == 1, (
        f"both filters must share one board resolution, as two writes in one call do: {fake.calls}"
    )


@pytest.mark.anyio
async def test_a_board_larger_than_one_read_fails_instead_of_answering_from_its_first_page(monkeypatch, board):
    """`item-list` reports the board's `totalCount` beside a list `--limit ITEM_LIMIT` has truncated.

    Every caller of the board read treats it as the whole board — `is_last`, the preflight's reachability
    check, the write-back verification's "the card is not there" — so a truncated read is the same
    false completeness this path keeps growing, one level lower down. Verified against the real `gh`: a
    board of 65 items answers `--limit 3` with three items and `totalCount: 65`.
    """
    truncated = {**_board_items([ISSUE_URL]), "totalCount": 2}
    fake = _install(monkeypatch, _repo_view(), _json(truncated))

    with pytest.raises(TrackerError, match="ITEM_LIMIT") as raised:
        await adapter.GithubAdapter().find_issues(status="ready")

    assert "2" in str(raised.value) and adapter.ITEM_LIMIT in str(raised.value), (
        f"the failure must name both counts so it is actionable: {raised.value}"
    )
    assert not any(call[1:3] == ["issue", "view"] for call in fake.calls), (
        f"a board that cannot be read completely must not be answered from partially: {fake.calls}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "count",
    [{"totalCount": "1"}, {"totalCount": 1.0}, {"totalCount": True}, {"totalCount": False}, {"totalCount": None}, {}],
)
async def test_a_board_read_whose_item_count_is_unreadable_is_a_failure_not_a_skipped_check(
    monkeypatch, board, count
):
    """Found by review: the completeness check fired only for a clean `int`, so drift bypassed it.

    A `totalCount` of `"65"` — or a float, a bool, a null, or an absent key — skipped the guard entirely
    and the truncated read was answered from as though it were the whole board: the same false
    completeness this check exists to prevent, one type away from where it was fixed. `gh` prints the
    count for every board, so an unreadable one is a shape this adapter must not read a board out of.
    """
    _install(monkeypatch, _repo_view(), _json({"items": _board_items([ISSUE_URL])["items"], **count}))

    with pytest.raises(TrackerError, match="item count"):
        await adapter.GithubAdapter().find_issues(status="ready")


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [{"totalCount": 5}, {"items": {}, "totalCount": 0}, {}])
async def test_a_board_read_with_no_item_list_is_a_failure_not_an_empty_board(monkeypatch, board, payload):
    """`_as_list` is tolerant by design, and tolerance here reads an unreadable board as an empty one.

    The shared helper stays tolerant — the relations it also parses are legitimately absent — so the check
    lives in `_raw_items`, where a missing `items` key means the board was not read at all. That is not the
    same answer as a board which genuinely holds nothing, and a caller cannot tell the two apart.
    """
    _install(monkeypatch, _repo_view(), _json(payload))

    with pytest.raises(TrackerError, match="no list of items"):
        await adapter.GithubAdapter().find_issues(status="ready")


@pytest.mark.anyio
async def test_a_repo_gh_answers_with_something_other_than_one_pair_is_a_failure(monkeypatch):
    """A zero-exit answer this cannot compare a card against is as invisible as an empty board.

    `gh repo view` prints one `nameWithOwner`, so anything else is a shape this adapter does not know how
    to scope a search by — and scoping by it silently would filter every card out while the page still
    reported itself complete.
    """
    _configure(monkeypatch)
    fake = _BoardReads(_board_items([ISSUE_URL]), {ISSUE_URL: _list_row()}, resolved_repo="not-a-pair")
    monkeypatch.setattr(adapter, "subprocess", fake)

    with pytest.raises(TrackerError, match="not one owner/repo pair"):
        await adapter.GithubAdapter().find_issues(status="ready")

    assert [call[1:3] for call in fake.calls] == [["repo", "view"]], (
        f"an unusable answer must not fall back to reading the whole board: {fake.calls}"
    )


@pytest.mark.anyio
async def test_a_board_entry_that_is_not_an_item_object_is_a_failure_not_a_shorter_board(monkeypatch, board):
    """Filtering unreadable entries out is the same silent shortening as truncating the read."""
    payload = {"items": [_board_items([ISSUE_URL])["items"][0], "junk"], "totalCount": 2}
    _install(monkeypatch, _repo_view(), _json(payload))

    with pytest.raises(TrackerError, match="other than an item object"):
        await adapter.GithubAdapter().find_issues(status="ready")


@pytest.mark.anyio
async def test_a_genuinely_empty_board_is_still_an_empty_page(monkeypatch, board):
    """The counterpart to the check above: `items: []` is a real answer and must not raise."""
    _install(monkeypatch, _repo_view(), _json({"items": [], "totalCount": 0}), *_resolution())

    found = await adapter.GithubAdapter().find_issues(status="ready")

    assert (found["count"], found["is_last"], found["issues"]) == (0, True, []), (
        f"a board with nothing on it is an empty page, completely read: {found}"
    )


@pytest.mark.anyio
async def test_the_write_verification_retry_tolerates_a_board_read_the_search_paths_refuse(monkeypatch, board):
    """The verifying re-read is retried because the item list is eventually consistent, and a board `gh`
    pages through internally can transiently answer with a `totalCount` its items disagree with.

    Raising on that inside the retry loop aborts the retry on its first attempt and reports a write that
    landed as failed — the exact failure the retry exists to prevent, reintroduced one level down. The
    same payload must still fail a search and the preflight, where completeness is what makes the answer
    true rather than something a later read can correct.
    """
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    hiccup, malformed = {"items": [], "totalCount": 3}, {"items": {}, "totalCount": 0}
    _install(
        monkeypatch,
        _json({"url": ISSUE_URL}),
        _json(PROJECT_VIEW),
        _json(_fields()),
        _json(_items()),
        (0, "", ""),
        _json(hiccup),
        _json(malformed),
        _json(_items(status="In progress")),
    )

    moved = await adapter.GithubAdapter().set_status("7", "in-progress")

    assert moved == {"id": ISSUE_URL, "status": "in-progress", "native": "In Progress"}, (
        f"a transient board-read hiccup must not report a landed write as failed: {moved}"
    )

    for payload, expected in ((hiccup, "ITEM_LIMIT"), (malformed, "no list of items")):
        _install(monkeypatch, _repo_view(), _json(payload))
        with pytest.raises(TrackerError, match=expected):
            await adapter.GithubAdapter().find_issues(status="ready")


@pytest.mark.anyio
async def test_a_board_read_failure_that_is_not_about_scopes_keeps_its_own_preflight_message(monkeypatch, board):
    """The scopeless-token fallback reads the board, and only `gh` refusing that read is a scope problem.

    A board too large to read in one call, or a payload this adapter will not parse as a board, is a
    correctly credentialled setup with a different fault, and relabelling it sends whoever reads the
    preflight to `gh auth refresh` over something no grant will fix.
    """
    _install(
        monkeypatch, (0, "gh version 2.96.0 (2025-01-01)\n", ""), (0, _auth_status(), ""), _json({"totalCount": 5})
    )

    with pytest.raises(TrackerError, match="no list of items") as raised:
        await adapter.GithubAdapter().preflight()

    assert "auth refresh" not in str(raised.value), (
        f"a board-shape failure must not be reported as a missing token scope: {raised.value}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_json({}), "no list of issues"),
        ((0, "", ""), "no list of issues"),
        (_json({"issues": {}}), "no list of issues"),
        (_json("not a list"), "no list of issues"),
        (_json([_list_row(), "junk"]), "other than an issue object"),
    ],
)
async def test_an_unreadable_issue_list_is_a_failure_not_an_empty_page(monkeypatch, board, result, expected):
    """The path with no board filter never got the hardening the board-filtered path was given four times.

    `gh issue list` went through the deliberately tolerant `_as_list`, so a response this cannot read came
    back as `count: 0, is_last: true` — a caller told the repository holds nothing matching, which is the
    one wrong answer it cannot tell from the truth.
    """
    _install(monkeypatch, result)

    with pytest.raises(TrackerError, match=expected):
        await adapter.GithubAdapter().find_issues()


@pytest.mark.anyio
async def test_a_listed_row_with_no_url_is_a_failure_not_an_unaddressable_result(monkeypatch, board):
    """Found by review: the url-less row the board path and `get_issue` both refuse was a found issue here.

    It came back as `{"id": "", "url": ""}` — a result naming an issue the caller cannot then read, comment
    on or move, because `_checked_ref` rejects the empty reference every follow-up call would pass. A row
    `gh issue list --json url` reports without one is a shape problem, so it fails like its siblings.
    """
    fake = _install(monkeypatch, _json([{key: value for key, value in _list_row().items() if key != "url"}]))

    with pytest.raises(TrackerError, match="no issue URL"):
        await adapter.GithubAdapter().find_issues()

    assert [call[1:3] for call in fake.calls] == [["issue", "list"]], (
        f"an unreadable page must fail before the board is read for it: {fake.calls}"
    )


@pytest.mark.anyio
async def test_a_card_whose_content_is_not_an_object_does_not_break_a_board_write(monkeypatch, board):
    """Found by review: the non-dict-`content` guard covered one of the three sites that read `content`.

    The board index got it last round; the board-item lookup a write does and the write's verifying re-read
    did not, so the same `content: "REDACTED"` card that `find_issues` now tolerates crossed the tool
    boundary from `set_status` as a raw `AttributeError` instead of a `TrackerError`.
    """
    unviewable = {"id": "ITEM_9", "status": "Ready", "type": "Task", "content": "REDACTED"}
    board_read = {"items": [unviewable, *_items(status="In progress")["items"]], "totalCount": 2}
    _install(
        monkeypatch,
        _json({"url": ISSUE_URL}),
        _json(PROJECT_VIEW),
        _json(_fields()),
        _json(board_read),
        (0, "", ""),
        _json(board_read),
    )

    moved = await adapter.GithubAdapter().set_status("7", "in-progress")

    assert moved == {"id": ISSUE_URL, "status": "in-progress", "native": "In Progress"}, (
        f"a card the credential may not view must not break the write's board scan or its re-read: {moved}"
    )


@pytest.mark.anyio
async def test_a_repository_with_no_matching_issues_is_still_an_empty_page(monkeypatch, board):
    """The counterpart to the check above: `gh issue list` printing `[]` is a real, complete answer."""
    _install(monkeypatch, _json([]), _json(_items(present=False)))

    found = await adapter.GithubAdapter().find_issues()

    assert (found["count"], found["is_last"], found["issues"]) == (0, True, []), found


@pytest.mark.anyio
async def test_a_non_positive_limit_is_refused_rather_than_forwarded_to_gh(monkeypatch, board):
    """`--limit 0` asks `gh` for every issue; the other adapter refuses it, and so must this one."""
    fake = _install(monkeypatch)

    with pytest.raises(TrackerError, match="limit must be a positive number of issues"):
        await adapter.GithubAdapter().find_issues(limit=0)

    assert fake.calls == [], "the refusal must come before any gh call"


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
        _json({"login": "octocat"}),
        (0, f"{ISSUE_URL}\n", ""),
        (0, json.dumps({"assignees": [{"login": "octocat"}]}), ""),
    )

    assigned = await adapter.GithubAdapter().assign("7")

    assert fake.calls[0][1:] == ["api", "user"], f"'@me' must be resolved to a login to check against: {fake.calls}"
    assert fake.calls[1][1:] == ["issue", "edit", "7", *REPO_ARGS, "--add-assignee", "@me"], fake.calls[1]
    assert assigned == {"id": ISSUE_URL, "assignee": "octocat"}, assigned


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("assignees", "reason"),
    [([], "no assignee at all"), ([{"login": "hubot"}], "somebody else's issue")],
)
async def test_an_assignment_the_read_back_does_not_confirm_is_a_failure(monkeypatch, board, assignees, reason):
    """`--add-assignee` is a no-op on an already-assigned issue and exits zero either way.

    So a non-empty assignee list proves someone owns the issue, not that this account does; reporting
    the first login back would fabricate a confirmation of a write that never happened.
    """
    fake = _install(
        monkeypatch,
        _json({"login": "octocat"}),
        (0, f"{ISSUE_URL}\n", ""),
        (0, json.dumps({"assignees": assignees}), ""),
    )

    with pytest.raises(TrackerError, match="unconfirmed"):
        await adapter.GithubAdapter().assign("7")

    assert len(fake.calls) == 3, f"the read-back must happen before the result is trusted ({reason})"


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


def _comparable(call: list[str]) -> list[str]:
    """One `gh` call with the adapter's deliberate `field-list --limit` divergence normalised away.

    The CLI helper inherits `gh`'s 30-field default and cannot be changed here: its deployment stays
    byte-identical. The adapter asks for the board's fields at `gh`'s maximum instead, because a board
    wide enough to push `Status` past the thirtieth field would otherwise resolve without it. Only that
    one flag is normalised, so every other difference in the two call sequences still fails the test.
    """
    if call[:2] == ["project", "field-list"] and "--limit" in call:
        at = call.index("--limit")
        return call[:at] + call[at + 2 :]
    return call


def _assert_same_calls(mine: list[list[str]], theirs: list[list[str]]) -> None:
    """The two must issue the same `gh` calls, bar the adapter's documented extra read-back."""
    stripped = [_comparable(call[1:]) for call in mine]
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
    assert [_comparable(call[1:]) for call in router.calls] == theirs_calls, (
        "the two resolvers issue different gh calls"
    )
    assert set(mine["fields"]) == {"Status", "Type"}, f"both must keep only the single-selects: {set(mine['fields'])}"
    for option in ("In progress", "IN PROGRESS", " in progress ", "On hold"):
        assert adapter._option_id(mine, "Status", option) == cli_helper._option_id(theirs, "Status", option), option
    no_type: dict[str, Any] = {"type": None, "status": "Icebox", "content": {"number": 1, "url": ISSUE_URL}}
    raw_items: tuple[dict[str, Any], dict[str, Any]] = (_items()["items"][0], no_type)
    for raw in raw_items:
        mapped = adapter._normalize_item(raw)
        assert {key: value for key, value in mapped.items() if key != "kind"} == cli_helper._normalize_item(raw), (
            f"the ported item mapping disagrees with the CLI helper's for {raw}"
        )
        assert mapped["kind"] == (raw.get("content") or {}).get("type"), (
            "the adapter's one addition is the card's own content type, carried through unmapped: the CLI "
            "helper lists board items as board items, while find_issues owes its caller issues and must be "
            f"able to tell a pull request card from an issue card. Got {mapped['kind']!r} for {raw}"
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
    assert [_comparable(call[1:]) for call in router.calls] == theirs_calls, (
        "both must re-resolve once before failing"
    )
    assert isinstance(theirs.value, SystemExit) and not isinstance(mine.value, SystemExit), (
        "deliberate divergence: the CLI helper exits the process, the adapter raises TrackerError, because "
        "this process has other tool calls to serve after a bad one"
    )
