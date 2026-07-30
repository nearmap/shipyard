#!/usr/bin/env python3
"""Tracker-agnostic preflight cache: skip a repeated live liveness check once one has
recently succeeded for the same plugin build, tracker, and resolved config.

A live network read (a real read, not just a local-credential-status command) is the only
way to tell a present-but-dead credential from a working one, but it is not free, and the
setup it verifies changes rarely. This script owns only the fingerprint, cache, and TTL
mechanics so every adapter shares one cheap short-circuit; what "a real read" means for a
given tracker stays adapter-side, declared in that adapter's own configuration doc.

The fingerprint folds in the resolved Shipyard config, so a changed setting invalidates the cache
without the caller listing anything. `--vars` therefore carries only secret env var names, and is
optional for an adapter that holds no credential in the environment.

Commands:
  check --tracker <name> [--vars A,B] [--ttl-hours 24]   # exit 0: cached, skip the live check
                                                          # exit 2: no fresh cache, run it
  record --tracker <name> [--vars A,B]                    # call after the live check just passed
  self-test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

CACHE_PATH = Path(".scratch/sy/preflight-cache.json")
DEFAULT_TTL_HOURS = 24.0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "check":
        var_names = _split_vars(args.vars)
        ok = check(args.tracker, var_names, args.ttl_hours * 3600)
        print(json.dumps({"tracker": args.tracker, "cached_ok": ok}))
        return 0 if ok else 2
    if args.command == "record":
        record(args.tracker, _split_vars(args.vars))
        print(json.dumps({"tracker": args.tracker, "recorded": True}))
        return 0
    _self_test()
    print("sy_preflight self-test passed")
    return 0


def check(tracker: str, var_names: list[str], ttl_seconds: float) -> bool:
    """True when a prior `record` for this exact tracker+config is still within its TTL."""
    cache = _load_cache()
    if cache.get("tracker") != tracker or cache.get("fingerprint") != fingerprint(tracker, var_names):
        return False
    age = time.time() - cache.get("verified_at", 0)
    return 0 <= age < ttl_seconds


def record(tracker: str, var_names: list[str]) -> None:
    """Record that a live check for this tracker+config just succeeded, right now."""
    _save_cache({
        "tracker": tracker,
        "fingerprint": fingerprint(tracker, var_names),
        "verified_at": time.time(),
    })


def fingerprint(tracker: str, var_names: list[str]) -> str:
    """Hash the plugin build, the tracker, the resolved config, and the values of `var_names`.

    A changed var value (a rotated token), a changed setting (a switched project, a renamed
    column), or a new plugin build invalidates the cache automatically; the raw values never
    leave this process. `var_names` now carries only secrets, since everything else moved into
    the config file the config fingerprint covers.
    """
    # Imported here, not at module scope: sy_config imports plugin_build from this module, so a
    # top-level import either way is circular.
    from sy_config import fingerprint as config_fingerprint

    values = "|".join(f"{name}={os.environ.get(name, '')}" for name in sorted(var_names))
    digest = hashlib.sha256(f"{plugin_build()}|{tracker}|{config_fingerprint()}|{values}".encode()).hexdigest()
    return digest[:16]


def plugin_build() -> str:
    """The plugin's identity: its git HEAD when `CLAUDE_PLUGIN_ROOT` is a checkout, else its version."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return "unknown"
    proc = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if proc.returncode == 0:
        return proc.stdout.strip()
    manifest = Path(root) / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        try:
            return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "unknown"))
        except json.JSONDecodeError:
            return "unknown"
    return "unknown"


def _split_vars(raw: str) -> list[str]:
    """Secret env var names the liveness check depends on. Empty is legitimate: an adapter may hold
    no credential in the environment at all, and the config fingerprint still covers its settings."""
    return [v.strip() for v in raw.split(",") if v.strip()]


def _load_cache() -> dict:
    if not CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="exit 0 if a fresh cached liveness check covers this config, else exit 2")
    c.add_argument("--tracker", required=True)
    c.add_argument("--vars", default="", help="comma-separated secret env var names the liveness check depends on")
    c.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    r = sub.add_parser("record", help="record that a live check for this tracker+config just succeeded")
    r.add_argument("--tracker", required=True)
    r.add_argument("--vars", default="")
    sub.add_parser("self-test", help="offline round-trip against a temporary cache path; no network")
    return parser


def _self_test() -> None:
    """Offline round-trip with placeholder tracker/var names — this script has no notion of
    what a "tracker" is beyond an opaque cache-key string, so the test never needs a real one."""
    global CACHE_PATH
    original = CACHE_PATH
    saved_env = {k: os.environ.get(k) for k in ("SY_TEST_VAR_A", "SY_TEST_VAR_B", "CLAUDE_PLUGIN_ROOT")}
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        CACHE_PATH = Path(tmp) / "sy" / "preflight-cache.json"
        os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        try:
            assert plugin_build() == "unknown", "no CLAUDE_PLUGIN_ROOT must resolve to a stable placeholder"
            os.environ["SY_TEST_VAR_A"] = "a@b.c"
            os.environ["SY_TEST_VAR_B"] = "tok-1"
            v = ["SY_TEST_VAR_A", "SY_TEST_VAR_B"]
            assert not check("trackerA", v, ttl_seconds=3600), "empty cache must miss"
            record("trackerA", v)
            assert check("trackerA", v, ttl_seconds=3600), "fresh record must hit"
            assert not check("trackerB", v, ttl_seconds=3600), "wrong tracker must miss"
            os.environ["SY_TEST_VAR_B"] = "tok-2"
            assert not check("trackerA", v, ttl_seconds=3600), "changed value must miss"
            os.environ["SY_TEST_VAR_B"] = "tok-1"
            assert check("trackerA", v, ttl_seconds=3600), "restored value must hit again"
            assert not check("trackerA", v, ttl_seconds=0), "zero TTL must always miss"
            fp_a = fingerprint("trackerA", v)
            fp_b = fingerprint("trackerA", list(reversed(v)))
            assert fp_a == fp_b, "var order must not affect the fingerprint"
        finally:
            CACHE_PATH = original
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    raise SystemExit(main())
