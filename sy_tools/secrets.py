"""Two-pass artifact sanitisation: known-value scrub first, then a pattern scanner.

The order is load-bearing, and over a text artifact there is no way to run one pass without the
other: the scrub catches a credential this process actually holds, verbatim, whatever shape it has;
the scanner catches a shape it recognises whether or not this process ever held the value. A payload
that is not UTF-8 text is refused outright unless the caller declares it opaque, and then the scrub
alone is skipped — it is the pass that needs the decode — while the scanner still runs behind the
scenes and a genuine finding still blocks the upload, but its result is never credited in the report,
because some binary content is silently invisible to it too. `skills/tracker/CONTRACT.md` states the
same contract for the two verbs that upload through it.

Nothing here returns, logs, or embeds a credential value — only variable names and occurrence counts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

DEFAULT_MIN_LENGTH = 6
SCANNER = "gitleaks"
SCANNER_TIMEOUT_SECONDS = 60

# The one home of the credential-name word set: every consumer reaches it through
# `looks_like_secret_name`, never through a copy of its own.
SECRET_WORDS = frozenset({
    "TOKEN", "SECRET", "SECRETS", "KEY", "KEYS", "APIKEY", "PASSWORD", "PASSWD",
    "CREDENTIAL", "CREDENTIALS", "PAT", "AUTH",
})


class SanitizeError(RuntimeError):
    """Sanitisation could not be completed, or the scanner found something after the scrub."""


def looks_like_secret_name(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """True when a variable or config key name is credential-shaped, by word rather than substring.

    `A_TOKEN` matches, `TOKENIZER_PATH` does not. `extra` merges org-specific words
    (`redaction.extra_words`) on top of the built-in set.
    """
    words = re.split(r"[^A-Za-z0-9]+", name.upper())
    all_words = SECRET_WORDS if not extra else SECRET_WORDS | extra
    return any(word in all_words for word in words if word)


def discover_secret_vars(
    min_length: int = DEFAULT_MIN_LENGTH, extra_words: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Every environment variable whose *name* is credential-shaped, with a value long enough to matter."""
    # Name-based, not value-based: value shape alone would redact ordinary long paths, URLs and ids.
    return {
        name: value
        for name, value in os.environ.items()
        if value and len(value) >= min_length and looks_like_secret_name(name, extra=extra_words)
    }


def scrub_text(text: str, secrets: dict[str, str]) -> tuple[str, dict[str, int]]:
    """Replace every literal occurrence of each secret value with its redaction marker."""
    counts: dict[str, int] = {}
    # Longest value first: a secret that is a substring of another is consumed whole, not fragmented.
    for name, value in sorted(secrets.items(), key=lambda kv: len(kv[1]), reverse=True):
        occurrences = text.count(value)
        if occurrences:
            text = text.replace(value, f"<REDACTED:{name}>")
            counts[name] = occurrences
    return text, counts


def scrub_file(path: Path, secrets: dict[str, str]) -> dict[str, int]:
    """Rewrite `path` in place with every known secret value redacted. Returns per-name counts.

    Raises `UnicodeDecodeError` on a payload that is not UTF-8 text; `sanitize` depends on that as the
    signal that the known-value scrub cannot act on the artifact.
    """
    scrubbed, counts = scrub_text(path.read_text(encoding="utf-8"), secrets)
    if counts:
        path.write_text(scrubbed, encoding="utf-8")
    return counts


def scan_file(path: Path) -> list[dict]:
    """Run the pattern scanner over an already-scrubbed file and return its findings."""
    if not shutil.which(SCANNER):
        raise SanitizeError(
            f"{SCANNER} is not installed, so the second sanitisation pass cannot run. "
            "Install it rather than uploading on the scrub alone."
        )
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        try:
            # `--redact`, never `--verbose`: an unredacted report writes the matched value back out.
            # `stdin` is closed as on every subprocess this server spawns — its own stdin is the
            # JSON-RPC transport, and a child that inherits it can eat a frame the server was to read.
            proc = subprocess.run(
                [SCANNER, "dir", str(path), "--redact", "--report-format", "json",
                 "--report-path", str(report), "--exit-code", "0", "--log-level", "error"],
                capture_output=True, text=True, check=False, timeout=SCANNER_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise SanitizeError(
                f"{SCANNER} did not finish within {SCANNER_TIMEOUT_SECONDS}s and was killed; "
                "refusing to upload an unscanned artifact."
            ) from exc
        if proc.returncode != 0:
            raise SanitizeError(f"{SCANNER} failed: {proc.stderr.strip()[:500]}")
        if not report.is_file():
            return []
        body = report.read_text(encoding="utf-8").strip()
        if not body:
            return []
        try:
            findings = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SanitizeError(
                f"{SCANNER} produced an unparseable report; refusing to upload unverified."
            ) from exc
    if not isinstance(findings, list):
        raise SanitizeError(
            f"{SCANNER} report was a {type(findings).__name__}, not the expected list; "
            "refusing to upload unverified."
        )
    return findings


def sanitize(
    path: Path, *, require: tuple[str, ...] = (), extra_words: frozenset[str] = frozenset(),
    allow_opaque: bool = False,
) -> dict:
    """Both passes, in order, over a text `path` in place; the scanner alone over a declared opaque one.

    Raises rather than returning an unsafe file. `require` names variables that must resolve to a
    scrubbable value in this process's environment; an absent one is a loud failure rather than a
    clean zero-redaction run. `allow_opaque` turns a payload the known-value scrub cannot decode from
    a refusal into a still-scanned passthrough: the pattern scanner needs no decode and still runs, and
    a genuine finding still blocks the upload, but the report never credits it with a clean result --
    only the declaration is returned, exactly as if neither pass had looked.
    """
    if not path.is_file():
        raise SanitizeError(f"artifact not found: {path}")
    secrets = discover_secret_vars(extra_words=extra_words)
    missing = sorted(name for name in require if name not in secrets)
    if missing:
        raise SanitizeError(
            "required credential(s) not present in this process's environment, so nothing would "
            f"be scrubbed for them: {', '.join(missing)}"
        )
    # Exactly one statement inside the `try`: `scan_file` decodes the scanner's own report, so a wider
    # span would read a corrupt report as an opaque payload and claim nothing needed scanning.
    try:
        redactions: dict[str, int] | None = scrub_file(path, secrets)
    except UnicodeDecodeError as exc:
        if not allow_opaque:
            raise SanitizeError(
                f"{path.name} is not UTF-8 text -- even one byte outside a valid UTF-8 sequence trips "
                "this -- so the known-value scrub cannot act on it; refusing to upload it unscrubbed. "
                "Pass allow_opaque only for an artifact you have separately established carries no "
                "credential: it declares that and still runs the pattern scanner alone before "
                "uploading it un-scrubbed."
            ) from exc
        redactions = None
    findings = scan_file(path)
    if findings:
        rules = sorted({str(f.get("RuleID") or f.get("Description") or "unknown") for f in findings})
        scrub_clause = "" if redactions is None else " after the known-value scrub"
        raise SanitizeError(
            f"{SCANNER} reports {len(findings)} finding(s){scrub_clause} ({', '.join(rules)}); "
            "refusing to upload."
        )
    if redactions is None:
        # The scanner still ran, above, as a best-effort check -- a real finding would already have
        # raised -- but some binary content is silently invisible to it too: its own default allowlist
        # skips many binary extensions, it skips any archive outright at the default archive depth, and
        # its reader skips any file whose leading bytes sniff as application/*. A `0` here would not be
        # a fact the report could stand behind, so only the declaration is returned, exactly as if
        # neither pass had looked.
        return {
            "opaque": True,
            "skipped_reason": "not UTF-8 text: the known-value scrub cannot act on it",
        }
    return {
        "scrubbed_vars": sorted(redactions),  # names only, never a value
        "redactions": sum(redactions.values()),
        "scanner": SCANNER,
        "scanner_findings": 0,
    }
