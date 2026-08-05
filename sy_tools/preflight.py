"""Tracker-agnostic preflight cache: skip a repeated live liveness check once one has
recently succeeded for the same plugin build, tracker, and resolved config.

A live network read (a real read, not just a local-credential-status command) is the only way to tell
a present-but-dead credential from a working one. This module owns the fingerprint, cache and TTL
mechanics so every adapter shares one cheap short-circuit; what "a real read" means for a given
tracker stays adapter-side, declared in that adapter's own configuration doc.

`var_names` carries only secret env var names — the fingerprint folds in the resolved config whole, and
`config.fingerprint()` covers the plugin build, so neither needs naming here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
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
    verified_at = cache.get("verified_at", 0)
    if not isinstance(verified_at, (int, float)):
        return False  # a hand-edited or corrupted cache is a miss, not a crash
    age = time.time() - verified_at
    return 0 <= age < ttl_seconds


def record(tracker: str, var_names: list[str]) -> None:
    """Record that a live check for this tracker+config just succeeded, right now."""
    _save_cache({
        "tracker": tracker,
        "fingerprint": fingerprint(tracker, var_names),
        "verified_at": time.time(),
    })


def fingerprint(tracker: str, var_names: list[str]) -> str:
    """Hash the tracker, the resolved config, and the values of `var_names`.

    A rotated var value, a changed setting or a new plugin build invalidates the cache; the raw values
    never leave this process. The plugin build is folded in by `config.fingerprint()` itself rather than
    hashed a second time here. An empty `var_names` is legitimate: an adapter may hold no credential in
    the environment at all, and the config fingerprint still covers its settings.
    """
    values = "|".join(f"{name}={os.environ.get(name, '')}" for name in sorted(var_names))
    return hashlib.sha256(f"{tracker}|{config_fingerprint()}|{values}".encode()).hexdigest()[:16]


def cache_path() -> Path:
    """Where the cache lives: this repository's own resolved scratch directory.

    Two repos sharing a directory name share one liveness verdict, as `repo_scratch_dir` itself does.
    """
    # Outside the checkout, not beside it, so the verdict outlives a `/sy:ship` worktree; resolved per
    # call rather than a module constant, since resolution shells out to git and reads config layers.
    return repo_scratch_dir() / "sy" / "preflight-cache.json"


def _load_cache() -> dict:
    path = cache_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}  # valid JSON but the wrong shape is still a miss


def _save_cache(cache: dict) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
