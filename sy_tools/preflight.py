"""Tracker-agnostic preflight cache: skip a repeated live liveness check once one has
recently succeeded for the same plugin build, tracker, and resolved config.

A live network read (a real read, not just a local-credential-status command) is the only way to tell
a present-but-dead credential from a working one. This module owns the fingerprint, cache, and TTL
mechanics so every adapter shares one cheap short-circuit; what "a real read" means for a given
tracker stays adapter-side, declared in that adapter's own configuration doc.

`var_names` carries only secret env var names: the fingerprint folds in the resolved Shipyard config
whole. `check` and `record` have one caller, the `preflight` MCP tool, which resolves both the tracker
and those var names from configuration itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

# Module scope is sound because the dependency runs one way: `sy_tools.config` never imports this.
from .config import fingerprint as config_fingerprint
from .config import repo_scratch_dir

DEFAULT_TTL_HOURS = 24.0


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

    A rotated var value, a changed setting, or a new plugin build therefore invalidates the cache
    automatically; the raw values never leave this process. An empty `var_names` is legitimate — an
    adapter may hold no credential in the environment at all, and the config fingerprint still covers
    its settings.
    """
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
        capture_output=True, text=True, check=False,
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


def cache_path() -> Path:
    """Where the cache lives: this repository's own resolved scratch directory.

    Scoped by the repository's own directory name rather than machine-global, so differently named
    repos never share one liveness verdict — two repos that happen to share a name still do, which is
    the same name-keying limitation `repo_scratch_dir` itself carries.
    """
    # Outside the checkout, not beside it, so the verdict outlives a `/sy:ship` worktree.
    # A function, not a constant: resolution shells out to git and reads the config layers.
    return repo_scratch_dir() / "sy" / "preflight-cache.json"


def _load_cache() -> dict:
    path = cache_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_cache(cache: dict) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
