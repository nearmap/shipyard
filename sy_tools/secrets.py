"""Two-pass artifact sanitisation: known-value scrub first, then a pattern scanner.

Port of `scripts/scrub_known_secrets.py` plus the scanner orchestration that the selected
adapter's attachments reference under `skills/tracker/` prescribes as prose. The two passes are
complementary and the order is load-bearing, so this module never exposes a way to run one
without the other: the scrub catches a credential this process actually holds, verbatim,
whatever shape it has; the scanner catches a shape it recognises whether or not this process
ever held the value.

Nothing here ever returns, logs, or embeds a credential value — only variable names and
occurrence counts.
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

# Kept in step with `scripts/secret_words.py`; duplicated rather than imported so this package
# stands alone (see the module docstring in `sy_tools/__init__.py`).
SECRET_WORDS = frozenset({
    "TOKEN", "SECRET", "SECRETS", "KEY", "KEYS", "APIKEY", "PASSWORD", "PASSWD",
    "CREDENTIAL", "CREDENTIALS", "PAT", "AUTH",
})


class SanitizeError(RuntimeError):
    """Sanitisation could not be completed, or the scanner found something after the scrub."""


def looks_like_secret_name(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """True when a variable or config key name is credential-shaped, by word rather than substring.

    Word-split so `A_TOKEN` matches while `TOKENIZER_PATH` does not. `extra` merges in
    org-specific fragments (the `redaction.extra_words` config key) on top of the built-in set.
    """
    words = re.split(r"[^A-Za-z0-9]+", name.upper())
    all_words = SECRET_WORDS if not extra else SECRET_WORDS | extra
    return any(word in all_words for word in words if word)


def discover_secret_vars(
    min_length: int = DEFAULT_MIN_LENGTH, extra_words: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Every environment variable whose *name* is credential-shaped, with a value long enough to matter.

    Name-based, not value-based: scrubbing on value shape alone would redact ordinary long
    strings (paths, URLs, ids) that happen to be in the environment.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if value and len(value) >= min_length and looks_like_secret_name(name, extra=extra_words)
    }


def scrub_text(text: str, secrets: dict[str, str]) -> tuple[str, dict[str, int]]:
    """Replace every literal occurrence of each secret value with its redaction marker.

    Longest value first, so a secret that is a substring of another is consumed whole by the
    longer replacement rather than fragmenting it.
    """
    counts: dict[str, int] = {}
    for name, value in sorted(secrets.items(), key=lambda kv: len(kv[1]), reverse=True):
        occurrences = text.count(value)
        if occurrences:
            text = text.replace(value, f"<REDACTED:{name}>")
            counts[name] = occurrences
    return text, counts


def scrub_file(path: Path, secrets: dict[str, str]) -> dict[str, int]:
    """Rewrite `path` in place with every known secret value redacted. Returns per-name counts."""
    scrubbed, counts = scrub_text(path.read_text(encoding="utf-8"), secrets)
    if counts:
        path.write_text(scrubbed, encoding="utf-8")
    return counts


def scan_file(path: Path) -> list[dict]:
    """Run the pattern scanner over an already-scrubbed file and return its findings.

    `--redact` is mandatory and `--verbose` is never passed: an unredacted scanner report would
    write the matched value straight back out, re-leaking exactly what this exists to prevent.
    """
    if not shutil.which(SCANNER):
        raise SanitizeError(
            f"{SCANNER} is not installed, so the second sanitisation pass cannot run. "
            "Install it rather than uploading on the scrub alone."
        )
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        try:
            proc = subprocess.run(
                [SCANNER, "dir", str(path), "--redact", "--report-format", "json",
                 "--report-path", str(report), "--exit-code", "0", "--log-level", "error"],
                capture_output=True, text=True, check=False, timeout=SCANNER_TIMEOUT_SECONDS,
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
        findings = json.loads(body) if body else []
    return findings if isinstance(findings, list) else []


def sanitize(
    path: Path, *, require: tuple[str, ...] = (), extra_words: frozenset[str] = frozenset(),
) -> dict:
    """Both passes, in order, over `path` in place. Raises rather than returning an unsafe file.

    `require` names variables that must actually resolve to a scrubbable value in this process's
    environment; an absent one is a loud failure, because auto-discovery alone would otherwise
    report a silent, clean zero-redaction run for exactly the credential it was asked to strip.
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
    redactions = scrub_file(path, secrets)
    findings = scan_file(path)
    if findings:
        rules = sorted({str(f.get("RuleID") or f.get("Description") or "unknown") for f in findings})
        raise SanitizeError(
            f"{SCANNER} still reports {len(findings)} finding(s) after the known-value scrub "
            f"({', '.join(rules)}); refusing to upload."
        )
    return {
        "scrubbed_vars": sorted(redactions),  # names only, never a value
        "redactions": sum(redactions.values()),
        "scanner": SCANNER,
        "scanner_findings": 0,
    }
