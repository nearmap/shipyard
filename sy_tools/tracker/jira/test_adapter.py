"""Offline tests for the Jira adapter: no network, no real credential.

The credential test is the load-bearing one. The fake token is planted in the environment the
same way the real one lives there, and every surface the call produces — the returned dict, the
exception message, the URL, the request body, the non-auth headers, stdout — is asserted clean.
The `Authorization` header is the single place it is allowed to appear.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import urllib.error

import pytest

from .. import TIMEOUT_SECONDS, TrackerError
from . import adapter

FAKE_TOKEN = "ATATT3xFfGF0-fake-adapter-fixture-6b21d0e9a7c4"
FAKE_EMAIL = "shipyard-bot@example.com"
FAKE_SITE = "example.atlassian.net"


@pytest.fixture
def credentials(monkeypatch) -> None:
    """Resolved identifiers and the planted credential, exactly where each really lives."""
    values = {"tracker_config.email": FAKE_EMAIL, "tracker_config.site": FAKE_SITE}
    monkeypatch.setattr(adapter.config, "get", lambda path, *, default=None: values.get(path, default))
    monkeypatch.setenv(adapter.TOKEN_ENV, FAKE_TOKEN)


@pytest.fixture
def artifact(tmp_path) -> Path:
    path = tmp_path / "PROJ-1-ship-transcript.txt"
    path.write_bytes(b"transcript body\n")
    return path


def _transport(monkeypatch, response: object) -> list[dict]:
    """Replace the module's request helper with a recorder returning `response`."""
    calls: list[dict] = []

    def fake_request(method, url, auth, data=None, headers=None):
        calls.append({"method": method, "url": url, "auth": auth, "data": data, "headers": headers or {}})
        return 200, response

    monkeypatch.setattr(adapter, "request", fake_request)
    return calls


def test_attach_artifact_posts_a_verified_multipart_upload(credentials, artifact, monkeypatch, capsys):
    echoed = [{"id": "10501", "filename": artifact.name, "size": 16, "created": "2026-07-30T00:00:00.000+0000"}]
    calls = _transport(monkeypatch, echoed)

    evidence = adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)

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


def test_a_response_that_does_not_confirm_the_filename_fails(credentials, artifact, monkeypatch):
    _transport(monkeypatch, [{"id": "9", "filename": "something-else.txt"}])
    with pytest.raises(TrackerError, match="did not confirm"):
        adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)


def test_the_credential_appears_only_in_the_authorization_header(credentials, artifact, monkeypatch, capsys):
    calls = _transport(monkeypatch, [{"id": "10501", "filename": artifact.name}])
    evidence = adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)
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
        adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)
    assert FAKE_TOKEN not in str(failure.value), "a failure message must not leak the credential either"


@pytest.mark.parametrize(
    "raised",
    [
        TimeoutError("timed out"),
        urllib.error.URLError(TimeoutError("timed out")),
    ],
    ids=["read-stall", "connect-stall"],
)
def test_a_stalled_call_is_bounded_and_becomes_a_tracker_error(monkeypatch, raised):
    """No REST call may hang this server, and neither shape a timeout takes may escape raw.

    `urlopen` wraps a connect-phase timeout in `URLError`, but a stall reading the response
    escapes as a bare `TimeoutError`, so the adapter has to catch both.
    """
    seen: list[object] = []

    def fake_urlopen(req, timeout=None):
        seen.append(timeout)
        raise raised

    monkeypatch.setattr(adapter.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(TrackerError) as failure:
        adapter.request("GET", "https://example.atlassian.net/rest/api/3/myself", "Basic x")

    assert seen == [TIMEOUT_SECONDS], f"the call must be bounded, got timeout={seen}"
    assert "example.atlassian.net" in str(failure.value), "the failure must name the call that stalled"


def test_missing_inputs_name_what_is_missing(credentials, artifact, monkeypatch):
    _transport(monkeypatch, [{"id": "1", "filename": artifact.name}])
    monkeypatch.delenv(adapter.TOKEN_ENV)
    with pytest.raises(TrackerError) as missing_token:
        adapter.JiraAdapter().attach_artifact("PROJ-1", artifact)
    assert adapter.TOKEN_ENV in str(missing_token.value)
    assert "config" in str(missing_token.value), "the message must say where the secret does not belong"

    monkeypatch.setenv(adapter.TOKEN_ENV, FAKE_TOKEN)
    monkeypatch.setattr(adapter.config, "get", lambda path, *, default=None: default)
    with pytest.raises(TrackerError, match=r"tracker_config\.email"):
        adapter.JiraAdapter().preflight()
