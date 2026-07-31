"""A known fake credential must be gone from the artifact before anything can upload it."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sy_tools import secrets

FAKE_TOKEN = "ATATT3xFfGF0-fake-shipyard-fixture-4c8a91e2b7d05f6a1e"
FAKE_VAR = "SY_TEST_FIXTURE_TOKEN"


@pytest.fixture
def planted(tmp_path, monkeypatch) -> Path:
    """An artifact with the fake credential written into it verbatim, and that value in the environment."""
    monkeypatch.setenv(FAKE_VAR, FAKE_TOKEN)
    path = tmp_path / "PROJ-1-ship-transcript.txt"
    path.write_text(
        f"line one\nAuthorization: Basic {FAKE_TOKEN}\nnoise\nrepeat: {FAKE_TOKEN}\n", encoding="utf-8",
    )
    return path


def test_name_heuristic_is_word_based():
    assert secrets.looks_like_secret_name("A_TOKEN")
    assert secrets.looks_like_secret_name("AWS_SECRET_ACCESS_KEY")
    assert not secrets.looks_like_secret_name("TOKENIZER_PATH"), "substring matching would over-redact"
    assert not secrets.looks_like_secret_name("SOME_SITE")
    assert not secrets.looks_like_secret_name("NM_BEARER")
    assert secrets.looks_like_secret_name("NM_BEARER", extra=frozenset({"BEARER"})), "extra must widen the match"


def test_longest_value_first_prevents_a_fragmented_redaction():
    found = {"SHORT_TOKEN": "abc123secret", "LONG_TOKEN": "prefix-abc123secret-suffix"}
    scrubbed, counts = secrets.scrub_text("value: prefix-abc123secret-suffix\n", found)
    assert "abc123secret" not in scrubbed
    assert scrubbed.strip() == "value: <REDACTED:LONG_TOKEN>"
    assert counts == {"LONG_TOKEN": 1}


def test_sanitize_redacts_the_fixture_before_it_can_be_uploaded(planted):
    report = secrets.sanitize(planted, require=(FAKE_VAR,))
    body = planted.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in body, "the planted credential must not survive sanitisation"
    assert body.count(f"<REDACTED:{FAKE_VAR}>") == 2
    assert report["redactions"] == 2
    assert report["scrubbed_vars"] == [FAKE_VAR]
    assert report["scanner_findings"] == 0
    assert FAKE_TOKEN not in json.dumps(report), "the report must carry names and counts, never a value"


def test_sanitize_runs_the_scanner_pass_too(planted, monkeypatch):
    """The scan is a second, independent pass — it is not skipped just because the scrub found things."""
    calls: list[Path] = []
    monkeypatch.setattr(secrets, "scan_file", lambda p: calls.append(p) or [])
    secrets.sanitize(planted, require=(FAKE_VAR,))
    assert calls == [planted], "the scanner must run on the already-scrubbed file, every time"


def test_a_scanner_finding_after_the_scrub_refuses_the_upload(planted, monkeypatch):
    monkeypatch.setattr(secrets, "scan_file", lambda _p: [{"RuleID": "generic-api-key"}])
    with pytest.raises(secrets.SanitizeError, match="refusing to upload"):
        secrets.sanitize(planted, require=(FAKE_VAR,))


def test_a_wedged_scanner_is_killed_and_refuses_the_upload(planted, monkeypatch):
    def wedge(*_args, **kwargs):
        assert kwargs.get("timeout") == secrets.SCANNER_TIMEOUT_SECONDS, "scanner call must be bounded"
        raise subprocess.TimeoutExpired(cmd=secrets.SCANNER, timeout=secrets.SCANNER_TIMEOUT_SECONDS)

    monkeypatch.setattr(secrets.subprocess, "run", wedge)
    with pytest.raises(secrets.SanitizeError, match="did not finish"):
        secrets.scan_file(planted)


@pytest.mark.parametrize("body", ['{"not": "a list"}', "not json at all"])
def test_an_unreadable_scanner_report_refuses_the_upload(planted, monkeypatch, body):
    """A report the code cannot read as findings must fail closed, never count as zero findings."""
    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
        Path(cmd[cmd.index("--report-path") + 1]).write_text(body, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(secrets.subprocess, "run", fake_run)
    with pytest.raises(secrets.SanitizeError, match="refusing to upload unverified"):
        secrets.scan_file(planted)


def test_a_required_credential_absent_from_the_environment_fails_loudly(planted, monkeypatch):
    monkeypatch.delenv(FAKE_VAR)
    with pytest.raises(secrets.SanitizeError, match="not present in this process's environment"):
        secrets.sanitize(planted, require=(FAKE_VAR,))
    assert FAKE_TOKEN in planted.read_text(encoding="utf-8"), "a loud failure must not half-scrub then stop"


def test_missing_artifact_is_refused(tmp_path):
    with pytest.raises(secrets.SanitizeError, match="artifact not found"):
        secrets.sanitize(tmp_path / "absent.txt")
