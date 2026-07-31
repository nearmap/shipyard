"""Offline tests for the GitHub adapter: the `gh` transport is replaced, never invoked.

The verbs are awaited, but the seam under test is unchanged: the sync helpers still call
`subprocess.run`, so monkeypatching `adapter.subprocess` still intercepts every `gh` call — now
from the worker thread the verb offloads to.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading

import pytest

from sy_tools.tracker import TIMEOUT_SECONDS, TrackerError
from sy_tools.tracker.github import adapter

GIST_URL = "https://gist.github.com/octocat/abc123"
COMMENT_URL = "https://github.com/octocat/repo/issues/7#issuecomment-1"


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
