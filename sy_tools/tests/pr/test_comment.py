"""`sy_tools.pr.comment.post`, with `gh` replaced at the subprocess seam so no test reaches a code host."""
from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from sy_tools.pr import comment

SESSION = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
PR = "https://git.example.test/acme/widgets/pull/42"
FAKE_SECRET = "sy-fake-token-3d91f7-not-a-credential"


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """Records each `gh` invocation; answers a GET with a pull request or review comment, a POST by echoing its body."""
    calls: list[dict] = []
    monkeypatch.setattr(comment.config, "resolved_root", lambda: tmp_path)
    monkeypatch.setattr(comment.config, "extra_secret_words", frozenset)
    pulls = "https://api.git.example.test/repos/acme/widgets/pulls/42"
    stored: dict[str, Any] = {"mutate": None, "in_reply_to_id": None, "pull_request_url": pulls}

    def run(args, *, cwd, input, **kwargs):
        if input is None:
            calls.append({"args": args, "stdin": kwargs.get("stdin"), "cwd": cwd})
            got = {"id": 42, "html_url": PR, "in_reply_to_id": stored["in_reply_to_id"],
                   "pull_request_url": stored["pull_request_url"]}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(got), stderr="")
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
    get, call = calls
    assert get["args"] == ["gh", "api", "--hostname", "git.example.test", "repos/acme/widgets/pulls/42"], get["args"]
    assert get["stdin"] is subprocess.DEVNULL, "a GET must never inherit the server's JSON-RPC stdin"
    assert call["args"] == [
        "gh", "api", "--hostname", "git.example.test", "--method", "POST",
        "repos/acme/widgets/issues/42/comments", "--input", "-",
    ], call["args"]
    assert call["cwd"] == root, "gh must run in the repository the configuration resolved against"
    assert call["body"] == f"Looks good.\n\n<!-- shipyard:agent session={SESSION} -->", call["body"]
    marker = comment.MARKER_RE.search(call["body"])
    assert marker is not None and marker["session"] == SESSION, "readers anchor on the last line"


def test_a_reply_to_a_comment_on_another_pull_request_is_refused_before_any_post(gh):
    calls, stored, _ = gh
    stored["pull_request_url"] = "https://api.git.example.test/repos/acme/widgets/pulls/41"
    with pytest.raises(comment.CommentError, match="not on pull request") as err:
        comment.post(PR, "x", SESSION, reply_to=991)
    assert not any("POST" in call["args"] for call in calls), calls
    assert "may already be posted" not in str(err.value), err.value


@pytest.mark.parametrize(("in_reply_to_id", "thread"), [(990, 990), (None, 991)])
def test_a_reply_goes_to_the_root_of_the_review_comment_thread(gh, in_reply_to_id, thread):
    calls, stored, _ = gh
    stored["in_reply_to_id"] = in_reply_to_id
    comment.post(PR + "/", "Fixed.", SESSION, reply_to=991)
    get, call = calls
    assert get["args"][4] == "repos/acme/widgets/pulls/comments/991", get["args"]
    assert call["args"][6] == f"repos/acme/widgets/pulls/42/comments/{thread}/replies", call["args"]


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
    answer_get = comment.subprocess.run

    def failing(args, **kwargs):
        if "POST" not in args:
            return answer_get(args, **kwargs)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="HTTP 404: Not Found")

    monkeypatch.setattr(comment.subprocess, "run", failing)
    with pytest.raises(comment.CommentError, match=r"HTTP 404.*may already be posted"):
        comment.post(PR, "x", SESSION)

    def slow(args, **kwargs):
        if "POST" not in args:
            return answer_get(args, **kwargs)
        raise subprocess.TimeoutExpired(args, 30)

    monkeypatch.setattr(comment.subprocess, "run", slow)
    with pytest.raises(comment.CommentError, match=r"killed.*may already be posted"):
        comment.post(PR, "x", SESSION)

    for stdout, shape in (("not json", "non-JSON"), ('{"body": "x"}', "id and html_url")):

        def unusable(args, _stdout=stdout, **kwargs):
            if "POST" not in args:
                return answer_get(args, **kwargs)
            return subprocess.CompletedProcess(args, 0, stdout=_stdout, stderr="")

        monkeypatch.setattr(comment.subprocess, "run", unusable)
        with pytest.raises(comment.CommentError, match=rf"{shape}.*may already be posted"):
            comment.post(PR, "x", SESSION)

    def missing(args, **_kwargs):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(comment.subprocess, "run", missing)
    with pytest.raises(comment.CommentError, match="not installed"):
        comment.post(PR, "x", SESSION)


def test_a_url_that_names_an_issue_is_refused_before_anything_is_posted(gh, monkeypatch):
    calls: list[list[str]] = []

    def not_a_pull(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh: Not Found (HTTP 404)")

    monkeypatch.setattr(comment.subprocess, "run", not_a_pull)
    with pytest.raises(comment.CommentError, match="HTTP 404") as raised:
        comment.post(PR, "x", SESSION)
    assert not any("POST" in args for args in calls), calls
    assert "may already be posted" not in str(raised.value), "nothing was posted, so the error must not say so"


def test_output_is_scrubbed_before_it_is_cut(gh, monkeypatch):
    monkeypatch.setenv("SY_TEST_FAKE_TOKEN", FAKE_SECRET)

    def garbled(args, **_kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="x" * 190 + FAKE_SECRET, stderr="")

    monkeypatch.setattr(comment.subprocess, "run", garbled)
    with pytest.raises(comment.CommentError, match="non-JSON") as raised:
        comment.post(PR, "x", SESSION)
    assert FAKE_SECRET[:10] not in str(raised.value), "a secret cut at the limit leaked its prefix"
