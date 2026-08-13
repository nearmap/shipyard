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


@pytest.fixture
def opaque(tmp_path, monkeypatch) -> Path:
    """An artifact no codec can read that still holds the fake credential, with that value in the environment.

    `planted` can never reach the opaque branch — it is valid UTF-8 by construction — so the one
    trailing invalid byte is what makes the payload undecodable while it still carries a value the
    refusal and the report both have to keep to themselves.
    """
    monkeypatch.setenv(FAKE_VAR, FAKE_TOKEN)
    path = tmp_path / "PROJ-1-ship-transcript.bin"
    path.write_bytes(FAKE_TOKEN.encode() + b"\xff")
    return path


def test_name_heuristic_is_word_based():
    assert secrets.looks_like_secret_name("A_TOKEN")
    assert secrets.looks_like_secret_name("AWS_SECRET_ACCESS_KEY")
    assert not secrets.looks_like_secret_name("TOKENIZER_PATH"), "substring matching would over-redact"
    assert not secrets.looks_like_secret_name("SOME_SITE")
    assert not secrets.looks_like_secret_name("NM_BEARER")
    assert secrets.looks_like_secret_name("NM_BEARER", extra=frozenset({"BEARER"})), "extra must widen the match"


def test_discovery_needs_both_a_secret_shaped_name_and_a_long_enough_value(monkeypatch):
    monkeypatch.setenv("SY_TEST_DISCOVER_TOKEN", "abcdef0123456789secretvalue")
    monkeypatch.setenv("SY_TEST_DISCOVER_SHORT_KEY", "ab")
    monkeypatch.setenv("SY_TEST_DISCOVER_NOTASECRET", "this-name-is-not-secret-shaped-but-is-long-enough")
    monkeypatch.setenv("SY_TEST_DISCOVER_BEARER", "abcdef0123456789bearervalue")

    found = secrets.discover_secret_vars()
    assert found["SY_TEST_DISCOVER_TOKEN"] == "abcdef0123456789secretvalue"
    assert "SY_TEST_DISCOVER_SHORT_KEY" not in found, "a value below the min-length floor must be skipped"
    assert "SY_TEST_DISCOVER_NOTASECRET" not in found, "a non-secret-shaped name must be skipped whatever its length"
    assert "SY_TEST_DISCOVER_BEARER" not in found, "a fragment outside the built-in set is not a false positive"

    widened = secrets.discover_secret_vars(extra_words=frozenset({"BEARER"}))
    assert "SY_TEST_DISCOVER_BEARER" in widened, "extra_words must widen discovery, not just the name heuristic"


def test_scrubbing_an_already_scrubbed_file_finds_nothing(planted):
    found = {FAKE_VAR: FAKE_TOKEN}
    assert secrets.scrub_file(planted, found) == {FAKE_VAR: 2}
    assert secrets.scrub_file(planted, found) == {}, "a second pass must be a no-op, not a re-redaction"


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


def test_an_artifact_neither_pass_can_read_is_refused_unless_the_caller_declares_it(opaque):
    with pytest.raises(secrets.SanitizeError) as raised:
        secrets.sanitize(opaque, require=(FAKE_VAR,))
    assert "not UTF-8 text" in str(raised.value), "the refusal must name why neither pass could run"
    assert "allow_opaque" in str(raised.value), "a refusal with no named way forward is a dead end"
    assert FAKE_TOKEN not in str(raised.value), "a message must never carry the value it could not scrub"


def test_a_declared_opaque_artifact_still_runs_the_scanner(opaque, monkeypatch):
    """`allow_opaque` skips only the known-value scrub -- the scrub needs a decode this payload lacks.

    The pattern scanner needs no decode and must still run and still be reported, not read as skipped.
    """
    calls: list[Path] = []
    monkeypatch.setattr(secrets, "scan_file", lambda p: calls.append(p) or [])
    report = secrets.sanitize(opaque, require=(FAKE_VAR,), allow_opaque=True)
    assert calls == [opaque], "the scanner must still run over a declared-opaque payload"
    assert report["opaque"] is True, f"an unscrubbed upload must declare itself: {report}"
    assert report["skipped_reason"], "the declaration must carry its reason, not only a flag"
    assert report["scanner"] == secrets.SCANNER
    assert report["scanner_findings"] == 0
    assert not {"scrubbed_vars", "redactions"} & set(report), (
        f"the report claims a scrub pass that never ran: {sorted(report)}"
    )
    assert FAKE_TOKEN not in json.dumps(report), "the report must carry names and counts, never a value"
    assert opaque.read_bytes() == FAKE_TOKEN.encode() + b"\xff", (
        "a payload the scrub could not act on must be left byte-identical, not half-written"
    )


def test_a_declared_opaque_artifact_with_a_scanner_finding_is_still_refused(opaque, monkeypatch):
    """`allow_opaque` opts out of the scrub, never out of the scanner: a live finding still blocks."""
    monkeypatch.setattr(secrets, "scan_file", lambda _p: [{"RuleID": "fake-rule-id"}])
    with pytest.raises(secrets.SanitizeError) as raised:
        secrets.sanitize(opaque, require=(FAKE_VAR,), allow_opaque=True)
    assert "fake-rule-id" in str(raised.value)
    assert FAKE_TOKEN not in str(raised.value), "a message must never carry the value it could not scrub"


def test_a_scanner_report_that_will_not_decode_is_not_read_as_an_opaque_payload(planted, monkeypatch):
    """`scan_file` decodes the scanner's own report, so a codec failure there is a scanner fault.

    Inside the same `try` as the scrub it would present as an opaque *payload* and, with the flag set,
    report a perfectly readable artifact as deliberately unscanned when nothing had scanned it.
    """
    def undecodable_report(_path: Path) -> list[dict]:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(secrets, "scan_file", undecodable_report)
    with pytest.raises(UnicodeDecodeError):
        secrets.sanitize(planted, require=(FAKE_VAR,), allow_opaque=True)


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

    A `PreToolUse` hook that dies emits no decision and Claude Code continues, so an unrunnable `git`
    silently disables every built-in denial too, not merely the configured extra words. Run as a
    subprocess: the failure is a process-level crash, which an in-process call cannot see.
    """
    empty_bin = tmp_path / "no-git-here"
    empty_bin.mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"},
        "PATH": str(empty_bin), "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "PYTHONPATH": str(PLUGIN_ROOT),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "sy_tools.guards.secret_guard"],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo $EXAMPLE_TOKEN"}}),
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

    `secret_guard.py` resolves `redaction.extra_words` through `config.repo_root()`, which shells out to
    `git rev-parse`. Unbounded, a git that blocks rather than fails — a wrapper or credential helper
    waiting on something, a binary that does not return — leaves the `PreToolUse` hook with no output for
    as long as the platform allows, and no output is no decision: every built-in denial silently skipped.
    The hook's own fail-closed backstop is an `except Exception`, which a hang never reaches, so only the
    bound closes this path. In a child process, because a hang is only observable as a process that
    produces no decision.
    """
    # Raises rather than asserts: the child inherits this environment, and a `PYTHONOPTIMIZE` in it
    # would strip the assert and pass the negative control of removing `timeout=` from the real code.
    bound_check = (
        "import subprocess\n"
        "from sy_tools import config as sy_config\n"
        "from sy_tools.guards import secret_guard\n"
        "calls = []\n"
        "def check(cmd, kwargs):\n"
        "    calls.append(list(cmd))\n"
        "    if kwargs.get('timeout') != sy_config.GIT_TIMEOUT_SECONDS:\n"
        "        raise AssertionError(f'the git call must be bounded: {cmd} {kwargs}')\n"
        "    if kwargs.get('stdin') is not subprocess.DEVNULL:\n"
        "        raise AssertionError(f'the git call must not read the hook event on stdin: {cmd} {kwargs}')\n"
    )
    # Phase one wedges the first git call, which the refusal-memoization claim needs: the root itself
    # has to refuse.
    wedge_first = bound_check + (
        "def wedge(cmd, **kwargs):\n"
        "    check(cmd, kwargs)\n"
        "    raise subprocess.TimeoutExpired(cmd=cmd, timeout=sy_config.GIT_TIMEOUT_SECONDS)\n"
        "subprocess.run = wedge\n"
        "try:\n"
        "    sy_config.repo_root()\n"
        "except sy_config.ConfigError as exc:\n"
        "    if 'did not resolve the repository root' not in str(exc):\n"
        "        raise AssertionError(f'the refusal must name its cause: {exc}')\n"
        "    if f'within {sy_config.GIT_TIMEOUT_SECONDS}s' not in str(exc):\n"
        "        raise AssertionError(f'and the bound it hit: {exc}')\n"
        "else:\n"
        "    raise AssertionError('a wedged git must be refused, not waited on')\n"
        "if secret_guard._extra_words() != frozenset():\n"
        "    raise AssertionError('the timeout must narrow the gate, not break it')\n"
        "if 'extra_words' not in (secret_guard._CONFIG_WARNING or ''):\n"
        "    raise AssertionError(f'the drop must be reported: {secret_guard._CONFIG_WARNING}')\n"
        "if not secret_guard.decision('Bash', {'command': 'echo $EXAMPLE_TOKEN'}):\n"
        "    raise AssertionError('built-ins must still deny')\n"
    )
    # Phase two answers every earlier call and wedges the last: the resolver reaches git in four
    # places, and wedging the first left the other three carrying no `timeout=` while this stayed green.
    wedge_last = bound_check + (
        "import os\n"
        "root = os.environ['CLAUDE_PROJECT_DIR']\n"
        "def answered(cmd, **kwargs):\n"
        "    check(cmd, kwargs)\n"
        "    if '--is-inside-work-tree' in cmd:\n"  # the last site a cold resolution reaches
        "        raise subprocess.TimeoutExpired(cmd=cmd, timeout=sy_config.GIT_TIMEOUT_SECONDS)\n"
        "    if '--show-toplevel' in cmd:\n"
        "        return subprocess.CompletedProcess(cmd, 0, stdout=root + '\\n', stderr='')\n"
        "    if '--git-common-dir' in cmd:\n"
        "        return subprocess.CompletedProcess(cmd, 0, stdout=os.path.join(root, '.git') + '\\n', stderr='')\n"
        "    return subprocess.CompletedProcess(cmd, 1, stdout='', stderr='')\n"
        "subprocess.run = answered\n"
        "try:\n"
        "    sy_config.resolve()\n"
        "except sy_config.ConfigError as exc:\n"
        "    if f'within {sy_config.GIT_TIMEOUT_SECONDS}s and was killed' not in str(exc):\n"
        "        raise AssertionError(f'the refusal must name the bound it hit: {exc}')\n"
        "else:\n"
        "    raise AssertionError('a wedged git must be refused, not waited on')\n"
        "if len(calls) < 2:\n"
        "    raise AssertionError(f'the earlier sites must be reached, not skipped: {calls}')\n"
        "for site in ('--show-toplevel', '--git-common-dir', 'core.worktree', '--is-inside-work-tree'):\n"
        "    if not any(site in ' '.join(cmd) for cmd in calls):\n"
        "        raise AssertionError(f'{site} was never reached, so its bound is unproven: {calls}')\n"
        "if secret_guard._extra_words() != frozenset():\n"
        "    raise AssertionError('the timeout must narrow the gate, not break it')\n"
        "if not secret_guard.decision('Bash', {'command': 'echo $EXAMPLE_TOKEN'}):\n"
        "    raise AssertionError('built-ins must still deny')\n"
    )
    for label, child in (("the first git call", wedge_first), ("a later git call", wedge_last)):
        proc = subprocess.run(
            [sys.executable, "-c", child], cwd=tmp_path, capture_output=True, text=True, check=False,
            env={
                **os.environ,
                "PYTHONPATH": str(PLUGIN_ROOT),
                "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "CLAUDE_PROJECT_DIR": str(tmp_path),
                "HOME": str(tmp_path / "home"),  # so worktree.root stays derived, as it is by default
            },
        )
        assert proc.returncode == 0, f"a git wedged on {label} was not bounded and degraded: {proc.stderr}"


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
