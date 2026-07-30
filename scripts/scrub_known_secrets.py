#!/usr/bin/env python3
"""Deterministically redact known-secret env var values from a file before pattern-based scanning.

`gitleaks` (and any other pattern/entropy scanner) only flags a secret if it matches a known
rule shape. A value can still leak verbatim without matching any rule — an org-specific token
format, a value below the entropy threshold, or simply a secret that got echoed into a Bash
tool-call result (an `env | grep` dump, a scanner run with `-v`) and is now permanently part of
the session JSONL. Every later transcript render reproduces that echo byte-for-byte, so a
pattern scanner has to re-catch it every single time.

This script sidesteps pattern matching entirely: it reads the actual values currently held by
credential-shaped environment variables and replaces every literal occurrence of each value with
`<REDACTED:VAR_NAME>`, regardless of what the value looks like or how many times or where it
appears. Run it first, then run gitleaks on the result — the two are complementary, not
redundant: this step catches known values verbatim; gitleaks still catches secrets this process
never held (e.g. one embedded directly in application code or config).

Auto-discovery only scrubs what this process's environment actually holds: if a secret a caller
expects to scrub (e.g. `ACLI_TOKEN`) isn't present here (a subagent whose environment wasn't
propagated, a rotated/blanked token), the run still exits 0 with zero redactions for it — a
silent gap in exactly the case this exists to cover. Pass `--require` to turn that into a loud
failure instead.

Commands:
  scrub <file> [<file> ...] [--vars A,B] [--require A,B] [--min-length N] [--report PATH] [--dry-run]
  self-test
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

DEFAULT_MIN_LENGTH = 6
_SECRET_WORDS = {
    "TOKEN", "SECRET", "SECRETS", "KEY", "KEYS", "APIKEY", "PASSWORD", "PASSWD",
    "CREDENTIAL", "CREDENTIALS", "PAT", "AUTH",
}


def discover_secret_vars(min_length: int) -> dict[str, str]:
    """Every env var whose name looks credential-shaped, with a value long enough to matter.

    Name-based, not value-based: a short value under a secret-shaped name is skipped (too
    likely to be a placeholder or boolean), but a long value under a non-secret-shaped name is
    left alone too, since scrubbing on value shape alone would redact ordinary long strings
    (paths, URLs, ids) that happen to appear in the environment.
    """
    found = {}
    for name, value in os.environ.items():
        if value and len(value) >= min_length and looks_like_secret_name(name):
            found[name] = value
    return found


def scrub_text(text: str, secrets: dict[str, str]) -> tuple[str, dict[str, int]]:
    """Replace every literal occurrence of each secret value with its redaction marker.

    Longest value first: if one secret's value is a substring of another's, redacting the
    longer one first consumes it whole so the shorter value's replacement never fragments it.
    """
    counts: dict[str, int] = {}
    for name, value in sorted(secrets.items(), key=lambda kv: len(kv[1]), reverse=True):
        occurrences = text.count(value)
        if occurrences:
            text = text.replace(value, f"<REDACTED:{name}>")
            counts[name] = occurrences
    return text, counts


def scrub_file(path: Path, secrets: dict[str, str], *, dry_run: bool = False) -> dict[str, int]:
    scrubbed, counts = scrub_text(path.read_text(encoding="utf-8"), secrets)
    if counts and not dry_run:
        # scrubbed already has every secret value replaced
        path.write_text(scrubbed, encoding="utf-8")
    return counts


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "self-test":
        _self_test()
        print("scrub_known_secrets self-test passed")
        return 0

    secrets = _resolve_secrets(args.vars, args.min_length)
    missing = _check_required(secrets, args.require)
    if missing:
        raise SystemExit(
            "scrub_known_secrets: required var(s) not present (or shorter than --min-length) "
            f"in this process's environment, so nothing would be scrubbed for them: {', '.join(missing)}"
        )

    report: dict[str, dict[str, int]] = {}
    for raw_path in args.files:
        path = Path(raw_path)
        if not path.is_file():
            raise SystemExit(f"scrub_known_secrets: not a file: {path}")
        counts = scrub_file(path, secrets, dry_run=args.dry_run)
        if counts:
            report[str(path)] = counts

    total = sum(c for counts in report.values() for c in counts.values())
    if args.report:
        full = {
            "vars_considered": sorted(secrets),  # var names only
            "dry_run": args.dry_run,
            "files": report,  # per-file {var_name: occurrence count}, never the value itself
            "total_redactions": total,
        }
        Path(args.report).write_text(json.dumps(full, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"files_scanned": len(args.files), "total_redactions": total, "dry_run": args.dry_run}))
    return 0


def _check_required(secrets: dict[str, str], required: list[str]) -> list[str]:
    return sorted(name for name in required if name not in secrets)


def _resolve_secrets(vars_arg: str | None, min_length: int) -> dict[str, str]:
    if not vars_arg:
        return discover_secret_vars(min_length)
    names = [v.strip() for v in vars_arg.split(",") if v.strip()]
    if not names:
        raise SystemExit("scrub_known_secrets: --vars must list at least one env var name")
    return {
        name: value
        for name in names
        if (value := os.environ.get(name, "")) and len(value) >= min_length
    }


def looks_like_secret_name(name: str) -> bool:
    """True when a variable or config key name is credential-shaped, by word rather than substring.

    Word-split so `ACLI_TOKEN` matches while `TOKENIZER_PATH` does not. Shared with
    `sy_config.py`, which uses it to refuse reading or storing a secret in a config file.
    """
    words = re.split(r"[^A-Za-z0-9]+", name.upper())
    return any(word in _SECRET_WORDS for word in words if word)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    scrub_parser = sub.add_parser("scrub", help="replace literal known-secret values with a redaction marker")
    scrub_parser.add_argument("files", nargs="+", help="files to scrub in place")
    scrub_parser.add_argument(
        "--vars", help="comma-separated env var names to treat as secrets (default: auto-discover by name)",
    )
    scrub_parser.add_argument(
        "--require", action="append", default=[],
        help="env var name that must resolve to a scrubbable value, or the run fails loudly instead of "
             "silently scrubbing nothing for it; repeatable",
    )
    scrub_parser.add_argument(
        "--min-length", type=int, default=DEFAULT_MIN_LENGTH, help="skip values shorter than this",
    )
    scrub_parser.add_argument("--report", help="also write the JSON summary to this path")
    scrub_parser.add_argument("--dry-run", action="store_true", help="report without modifying files")

    sub.add_parser("self-test", help="offline round-trip against placeholder env vars; no network")
    return parser


def _self_test() -> None:
    names = ("SY_TEST_TOKEN", "SY_TEST_SHORT_KEY", "SY_TEST_NOTASECRET", "SY_TEST_LONG_SECRET")
    saved = {k: os.environ.get(k) for k in names}
    os.environ["SY_TEST_TOKEN"] = "abcdef0123456789secretvalue"
    os.environ["SY_TEST_SHORT_KEY"] = "ab"
    os.environ["SY_TEST_NOTASECRET"] = "this-name-does-not-look-like-a-secret-but-is-long-enough"
    os.environ.pop("SY_TEST_LONG_SECRET", None)
    try:
        assert looks_like_secret_name("ACLI_TOKEN")
        assert looks_like_secret_name("GITHUB_TOKEN")
        assert looks_like_secret_name("AWS_SECRET_ACCESS_KEY")
        assert not looks_like_secret_name("ACLI_SITE")
        assert not looks_like_secret_name("PATH")
        assert not looks_like_secret_name("SY_SOME_DIRECTORY")

        secrets = discover_secret_vars(min_length=6)
        assert secrets.get("SY_TEST_TOKEN") == "abcdef0123456789secretvalue"
        assert "SY_TEST_SHORT_KEY" not in secrets, "below min-length must be skipped"
        assert "SY_TEST_NOTASECRET" not in secrets, "non-secret-shaped name must be skipped regardless of length"

        explicit = _resolve_secrets("SY_TEST_TOKEN,SY_TEST_SHORT_KEY,SY_TEST_NOT_SET", min_length=6)
        assert explicit == {"SY_TEST_TOKEN": "abcdef0123456789secretvalue"}, "explicit --vars still filters by length"

        assert _check_required(secrets, ["SY_TEST_TOKEN"]) == [], "a present var must satisfy --require"
        assert _check_required(secrets, ["SY_TEST_TOKEN", "SY_TEST_NOT_SET"]) == ["SY_TEST_NOT_SET"], (
            "an absent var must be reported so the caller fails loudly instead of silently scrubbing nothing"
        )

        text = "auth header: Bearer abcdef0123456789secretvalue\nsame again: abcdef0123456789secretvalue\n"
        scrubbed, counts = scrub_text(text, secrets)
        assert "abcdef0123456789secretvalue" not in scrubbed
        assert scrubbed.count("<REDACTED:SY_TEST_TOKEN>") == 2
        assert counts == {"SY_TEST_TOKEN": 2}

        long_value = "prefix-abcdef0123456789secretvalue-suffix"
        os.environ["SY_TEST_LONG_SECRET"] = long_value
        secrets_with_superstring = discover_secret_vars(min_length=6)
        scrubbed2, _ = scrub_text(f"only the long one: {long_value}\n", secrets_with_superstring)
        assert long_value not in scrubbed2
        assert "abcdef0123456789secretvalue" not in scrubbed2, "a shorter value embedded in a longer one must not survive"
        assert "<REDACTED:SY_TEST_LONG_SECRET>" in scrubbed2

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.txt"
            path.write_text(text, encoding="utf-8")
            assert scrub_file(path, secrets, dry_run=True) == {"SY_TEST_TOKEN": 2}
            assert "abcdef0123456789secretvalue" in path.read_text(encoding="utf-8"), "dry-run must not write"
            assert scrub_file(path, secrets) == {"SY_TEST_TOKEN": 2}
            assert "abcdef0123456789secretvalue" not in path.read_text(encoding="utf-8")
            assert scrub_file(path, secrets) == {}, "a second pass over an already-scrubbed file must find nothing"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    raise SystemExit(main())
