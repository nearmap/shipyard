"""Offline tests for the GitHub adapter: the `gh` transport is replaced, never invoked."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from mcp.tracker import TrackerError
from mcp.tracker.github import adapter

GIST_URL = "https://gist.github.com/octocat/abc123"
COMMENT_URL = "https://github.com/octocat/repo/issues/7#issuecomment-1"


class _FakeSubprocess:
    """Stands in for the `subprocess` module inside the adapter: records argv, replays results."""

    def __init__(self, *results: tuple[int, str, str]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.PIPE = subprocess.PIPE

    def run(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
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


def test_attach_artifact_gists_the_file_then_links_it_from_a_comment(tmp_path, monkeypatch):
    fake = _install(monkeypatch, *_happy_path())
    path = _artifact(tmp_path)

    evidence = adapter.GithubAdapter().attach_artifact("AM-1", path)

    create, verify, comment = fake.calls
    assert create[:3] == ["gh", "gist", "create"], create
    assert str(path) in create, "the gist must be created from the artifact file itself"
    assert verify[:3] == ["gh", "api", "gists/abc123"], "privacy must be re-read, not assumed"
    assert comment[:4] == ["gh", "issue", "comment", "AM-1"], comment
    body = comment[comment.index("--body") + 1]
    assert GIST_URL in body, "the comment must carry the gist URL, or the artifact is undiscoverable"
    assert evidence["gist_url"] == GIST_URL, "evidence must report the URL the transport produced"
    assert evidence["comment_url"] == COMMENT_URL, "evidence must report the comment the write returned"


def test_the_gist_is_never_created_public(tmp_path, monkeypatch):
    fake = _install(monkeypatch, *_happy_path())

    adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert "--public" not in fake.calls[0], (
        "gh gists are secret by default; passing --public would publish the transcript irrevocably"
    )


def test_a_gist_that_reads_back_public_is_refused_before_any_comment(tmp_path, monkeypatch):
    fake = _install(monkeypatch, *_happy_path(secret=False))

    with pytest.raises(TrackerError, match="public"):
        adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert len(fake.calls) == 2, "a public gist must not be linked from the work item"


@pytest.mark.parametrize(
    ("result", "reason"),
    [((0, "", ""), "empty output"), ((1, "", "HTTP 422"), "non-zero exit")],
)
def test_a_failed_gist_call_is_never_a_silent_success(tmp_path, monkeypatch, result, reason):
    fake = _install(monkeypatch, result)

    with pytest.raises(TrackerError):
        adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert len(fake.calls) == 1, f"{reason} must stop the attachment, not fall through to a comment"


def test_a_credential_in_command_output_never_reaches_the_error_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIPYARD_TEST_TOKEN", "s3cr3t-value-not-for-logs")
    _install(monkeypatch, (1, "", "bad credentials: s3cr3t-value-not-for-logs"))

    with pytest.raises(TrackerError) as raised:
        adapter.GithubAdapter().attach_artifact("AM-1", _artifact(tmp_path))

    assert "s3cr3t-value-not-for-logs" not in str(raised.value), "a held credential leaked into an error"


def test_preflight_reports_facts_without_the_token(monkeypatch):
    status = (
        "github.com\n  ✓ Logged in to github.com account octocat (keyring)\n"
        "  - Token: gho_exampletokenvalue\n  - Token scopes: 'gist', 'project', 'repo'\n"
    )
    _install(monkeypatch, (0, "gh version 2.94.0 (2025-01-01)\n", ""), (0, status, ""))

    facts = adapter.GithubAdapter().preflight()

    assert facts["account"] == "octocat" and facts["scopes"] == ["gist", "project", "repo"], facts
    assert "gho_exampletokenvalue" not in json.dumps(facts), "preflight must never return a token"


def test_missing_gh_is_an_actionable_preflight_failure(monkeypatch):
    class _Missing:
        PIPE = subprocess.PIPE

        def run(self, argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(argv[0])

    monkeypatch.setattr(adapter, "subprocess", _Missing())

    with pytest.raises(TrackerError, match="not installed"):
        adapter.GithubAdapter().preflight()
