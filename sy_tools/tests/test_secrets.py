"""A known fake credential must be gone from the artifact before anything can upload it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sy_tools import secrets

FAKE_TOKEN = "ATATT3xFfGF0-fake-shipyard-fixture-4c8a91e2b7d05f6a1e"
FAKE_VAR = "SY_TEST_FIXTURE_TOKEN"
PLUGIN_ROOT = Path(__file__).resolve().parents[2]


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


def test_a_git_that_cannot_be_run_does_not_disable_the_secret_gate(tmp_path):
    """`secret_guard.py` reads `redaction.extra_words`, and that resolution shells out to `git`.

    With no `git` on `PATH` the resolver raised `FileNotFoundError`, which crashed the whole hook
    process: a `PreToolUse` hook that dies emits no decision and Claude Code continues, so a missing
    binary silently disabled every built-in denial too, not merely the configured extra words. Run as
    a subprocess because the failure was a process-level crash, which an in-process call cannot see.
    """
    empty_bin = tmp_path / "no-git-here"
    empty_bin.mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"},
        "PATH": str(empty_bin), "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
    }
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "secret_guard.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo $ACLI_TOKEN"}}),
        cwd=tmp_path, capture_output=True, text=True, check=False, env=env,
    )
    assert proc.returncode == 0, f"the hook crashed instead of degrading: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny", payload
    assert "redaction.extra_words" in payload.get("systemMessage", ""), (
        f"the narrowed gate must be reported to the user, not only on unread stderr: {payload!r}"
    )


def test_a_wedged_git_does_not_hang_the_secret_gate(tmp_path):
    """The same call site as the test above, failing the one way an `except` clause cannot catch.

    `secret_guard.py` resolves `redaction.extra_words` through `sy_config.repo_root()`, which shells out
    to `git rev-parse`. That call had no `timeout=`, so a git that blocks rather than fails — an
    unresponsive network mount, a git wrapper that waits on something — left the `PreToolUse` hook with
    no output for as long as the platform allowed, and no output is no decision, i.e. every built-in
    denial silently skipped. The hook's own fail-closed backstop is an `except Exception`, which a hang
    never reaches, so only the bound closes this path. Once bounded, it degrades exactly as the missing
    binary above does: a `SystemExit` the hook's word-list fallback already catches.

    In a child process with `scripts/` on `PYTHONPATH`, as `test_config.py` runs that resolver: those
    modules are the shipped CLI, not part of this package, so they are not importable from here.
    """
    child = (
        "import subprocess\n"
        "import sy_config, secret_guard\n"
        "def wedge(cmd, **kwargs):\n"
        "    assert kwargs.get('timeout') == sy_config.GIT_TIMEOUT_SECONDS, 'the git call must be bounded'\n"
        "    raise subprocess.TimeoutExpired(cmd=cmd, timeout=sy_config.GIT_TIMEOUT_SECONDS)\n"
        "subprocess.run = wedge\n"
        "try:\n"
        "    sy_config.repo_root()\n"
        "except SystemExit as exc:\n"
        "    assert 'did not resolve the repository root' in str(exc), exc\n"
        "    assert f'within {sy_config.GIT_TIMEOUT_SECONDS}s' in str(exc), exc\n"
        "else:\n"
        "    raise AssertionError('a wedged git must be refused, not waited on')\n"
        "assert secret_guard._extra_words() == frozenset(), 'the timeout must narrow the gate, not break it'\n"
        "assert 'extra_words' in (secret_guard._CONFIG_WARNING or ''), secret_guard._CONFIG_WARNING\n"
        "assert secret_guard.decision('Bash', {'command': 'echo $ACLI_TOKEN'}), 'built-ins must still deny'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child], cwd=tmp_path, capture_output=True, text=True, check=False,
        env={
            **os.environ, "PYTHONPATH": str(PLUGIN_ROOT / "scripts"),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "CLAUDE_PROJECT_DIR": str(tmp_path),
        },
    )
    assert proc.returncode == 0, f"a wedged git was not bounded and degraded: {proc.stderr}"


def test_the_scanner_does_not_inherit_the_servers_stdin(planted, monkeypatch):
    """The scanner is spawned from inside the MCP server, whose stdin is the JSON-RPC transport.

    Same invariant the tracker adapters pin for their own spawns: a child that inherits this stdin
    can consume a frame the server was going to read, desynchronising the session.
    """
    seen: list[dict] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        seen.append(kwargs)
        Path(cmd[cmd.index("--report-path") + 1]).write_text("[]", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(secrets.subprocess, "run", fake_run)
    secrets.scan_file(planted)
    assert seen and all(kwargs.get("stdin") == subprocess.DEVNULL for kwargs in seen), (
        f"the scanner was handed the server's own stdin: {seen}"
    )
