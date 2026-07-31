"""Offline tests for the Jira adapter and its in-process rich-text conversion: no network, no
real credential.

The credential test is the load-bearing one. The fake token is planted in the environment the
same way the real one lives there, and every surface the call produces — the returned dict, the
exception message, the URL, the request body, the non-auth headers, stdout — is asserted clean.
The `Authorization` header is the single place it is allowed to appear.

The transport tests go through a real `httpx2.MockTransport` rather than a stub of `request`, so
the adapter's own exception mapping and its own timeout are what get exercised.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx2
import pytest

from sy_tools.tracker import TIMEOUT_SECONDS, TrackerError
from sy_tools.tracker.jira import adapter, adf

FAKE_TOKEN = "ATATT3xFfGF0-fake-adapter-fixture-6b21d0e9a7c4"
FAKE_EMAIL = "shipyard-bot@example.com"
FAKE_SITE = "example.atlassian.net"
FAKE_PROJECT = "PROJ"
BASE = f"https://{FAKE_SITE}/rest/api/3"
MYSELF = f"{BASE}/myself"

COLUMNS = {
    "columns.backlog": "Created",
    "columns.ready": "Ready for Build",
    "columns.in_progress": "In Progress",
    "columns.in_review": "In Review",
    "columns.done": "Closed",
}
"""Column names that differ from the canonical tokens, so a test cannot pass by echoing the token."""


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """asyncio only: that is the loop the MCP server runs the adapter on."""
    return "asyncio"


@pytest.fixture
def credentials(monkeypatch) -> None:
    """Resolved identifiers and the planted credential, exactly where each really lives."""
    values = {
        "tracker_config.email": FAKE_EMAIL,
        "tracker_config.site": FAKE_SITE,
        "tracker_config.project": FAKE_PROJECT,
        **COLUMNS,
    }
    monkeypatch.setattr(adapter.config, "get", lambda path, *, default=None: values.get(path, default))
    monkeypatch.setenv(adapter.TOKEN_ENV, FAKE_TOKEN)


@pytest.fixture
def artifact(tmp_path) -> Path:
    path = tmp_path / "PROJ-1-ship-transcript.txt"
    path.write_bytes(b"transcript body\n")
    return path


def _transport(monkeypatch, *responses: object) -> list[dict]:
    """Replace the module's request helper with a recorder answering `responses` in order.

    A single response answers every call, which is all a one-call verb needs; several are handed out
    in order and the last one repeats, which is what the read-write-verify verbs need. A response
    given as a `(status, body)` pair sets the status too — Jira answers most writes 204 with no body,
    and a verb that asserts that status has to be able to see it.
    """
    calls: list[dict] = []
    queue = list(responses) or [None]

    async def fake_request(method, url, auth, data=None, headers=None, *, transport=None):
        calls.append({"method": method, "url": url, "auth": auth, "data": data, "headers": headers or {}})
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return answer if isinstance(answer, tuple) else (200, answer)

    monkeypatch.setattr(adapter, "request", fake_request)
    return calls


def _sent(call: dict) -> dict:
    """The JSON body a recorded call actually put on the wire."""
    assert call["headers"].get("Content-Type") == "application/json", f"a JSON write needs the header: {call}"
    return json.loads(call["data"])


@pytest.mark.anyio
async def test_attach_artifact_posts_a_verified_multipart_upload(credentials, artifact, monkeypatch, capsys):
    echoed = [{"id": "10501", "filename": artifact.name, "size": 16, "created": "2026-07-30T00:00:00.000+0000"}]
    calls = _transport(monkeypatch, echoed)

    evidence = await adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)

    assert len(calls) == 1, f"one upload call expected, got {calls}"
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"https://{FAKE_SITE}/rest/api/3/issue/PROJ-1/attachments", call["url"]
    assert call["headers"]["X-Atlassian-Token"] == "no-check", "Jira rejects the upload without the XSRF opt-out"
    content_type = call["headers"]["Content-Type"]
    boundary = content_type.partition("boundary=")[2]
    assert content_type.startswith("multipart/form-data; boundary=") and boundary, content_type
    body = call["data"]
    assert body.startswith(f"--{boundary}\r\n".encode()), body[:80]
    assert f'filename="{artifact.name}"'.encode() in body
    assert b"\r\n\r\ntranscript body\n\r\n" in body, "the file bytes must follow the blank line verbatim"
    assert body.endswith(f"--{boundary}--\r\n".encode()), body[-80:]
    assert evidence == {
        "issue": "PROJ-1", "filename": artifact.name, "id": "10501", "size": 16,
        "created": "2026-07-30T00:00:00.000+0000",
    }
    assert capsys.readouterr().out == "", "nothing in the adapter may write to stdout: it carries JSON-RPC frames"


@pytest.mark.anyio
async def test_a_response_that_does_not_confirm_the_filename_fails(credentials, artifact, monkeypatch):
    _transport(monkeypatch, [{"id": "9", "filename": "something-else.txt"}])
    with pytest.raises(TrackerError, match="did not confirm"):
        await adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)


@pytest.mark.anyio
async def test_a_confirmation_without_an_id_is_not_reported_as_attached(credentials, artifact, monkeypatch):
    """An id-less echo leaves nothing to link to later, so it is a failed upload, not an empty id."""
    _transport(monkeypatch, [{"filename": artifact.name, "size": 16}])
    with pytest.raises(TrackerError, match="without an attachment id"):
        await adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)


@pytest.mark.anyio
@pytest.mark.parametrize("name", ['quote".txt', "carriage\rreturn.txt", "new\nline.txt"], ids=["quote", "cr", "lf"])
async def test_a_filename_that_could_forge_multipart_headers_is_refused(credentials, monkeypatch, tmp_path, name):
    """The multipart header is hand-built, so these three characters are header injection, not names.

    All are legal in a POSIX filename: a quote closes `filename="..."` early and a CR or LF starts a
    header line — or a whole extra part — that the caller never asked to send.
    """
    hostile = tmp_path / name
    hostile.write_bytes(b"payload")
    calls = _transport(monkeypatch, [{"id": "1", "filename": name}])

    with pytest.raises(TrackerError, match="quote, carriage return or newline"):
        await adapter.JiraAdapter().attach_artifact("PROJ-1", hostile)
    assert calls == [], "the name must be refused before anything is put on the wire"


@pytest.mark.anyio
async def test_the_credential_appears_only_in_the_authorization_header(credentials, artifact, monkeypatch, capsys):
    calls = _transport(monkeypatch, [{"id": "10501", "filename": artifact.name}])
    evidence = await adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)
    call = calls[0]

    assert FAKE_TOKEN not in json.dumps(evidence), "the returned evidence must never carry the credential"
    argv_shaped = [call["method"], call["url"], *(f"{k}: {v}" for k, v in call["headers"].items())]
    assert not any(FAKE_TOKEN in part for part in argv_shaped), "the credential must never be argv-visible"
    assert FAKE_TOKEN.encode() not in call["data"], "the credential must never ride in the request body"
    assert capsys.readouterr().out == "", "the credential must never reach stdout"

    assert call["auth"].startswith("Basic "), call["auth"]
    decoded = base64.b64decode(call["auth"].removeprefix("Basic ")).decode()
    assert decoded == f"{FAKE_EMAIL}:{FAKE_TOKEN}", "the header is the one legitimate carrier"

    _transport(monkeypatch, [{"id": "9", "filename": "other.txt"}])
    with pytest.raises(TrackerError) as failure:
        await adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)
    assert FAKE_TOKEN not in str(failure.value), "a failure message must not leak the credential either"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raised",
    [
        httpx2.ReadTimeout("timed out reading the response"),
        httpx2.ConnectError("connection refused"),
    ],
    ids=["read-stall", "connect-stall"],
)
async def test_a_stalled_call_is_bounded_and_becomes_a_tracker_error(raised):
    """No REST call may hang unbounded, and no transport failure may escape as an httpx2 exception.

    Both families are checked because `TimeoutException` is a subclass of `RequestError`: a mapping
    that caught the family first would still pass a timeout-only test. The bound is read off the
    request the client actually handed the transport, not off the constant.
    """
    bounds: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bounds.append(dict(request.extensions["timeout"]))
        raise raised

    with pytest.raises(TrackerError) as failure:
        await adapter.request("GET", MYSELF, "Basic x", transport=httpx2.MockTransport(handler))

    expected = {"connect": TIMEOUT_SECONDS, "read": TIMEOUT_SECONDS, "write": TIMEOUT_SECONDS, "pool": TIMEOUT_SECONDS}
    assert bounds == [expected], f"every phase of the call must be bounded, got {bounds}"
    assert FAKE_SITE in str(failure.value), "the failure must name the call that stalled"


@pytest.mark.anyio
async def test_a_non_json_2xx_body_becomes_a_tracker_error():
    """A proxy or SSO wall can answer 200 with HTML; that must map, not escape as a JSONDecodeError."""
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"<html>sign in</html>")

    with pytest.raises(TrackerError, match="not JSON"):
        await adapter.request("GET", MYSELF, "Basic x", transport=httpx2.MockTransport(handler))


@pytest.mark.anyio
async def test_a_rejected_call_becomes_a_tracker_error_naming_the_status():
    """A non-2xx is a failure, reported with its status and Jira's own detail — and no credential."""
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, json={"errorMessages": ["You do not have permission to attach files"]})

    auth = "Basic " + base64.b64encode(f"{FAKE_EMAIL}:{FAKE_TOKEN}".encode()).decode()
    with pytest.raises(TrackerError) as failure:
        await adapter.request("POST", MYSELF, auth, b"body", transport=httpx2.MockTransport(handler))

    message = str(failure.value)
    assert "HTTP 403" in message, message
    assert "You do not have permission" in message, "Jira's own detail is what makes the failure actionable"
    assert FAKE_TOKEN not in message, "a rejection message must not leak the credential it was rejected with"


@pytest.mark.anyio
async def test_missing_inputs_name_what_is_missing(credentials, artifact, monkeypatch):
    _transport(monkeypatch, [{"id": "1", "filename": artifact.name}])
    monkeypatch.delenv(adapter.TOKEN_ENV)
    with pytest.raises(TrackerError) as missing_token:
        await adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)
    assert adapter.TOKEN_ENV in str(missing_token.value)
    assert "config" in str(missing_token.value), "the message must say where the secret does not belong"

    monkeypatch.setenv(adapter.TOKEN_ENV, FAKE_TOKEN)
    monkeypatch.setattr(adapter.config, "get", lambda path, *, default=None: default)
    with pytest.raises(TrackerError, match=r"tracker_config\.email"):
        await adapter.JiraAdapter().preflight()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "site",
    [f"someone:s3cret@{FAKE_SITE}", f"https://someone:s3cret@{FAKE_SITE}"],
    ids=["bare", "with-scheme"],
)
async def test_a_site_carrying_userinfo_is_refused_without_echoing_it(monkeypatch, artifact, site):
    """A `user:pass@host` site would ride in every request URL, every browse link and every failure.

    Refused where the site is first turned into a base URL, so no verb can build one — and the
    rejection must not quote the value, which would put the embedded secret in the message instead.
    """
    values = {"tracker_config.email": FAKE_EMAIL, "tracker_config.site": site, "tracker_config.project": FAKE_PROJECT}
    monkeypatch.setattr(adapter.config, "get", lambda path, *, default=None: values.get(path, default))
    monkeypatch.setenv(adapter.TOKEN_ENV, FAKE_TOKEN)
    calls = _transport(monkeypatch, MYSELF_BODY)

    with pytest.raises(TrackerError) as failure:
        await adapter.JiraAdapter().preflight()

    message = str(failure.value)
    assert "user:password@" in message, f"the failure must say what is wrong with the site: {message}"
    assert "s3cret" not in message, "the rejection must not repeat the credential embedded in the site"
    assert calls == [], "a site that cannot be trusted in a URL must fail before any call is made"


# ---- the canonical verbs -------------------------------------------------------------------------
#
# Every verb is driven through the recorder above, so what is asserted is the request that would have
# gone to Jira — method, URL and body — and the dict the verb hands back. The response fixtures are
# the shapes the real API returns (measured against a live instance), trimmed to the fields the
# adapter reads.

ADF_BODY = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
    {"type": "text", "text": "Body line."}]}]}

ISSUE = {
    "id": "10001",
    "key": "PROJ-7",
    "fields": {
        "summary": "Ship the thing",
        "description": ADF_BODY,
        "status": {"name": "In Review"},
        "issuetype": {"name": "Task"},
        "parent": {"key": "PROJ-1", "fields": {"summary": "The epic"}},
        "subtasks": [{"key": "PROJ-8"}, {"key": "PROJ-9"}],
        # Read semantics measured live: a counterpart under `outwardIssue` blocks this issue, one
        # under `inwardIssue` is blocked BY it. So PROJ-5 is the blocker and PROJ-6 is downstream.
        "issuelinks": [
            {"type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-5"}},
            {"type": {"name": "Blocks"}, "inwardIssue": {"key": "PROJ-6"}},
            {"type": {"name": "Relates"}, "outwardIssue": {"key": "PROJ-4"}},
        ],
        "labels": ["decomposed", "shipyard"],
    },
}
"""One issue as `GET /issue/{id}?fields=...` returns it: `PROJ-5` blocks it, it blocks `PROJ-6`."""

THREAD = {
    "comments": [{
        "id": "20001", "author": {"displayName": "Ship Bot"},
        "created": "2026-07-30T00:00:00.000+0000", "body": ADF_BODY,
    }],
    "maxResults": 50, "startAt": 0, "total": 1,
}

SEARCH_PAGE = {
    "isLast": False,
    "nextPageToken": "eyJzdGFydEF0Ijo1MH0",
    "issues": [{
        "id": "10001", "key": "PROJ-7",
        "fields": {k: ISSUE["fields"][k] for k in ("summary", "status", "issuetype", "parent", "labels")},
    }],
}
"""A `POST /search/jql` page: no `total` and no `startAt` exist on this endpoint, so nothing may read them."""

TRANSITIONS = {"transitions": [
    {"id": "31", "name": "Start work", "to": {"name": "in progress"}},
    {"id": "6", "name": "In Progress", "to": {"name": "In Review"}},
]}
"""Deliberately adversarial: transition 6 is *named* for the column transition 31 actually reaches."""

STATUS_READ = {"fields": {"status": {"name": "In Progress"}}}
LABELS_READ = {"fields": {"labels": ["decomposed"]}}
LINK_CONFIRMED = {"fields": {"issuelinks": [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "PROJ-7"}}]}}
"""Read back from the BLOCKER: the issue it blocks arrives as the link's inward end."""
MYSELF_BODY = {"accountId": "5f8a1c2d3e4f", "displayName": "Ship Bot"}


@pytest.mark.anyio
@pytest.mark.parametrize("parent", [None, "PROJ-1"], ids=["standalone", "child"])
async def test_create_issue_posts_a_typed_issue_without_a_reporter(credentials, monkeypatch, parent):
    """`reporter` is required by createmeta yet must not be sent: Jira defaults it to this account."""
    calls = _transport(monkeypatch, (201, {"id": "10001", "key": "PROJ-7"}))

    created = await adapter.JiraAdapter().create_issue("task", "Ship the thing", "Body line.", parent)

    assert [(c["method"], c["url"]) for c in calls] == [("POST", f"{BASE}/issue")], calls
    fields = _sent(calls[0])["fields"]
    assert "reporter" not in fields, "sending reporter lets a shared config decide who reported an issue"
    assert fields["project"] == {"key": FAKE_PROJECT}
    assert fields["issuetype"] == {"name": "Task"}, f"the canonical token must map to a native type: {fields}"
    assert fields["summary"] == "Ship the thing"
    assert fields["description"]["type"] == "doc", f"the body must be sent as rich text: {fields['description']}"
    assert fields.get("parent") == ({"key": parent} if parent else None), f"parent sent wrongly: {fields}"
    assert created == {
        "id": "PROJ-7", "url": f"https://{FAKE_SITE}/browse/PROJ-7", "type": "task",
        "title": "Ship the thing", "parent": parent,
    }


@pytest.mark.anyio
async def test_create_issue_refuses_an_unknown_type_before_it_calls(credentials, monkeypatch):
    calls = _transport(monkeypatch, (201, {"key": "PROJ-7"}))
    with pytest.raises(TrackerError, match="unknown canonical type"):
        await adapter.JiraAdapter().create_issue("story", "Ship the thing")
    assert calls == [], "an unmappable type must fail before anything is created"


@pytest.mark.anyio
async def test_a_create_that_returns_no_key_is_not_reported_as_created(credentials, monkeypatch):
    _transport(monkeypatch, (201, {"self": "https://example/rest/api/3/issue/10001"}))
    with pytest.raises(TrackerError, match="no issue key"):
        await adapter.JiraAdapter().create_issue("task", "Ship the thing")


@pytest.mark.anyio
async def test_get_issue_reads_canonical_fields_markdown_and_only_the_blockers(credentials, monkeypatch):
    """`dependencies` is what blocks this issue: the counterpart arriving as the link's OUTWARD end.

    The direction was measured against real linked issues, not inferred — a read carries only the
    counterpart, and the field it arrives under names that counterpart's absolute role, so the
    blocker of this issue is its outward end. The fixture carries one link each way plus an
    unrelated type, because reporting the wrong end inverts every dependency downstream reasoning
    then depends on, while every call still succeeds.
    """
    calls = _transport(monkeypatch, ISSUE, THREAD)

    full = await adapter.JiraAdapter().get_issue("PROJ-7")

    assert [c["method"] for c in calls] == ["GET", "GET"], calls
    assert calls[0]["url"].startswith(f"{BASE}/issue/PROJ-7?fields="), calls[0]["url"]
    assert "*all" not in calls[0]["url"], "a read must name its fields, not pull every custom field"
    assert calls[1]["url"].startswith(f"{BASE}/issue/PROJ-7/comment?maxResults="), calls[1]["url"]
    assert set(full) == {
        "id", "title", "body", "status", "type", "parent", "children", "labels", "dependencies", "url", "comments",
        "comments_truncated",
    }, f"the return shape is a frozen cross-adapter contract: {sorted(full)}"
    assert full["comments_truncated"] is False, "a thread Jira reports as complete must not read as clipped"
    assert (full["status"], full["type"]) == ("in-review", "task"), f"natives were not canonicalised: {full}"
    assert full["parent"] == "PROJ-1", f"the parent key was not extracted: {full['parent']}"
    assert full["children"] == ["PROJ-8", "PROJ-9"]
    assert full["dependencies"] == ["PROJ-5"], (
        f"dependencies must be this issue's blockers only, got {full['dependencies']}: PROJ-6 is blocked BY this "
        "issue and PROJ-4 is not a Blocks link at all"
    )
    assert full["labels"] == ["decomposed", "shipyard"]
    assert full["body"].strip() == "Body line.", f"the rich-text body was not converted: {full['body']!r}"
    comment = full["comments"][0]
    assert len(full["comments"]) == 1 and {k: comment[k] for k in ("id", "author", "created")} == {
        "id": "20001", "author": "Ship Bot", "created": "2026-07-30T00:00:00.000+0000",
    }, f"the comment shape is part of the frozen contract: {full['comments']}"
    assert comment["body"].strip() == "Body line.", "comment bodies cross this seam as Markdown"


@pytest.mark.anyio
async def test_get_issue_survives_an_issue_with_no_body_no_children_and_no_comments(credentials, monkeypatch):
    """Jira returns `description: null` for an empty body; that is an empty string, not a failure."""
    bare = {"fields": {
        "summary": "Bare", "description": None, "status": {"name": "Backlog"}, "issuetype": {"name": "Bug"},
    }}
    _transport(monkeypatch, bare, {"comments": []})

    full = await adapter.JiraAdapter().get_issue("PROJ-7")

    assert full["body"] == "", f"a missing description must read as empty text: {full['body']!r}"
    assert (full["parent"], full["children"], full["dependencies"], full["labels"]) == (None, [], [], [])
    assert full["comments"] == []
    assert full["status"] == "Backlog", "a column this repo does not map must pass through, not vanish"
    assert full["type"] == "bug"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("thread", "truncated"),
    [
        (THREAD, False),
        ({**THREAD, "total": 120}, True),
        ({"comments": [{"id": str(n)} for n in range(adapter.COMMENT_PAGE)]}, True),
    ],
    ids=["complete", "counted-short", "full-page-with-no-total"],
)
async def test_get_issue_says_whether_the_comment_page_left_anything_out(
    credentials, monkeypatch, thread, truncated
):
    """A clipped thread reads exactly like a quiet issue, so the bound has to be visible.

    Both signals Jira offers are covered: the counted case, where `startAt` plus the page is short of
    `total`, and the case where `total` is missing entirely and a page that came back full is all
    there is to go on.
    """
    _transport(monkeypatch, ISSUE, thread)

    full = await adapter.JiraAdapter().get_issue("PROJ-7")

    assert full["comments_truncated"] is truncated, (
        f"a page of {len(thread['comments'])} against total={thread.get('total')!r} must read as "
        f"truncated={truncated}: {full['comments_truncated']}"
    )
    assert len(full["comments"]) == len(thread["comments"]), "every comment on the page must still be returned"


@pytest.mark.anyio
async def test_a_read_whose_shape_is_wrong_fails_without_quoting_the_credential(credentials, monkeypatch):
    _transport(monkeypatch, {"errorMessages": ["not found"]})
    with pytest.raises(TrackerError) as failure:
        await adapter.JiraAdapter().get_issue("PROJ-7")
    assert "no fields block" in str(failure.value), failure.value
    assert FAKE_TOKEN not in str(failure.value), "a shape failure must not leak the credential"


@pytest.mark.anyio
async def test_update_issue_writes_a_converted_description(credentials, monkeypatch):
    calls = _transport(monkeypatch, (204, None))

    result = await adapter.JiraAdapter().update_issue("PROJ-7", "- alpha\n- beta\n")

    assert (calls[0]["method"], calls[0]["url"]) == ("PUT", f"{BASE}/issue/PROJ-7"), calls
    description = _sent(calls[0])["fields"]["description"]
    assert [n["type"] for n in description["content"]] == ["bulletList"], f"the list was dropped: {description}"
    assert result == {"id": "PROJ-7", "updated": True, "url": f"https://{FAKE_SITE}/browse/PROJ-7"}


@pytest.mark.anyio
async def test_find_issues_searches_the_supported_endpoint_with_scoped_jql(credentials, monkeypatch):
    """`GET /search` is 410 Gone, and `/search/jql` reports no total — only `isLast` plus a token."""
    calls = _transport(monkeypatch, SEARCH_PAGE)

    page = await adapter.JiraAdapter().find_issues(status="in-review", issue_type="task", parent="PROJ-1", limit=5)

    assert (calls[0]["method"], calls[0]["url"]) == ("POST", f"{BASE}/search/jql"), calls
    body = _sent(calls[0])
    assert body["jql"] == 'project = "PROJ" AND status = "In Review" AND issuetype = "Task" AND parent = "PROJ-1"', (
        f"filters must be native and project-scoped: {body['jql']!r}"
    )
    assert body["maxResults"] == 5 and body["fields"] == list(adapter.SUMMARY_FIELDS), body
    assert page == {
        "issues": [{
            "id": "PROJ-7",
            "title": "Ship the thing",
            "status": "in-review",
            "type": "task",
            "parent": "PROJ-1",
            "labels": ["decomposed", "shipyard"],
            "url": f"https://{FAKE_SITE}/browse/PROJ-7",
        }],
        "count": 1,
        "is_last": False,
        "next_page_token": "eyJzdGFydEF0Ijo1MH0",
    }, f"the page must carry the canonicalised issues plus paging from nextPageToken: {page}"


@pytest.mark.anyio
async def test_find_issues_reads_exhaustion_from_the_token_not_from_isLast(credentials, monkeypatch):
    """`/search/jql` does not guarantee `isLast`, so a page with a next token is never the last one.

    Treating an absent `isLast` as "done" silently reports one page as the whole result set, which is
    exactly how a decomposition ends up planned against a truncated board.
    """
    _transport(monkeypatch, {"issues": [], "nextPageToken": "eyJzdGFydEF0Ijo1MH0"})
    page = await adapter.JiraAdapter().find_issues()
    assert page["is_last"] is False, f"a page carrying a next token is not the last page: {page}"

    _transport(monkeypatch, {"issues": []})
    exhausted = await adapter.JiraAdapter().find_issues()
    assert exhausted["is_last"] is True and exhausted["next_page_token"] is None, exhausted


@pytest.mark.anyio
async def test_find_issues_quotes_a_value_that_could_close_its_own_clause(credentials, monkeypatch):
    calls = _transport(monkeypatch, {"issues": [], "isLast": True})

    await adapter.JiraAdapter().find_issues(text='he said "ship it" OR project = OTHER')

    jql = _sent(calls[0])["jql"]
    assert jql == 'project = "PROJ" AND text ~ "he said \\"ship it\\" OR project = OTHER"', jql
    assert jql.count('project = "PROJ"') == 1, f"an unescaped quote widened the search: {jql!r}"


@pytest.mark.anyio
async def test_find_issues_bounds_the_page_size(credentials, monkeypatch):
    calls = _transport(monkeypatch, {"issues": [], "isLast": True})
    await adapter.JiraAdapter().find_issues(limit=100_000)
    assert _sent(calls[0])["maxResults"] == adapter.RESULT_CEILING, "an unbounded page size must be clamped"

    with pytest.raises(TrackerError, match="positive"):
        await adapter.JiraAdapter().find_issues(limit=0)
    assert len(calls) == 1, "a nonsensical limit must fail before the search runs"


@pytest.mark.anyio
async def test_a_search_item_agrees_with_the_full_read_of_the_same_issue(credentials, monkeypatch):
    """The two verbs must describe one issue identically; pinned here rather than left to inspection."""
    _transport(monkeypatch, SEARCH_PAGE)
    item = (await adapter.JiraAdapter().find_issues())["issues"][0]
    _transport(monkeypatch, ISSUE, THREAD)
    full = await adapter.JiraAdapter().get_issue("PROJ-7")

    assert set(item) < set(full), f"a search item's keys must be a subset of a read's: {sorted(item)}"
    assert set(item) == {"id", "title", "status", "type", "parent", "labels", "url"}, sorted(item)
    differing = {k: (item[k], full[k]) for k in item if item[k] != full[k]}
    assert not differing, f"the two verbs disagree about the same issue: {differing}"


@pytest.mark.anyio
async def test_set_status_matches_the_transition_target_not_the_transition_name(credentials, monkeypatch):
    """`to.name` is the column a transition reaches; the transition's own name is decoration."""
    calls = _transport(monkeypatch, TRANSITIONS, (204, None), STATUS_READ)

    result = await adapter.JiraAdapter().set_status("PROJ-7", "in-progress")

    assert [(c["method"], c["url"]) for c in calls] == [
        ("GET", f"{BASE}/issue/PROJ-7/transitions"),
        ("POST", f"{BASE}/issue/PROJ-7/transitions"),
        ("GET", f"{BASE}/issue/PROJ-7?fields=status"),
    ], calls
    assert _sent(calls[1]) == {"transition": {"id": "31"}}, (
        f"matched the wrong transition: {_sent(calls[1])} — 6 is merely NAMED 'In Progress', 31 reaches it"
    )
    assert result == {"id": "PROJ-7", "status": "in-progress", "native": "In Progress"}


@pytest.mark.anyio
async def test_set_status_fails_loudly_listing_what_is_reachable(credentials, monkeypatch):
    calls = _transport(monkeypatch, TRANSITIONS)
    with pytest.raises(TrackerError) as failure:
        await adapter.JiraAdapter().set_status("PROJ-7", "done")
    assert "Closed" in str(failure.value), "the failure must name the column that was asked for"
    assert "In Review" in str(failure.value), f"the reachable targets are the actionable part: {failure.value}"
    assert len(calls) == 1, "an unreachable target must not be attempted blind"


@pytest.mark.anyio
async def test_set_status_fails_when_the_issue_did_not_actually_move(credentials, monkeypatch):
    """A workflow can accept the POST and leave the issue where it was; that is not a success."""
    _transport(monkeypatch, TRANSITIONS, (204, None), {"fields": {"status": {"name": "In Review"}}})
    with pytest.raises(TrackerError, match="treat the move as failed"):
        await adapter.JiraAdapter().set_status("PROJ-7", "in-progress")


@pytest.mark.anyio
async def test_assign_reads_the_account_once_and_reuses_it(credentials, monkeypatch):
    calls = _transport(monkeypatch, MYSELF_BODY, (204, None))
    jira = adapter.JiraAdapter()

    first = await jira.assign("PROJ-7")
    second = await jira.assign("PROJ-8")

    assert [c["url"] for c in calls if c["url"] == MYSELF] == [MYSELF], (
        f"the account id must be cached per instance, got {[c['url'] for c in calls]}"
    )
    assert [(c["method"], c["url"]) for c in calls[1:]] == [
        ("PUT", f"{BASE}/issue/PROJ-7/assignee"), ("PUT", f"{BASE}/issue/PROJ-8/assignee")
    ], calls
    assert _sent(calls[1]) == {"accountId": MYSELF_BODY["accountId"]}
    assert first == {"id": "PROJ-7", "assignee": MYSELF_BODY["accountId"]}
    assert second["id"] == "PROJ-8"


@pytest.mark.anyio
async def test_assign_refuses_anyone_but_the_authenticated_account(credentials, monkeypatch):
    """Silently self-assigning instead would land the work on the wrong person's board."""
    calls = _transport(monkeypatch, MYSELF_BODY)
    with pytest.raises(TrackerError, match="only self-assignment"):
        await adapter.JiraAdapter().assign("PROJ-7", "someone.else@example.com")
    assert calls == [], "an unsupported assignee must fail before anything is written"


@pytest.mark.anyio
async def test_link_parent_writes_the_parent_field(credentials, monkeypatch):
    calls = _transport(monkeypatch, (204, None))
    result = await adapter.JiraAdapter().link_parent("PROJ-7", "PROJ-1")
    assert (calls[0]["method"], calls[0]["url"]) == ("PUT", f"{BASE}/issue/PROJ-7"), calls
    assert _sent(calls[0]) == {"fields": {"parent": {"key": "PROJ-1"}}}
    assert result == {"id": "PROJ-7", "parent": "PROJ-1"}


@pytest.mark.anyio
async def test_add_dependency_posts_the_blocker_outward_and_verifies_the_direction(credentials, monkeypatch):
    calls = _transport(monkeypatch, (201, None), LINK_CONFIRMED)

    result = await adapter.JiraAdapter().add_dependency("PROJ-7", "PROJ-5")

    assert (calls[0]["method"], calls[0]["url"]) == ("POST", f"{BASE}/issueLink"), calls
    assert _sent(calls[0]) == {
        "type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-5"}, "inwardIssue": {"key": "PROJ-7"},
    }, f"the blocker is the outward end in Jira's model: {_sent(calls[0])}"
    assert calls[1]["url"] == f"{BASE}/issue/PROJ-5?fields=issuelinks", "the direction is verified from the blocker"
    assert result == {"id": "PROJ-7", "blocked_by": "PROJ-5", "verified": True}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "verification",
    [
        {"fields": {"issuelinks": []}},
        {"fields": {"issuelinks": [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-7"}}]}},
        {"fields": {"issuelinks": [{"type": {"name": "Relates"}, "inwardIssue": {"key": "PROJ-7"}}]}},
    ],
    ids=["no-link", "reversed-link", "wrong-type"],
)
async def test_add_dependency_fails_when_the_direction_is_not_confirmed(credentials, monkeypatch, verification):
    """A reversed link reads as plausible forever after, so an unconfirmed one is a failure."""
    _transport(monkeypatch, (201, None), verification)
    with pytest.raises(TrackerError, match="direction not confirmed"):
        await adapter.JiraAdapter().add_dependency("PROJ-7", "PROJ-5")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "expected"),
    [("shipyard", ["decomposed", "shipyard"]), ("decomposed", ["decomposed"])],
    ids=["new-label", "already-present"],
)
async def test_add_label_writes_back_the_union_of_the_existing_labels(credentials, monkeypatch, label, expected):
    calls = _transport(monkeypatch, LABELS_READ, (204, None))

    result = await adapter.JiraAdapter().add_label("PROJ-7", label)

    assert calls[0]["url"] == f"{BASE}/issue/PROJ-7?fields=labels", calls[0]["url"]
    assert _sent(calls[1]) == {"fields": {"labels": expected}}, (
        f"Jira replaces the whole field, so a write that is not the union deletes labels: {_sent(calls[1])}"
    )
    assert result == {"id": "PROJ-7", "labels": expected}


@pytest.mark.anyio
async def test_add_label_refuses_to_write_back_a_labels_field_it_cannot_read(credentials, monkeypatch):
    _transport(monkeypatch, {"fields": {"labels": [{"name": "decomposed"}]}}, (204, None))
    with pytest.raises(TrackerError, match="not a list of strings"):
        await adapter.JiraAdapter().add_label("PROJ-7", "shipyard")


@pytest.mark.anyio
async def test_post_comment_sends_lists_and_code_as_rich_text_nodes(credentials, monkeypatch):
    """The named verification obligation: the node classes a client-side parser drops must survive."""
    calls = _transport(monkeypatch, (201, {"id": "20001"}))

    result = await adapter.JiraAdapter().post_comment("PROJ-7", RICH_TEXT)

    assert (calls[0]["method"], calls[0]["url"]) == ("POST", f"{BASE}/issue/PROJ-7/comment"), calls
    types = [node.get("type") for node in _sent(calls[0])["body"]["content"]]
    assert {"bulletList", "codeBlock"} <= set(types), f"nodes dropped on the way to Jira: {types}"
    assert result == {
        "id": "PROJ-7", "comment_id": "20001",
        "url": f"https://{FAKE_SITE}/browse/PROJ-7?focusedCommentId=20001",
    }


@pytest.mark.anyio
async def test_a_comment_that_returns_no_id_is_not_reported_as_posted(credentials, monkeypatch):
    _transport(monkeypatch, (201, {"self": "https://example/comment"}))
    with pytest.raises(TrackerError, match="no id"):
        await adapter.JiraAdapter().post_comment("PROJ-7", "log")


VERB_CALLS = [
    ("create_issue", lambda a: a.create_issue("task", "T", "body"), [(201, {"key": "PROJ-7"})]),
    ("get_issue", lambda a: a.get_issue("PROJ-7"), [ISSUE, THREAD]),
    ("update_issue", lambda a: a.update_issue("PROJ-7", "body"), [(204, None)]),
    ("find_issues", lambda a: a.find_issues(status="done"), [SEARCH_PAGE]),
    ("set_status", lambda a: a.set_status("PROJ-7", "in-progress"), [TRANSITIONS, (204, None), STATUS_READ]),
    ("assign", lambda a: a.assign("PROJ-7"), [MYSELF_BODY, (204, None)]),
    ("link_parent", lambda a: a.link_parent("PROJ-7", "PROJ-1"), [(204, None)]),
    ("add_dependency", lambda a: a.add_dependency("PROJ-7", "PROJ-5"), [(201, None), LINK_CONFIRMED]),
    ("add_label", lambda a: a.add_label("PROJ-7", "shipyard"), [LABELS_READ, (204, None)]),
    ("post_comment", lambda a: a.post_comment("PROJ-7", "log"), [(201, {"id": "20001"})]),
]
"""Every canonical verb with just enough canned responses to complete, for the whole-surface sweeps."""


@pytest.mark.anyio
@pytest.mark.parametrize(("verb", "call", "responses"), VERB_CALLS, ids=[v[0] for v in VERB_CALLS])
async def test_no_verb_leaks_the_credential_or_writes_to_stdout(
    credentials, monkeypatch, capsys, verb, call, responses
):
    calls = _transport(monkeypatch, *responses)

    result = await call(adapter.JiraAdapter())

    assert FAKE_TOKEN not in json.dumps(result), f"{verb} handed the credential back to its caller"
    assert capsys.readouterr().out == "", f"{verb} wrote to stdout, which carries JSON-RPC frames"
    for one in calls:
        assert FAKE_TOKEN not in one["url"], f"{verb} put the credential in a URL"
        assert FAKE_TOKEN not in json.dumps(one["headers"]), f"{verb} put the credential in a non-auth header"
        assert one["data"] is None or FAKE_TOKEN.encode() not in one["data"], f"{verb} put the credential in a body"
        assert one["auth"].startswith("Basic "), f"{verb} did not authenticate through the header: {one['auth']}"


# ---- in-process rich-text conversion ------------------------------------------------------------
#
# The round-trip test is the load-bearing one. Lists and code blocks are exactly the node classes a
# client-side Markdown parser is known to drop silently (atlassian/homebrew-acli#45), so it asserts
# they exist as document nodes going out and that their content is still there coming back, rather
# than asserting on the whole document — the converter's own cosmetic spacing choices are not a
# contract, the surviving content is.

RICH_TEXT = """Intro paragraph.

- alpha
- beta

1. first
2. second

```python
def f(x):
    return x + 1
```
"""

CODE_BODY = "def f(x):\n    return x + 1"


def test_lists_and_code_blocks_survive_the_round_trip(capsys):
    doc = adf.markdown_to_adf(RICH_TEXT)

    types = [node.get("type") for node in doc["content"]]
    assert {"bulletList", "orderedList", "codeBlock"} <= set(types), f"nodes dropped on the way out: {types}"
    code = next(node for node in doc["content"] if node["type"] == "codeBlock")
    assert code["attrs"]["language"] == "python", f"the code block lost its language: {code.get('attrs')}"
    assert code["content"][0]["text"] == CODE_BODY, f"the code block body was altered: {code['content'][0]['text']!r}"

    markdown = adf.adf_to_markdown(doc)
    for item in ("alpha", "beta", "first", "second"):
        assert item in markdown, f"list item {item!r} was dropped coming back: {markdown!r}"
    assert CODE_BODY in markdown, f"the code block body was dropped coming back: {markdown!r}"
    assert "adf=" not in markdown, "the read path must not leak tracker-native markup into Markdown"
    assert capsys.readouterr().out == "", "nothing here may write to stdout: it carries JSON-RPC frames"


def test_a_second_pass_changes_nothing():
    """Comments get read, edited and written back, so conversion must reach a fixed point."""
    once = adf.adf_to_markdown(adf.markdown_to_adf(RICH_TEXT))
    twice = adf.adf_to_markdown(adf.markdown_to_adf(once))
    assert once == twice, f"conversion is not idempotent:\n{once!r}\n{twice!r}"


def test_empty_markdown_becomes_a_valid_empty_document():
    """Optional bodies pass straight through, so empty input is a document, not a failure."""
    for text in ("", "   \n\n"):
        assert adf.markdown_to_adf(text) == {"type": "doc", "version": 1, "content": []}, repr(text)


@pytest.mark.parametrize(
    "returned",
    ['{"type": "doc"}', {"type": "paragraph", "version": 1, "content": []}, {"type": "doc", "version": 1}],
    ids=["json-string", "not-a-doc-node", "no-content-list"],
)
def test_an_ill_shaped_conversion_is_refused(monkeypatch, returned):
    """The guard is only reachable by faking the converter, and it is what stops bad writes."""
    monkeypatch.setattr(adf, "to_adf", lambda md: returned)
    with pytest.raises(TrackerError, match="converter returned"):
        adf.markdown_to_adf("# heading")


def test_the_read_path_degrades_on_what_the_tracker_really_returns():
    assert adf.adf_to_markdown(None) == "", "a field with no body is empty text, not an error"
    assert adf.adf_to_markdown("legacy rendered body") == "legacy rendered body"
    with pytest.raises(TrackerError, match="got int"):
        adf.adf_to_markdown(42)


def test_a_document_the_converter_cannot_read_names_its_shape_only():
    broken = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": "sensitive body text"}]}
    with pytest.raises(TrackerError) as failure:
        adf.adf_to_markdown(broken)
    assert "sensitive body text" not in str(failure.value), "a failure message must not dump the document"
    assert "'doc'" in str(failure.value), f"the failure must name the shape it got: {failure.value}"
