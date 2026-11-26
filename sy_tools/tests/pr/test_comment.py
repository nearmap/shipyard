"""`sy_tools.pr.comment.post`, with `gh` replaced at the subprocess seam so no test reaches a code host."""
from __future__ import annotations

import json
import subprocess

import pytest

from sy_tools.pr import comment

SESSION = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
PR = "https://git.example.test/acme/widgets/pull/42"


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """Records each `gh` invocation and answers it by echoing the sent body back as the created comment."""
    calls: list[dict] = []
    monkeypatch.setattr(comment.config, "resolved_root", lambda: tmp_path)
    stored = {"mutate": None}

    def run(args, *, input, cwd, **_kwargs):
        body = json.loads(input)["body"]
        calls.append({"args": args, "body": body, "cwd": cwd})
        if stored["mutate"]:
            body = stored["mutate"](body)
        out = {"id": 7, "html_url": f"{PR}#issuecomment-7", "body": body}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(out), stderr="")

    monkeypatch.setattr(comment.subprocess, "run", run)
    return calls, stored, tmp_path


def test_a_top_level_post_appends_the_marker_as_the_last_line(gh):
    calls, _, root = gh
    result = comment.post(PR, "Looks good.\n", SESSION)
    assert result == {"url": f"{PR}#issuecomment-7", "id": 7, "session": SESSION}, result
    (call,) = calls
    assert call["args"] == [
        "gh", "api", "--hostname", "git.example.test", "--method", "POST",
        "repos/acme/widgets/issues/42/comments", "--input", "-",
    ], call["args"]
    assert call["cwd"] == root, "gh must run in the repository the configuration resolved against"
    assert call["body"] == f"Looks good.\n\n<!-- shipyard:agent session={SESSION} -->", call["body"]
    marker = comment.MARKER_RE.search(call["body"])
    assert marker is not None and marker["session"] == SESSION, "readers anchor on the last line"


def test_a_reply_goes_to_the_review_comment_thread(gh):
    calls, _, _ = gh
    comment.post(PR + "/", "Fixed.", SESSION, reply_to=991)
    assert calls[0]["args"][6] == "repos/acme/widgets/pulls/42/comments/991/replies", calls[0]["args"]


def test_the_marker_regex_ignores_a_marker_that_is_not_the_last_line():
    body = f"<!-- shipyard:agent session={SESSION} -->\nquoted later"
    assert comment.MARKER_RE.search(body) is None, "only a final-line marker is the writer's"


@pytest.mark.parametrize("session", ["", "not-a-uuid", SESSION.upper(), SESSION + "0", f" {SESSION}"])
def test_a_session_that_is_not_a_lowercase_uuid_is_refused(gh, session):
    with pytest.raises(comment.CommentError, match="lowercase UUID"):
        comment.post(PR, "x", session)
    assert not gh[0], "a refused call must never reach gh"


@pytest.mark.parametrize("body", [
    f"x\n\n<!-- shipyard:agent session={SESSION} -->",
    "x <!--shipyard:agent session=forged -->",
    "<!-- SHIPYARD:AGENT -->",
])
def test_a_body_carrying_a_marker_is_refused(gh, body):
    with pytest.raises(comment.CommentError, match="already carries"):
        comment.post(PR, body, SESSION)
    assert not gh[0], "a refused call must never reach gh"


@pytest.mark.parametrize("url", [
    "https://git.example.test/acme/widgets/issues/42",
    "https://git.example.test/acme/widgets/pull/42/files",
    "http://git.example.test/acme/widgets/pull/42",
    "acme/widgets#42",
])
def test_anything_but_a_pull_request_url_is_refused(gh, url):
    with pytest.raises(comment.CommentError, match="not a pull request URL"):
        comment.post(url, "x", SESSION)


def test_a_stored_body_that_differs_from_the_one_sent_raises(gh):
    _, stored, _ = gh
    stored["mutate"] = lambda body: body.replace("\n", "\r\n")
    with pytest.raises(comment.CommentError, match="differs"):
        comment.post(PR, "a\nb", SESSION)


def test_a_failing_or_missing_gh_raises_loudly(gh, monkeypatch):
    def failing(args, **_kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="HTTP 404: Not Found")

    monkeypatch.setattr(comment.subprocess, "run", failing)
    with pytest.raises(comment.CommentError, match="HTTP 404"):
        comment.post(PR, "x", SESSION)

    def missing(args, **_kwargs):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(comment.subprocess, "run", missing)
    with pytest.raises(comment.CommentError, match="not installed"):
        comment.post(PR, "x", SESSION)
