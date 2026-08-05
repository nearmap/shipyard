"""Resolved Shipyard configuration: the resolver every `sy` tool call reads a setting through.

The layer chain, lowest precedence first: the shipped defaults, then the user-global, repo-committed
and repo-local `.shipyard/config.json` layers, deep-merged in that order. It resolves once and serves
every later read from memory for the life of the server process; `reload()` — the `reload_config`
tool — is the only way to re-read, so a config edit is picked up deliberately rather than racing a
call mid-flight.

Served alongside the values: the layer each key came from, the per-agent model and effort bindings
after floor clamping, the scratch directories, a digest of the whole resolved config, and
`validate()`'s report of every reason that config must be rejected.

Two invariants a caller can rely on. Every refusal is a `ConfigError`, including the environment
faults — an unrunnable `git`, an unreadable layer — that reaching a value at all depends on. And no
accessor here returns a credential-shaped value: a config layer is not where a secret lives.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .secrets import looks_like_secret_name

CONFIG_DIRNAME = ".shipyard"
CONFIG_FILENAME = "config.json"
LOCAL_FILENAME = "config.local.json"

MODEL_ORDER = ("haiku", "sonnet", "opus", "fable")
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
EFFORT_CAPABLE = frozenset({"sonnet", "opus", "fable"})

CANONICAL_COLUMNS = ("backlog", "ready", "in_progress", "in_review", "done")
REQUIRED_PATHS = (*(f"columns.{name}" for name in CANONICAL_COLUMNS), "tracker")
# Retired env var -> the config path that replaced it, for the conflict report. Tracker-specific names
# are not here: the selected adapter declares its own in skills/tracker/<name>/config-map.json.
LEGACY_ENV = {
    "SY_TRACKER": "tracker",
    "SY_WORKTREE_ROOT": "worktree.root",
    "SY_MEMORY_DIR": "memory.dir",
    "SY_DEBUG_EVALS": "debug.evals",
    "SY_CI_POLL_TIMEOUT": "ci.poll_timeout",
    "SY_BACKLOG_COLNAME": "columns.backlog",
    "SY_READY_COLNAME": "columns.ready",
    "SY_IN_PROGRESS_COLNAME": "columns.in_progress",
    "SY_IN_REVIEW_COLNAME": "columns.in_review",
    "SY_DONE_COLNAME": "columns.done",
    "SY_FRONTIER_MODEL": "models.tiers.frontier",
    "SY_FRONTIER_FALLBACK": "models.tiers.frontier_fallback",
    "SY_IMAGE_MODEL": "models.agents.img-inspector.model",
    "SY_DEBATE_MODEL": "models.agents.debate.model",
}
# Bounds every git query this module runs. They sit inside the MCP server on the path *every* tool call
# takes to a resolved value, so an unbounded wait is a hung server rather than a slow call. It bounds one
# subprocess, not one call: a cold `repo_scratch_dir()` reaches 13 of them in an ordinary checkout
# (measured), so a git answering just under the bound every time costs 13x this number.
GIT_TIMEOUT_SECONDS = 5


@dataclasses.dataclass(frozen=True)
class _Resolved:
    """One complete resolution: the merged values, each key's layer, and the repo it resolved for."""

    values: dict
    provenance: dict[str, str]
    root: Path


_STATE: _Resolved | None = None


class ConfigError(RuntimeError):
    """The configuration could not be resolved, or a caller asked for something it may not have."""


class _TransientConfigError(ConfigError):
    """A resolution failure a later attempt can still succeed at, so it is never memoized.

    A `ConfigError` to every caller; subclassed only so `repo_root` can decline to remember one.
    """


_REPO_ROOT: Path | None = None
_REPO_ROOT_REFUSAL: str | None = None
"""A settled root refusal's *message*, not the exception that carried it — see `repo_root`."""


def plugin_root() -> Path:
    """The plugin checkout: the session's own pointer when set, else this package's parent."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    return Path(root) if root else Path(__file__).resolve().parent.parent


def plugin_build() -> str:
    """The plugin's identity: its git HEAD when `CLAUDE_PLUGIN_ROOT` is a checkout, else its version.

    Reads the pointer directly rather than through `plugin_root()`: without one there is no build to
    identify, and `plugin_root()`'s package-parent fallback would report whichever checkout this file
    happens to sit in as the session's plugin build.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return "unknown"
    try:
        # Bounded and with `stdin` closed like every other git call here, and degrading to "unknown"
        # rather than refusing: `fingerprint()` folds this in, so raising would make a wedged git break
        # every caller of the digest instead of only widening it.
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
            stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        proc = None
    if proc is not None and proc.returncode == 0:
        return proc.stdout.strip()
    manifest = Path(root) / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        try:
            return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "unknown"))
        except (OSError, json.JSONDecodeError):
            return "unknown"
    return "unknown"


def repo_root() -> Path:
    """The consuming repository's root: Claude Code's own pointer when set, else derived from cwd.

    Both paths go through the same `git rev-parse`, so a pointer at a subdirectory resolves to the
    checkout root exactly as cwd does. Every failure is a separately named `ConfigError` — a pointer
    that names no checkout, a `git` that cannot be run, an unreadable working directory — so an
    unusable root is reportable rather than a silent fall-through to the shipped defaults alone.
    Resolved once per process, then memoized until `reset_cache()`.
    """
    global _REPO_ROOT, _REPO_ROOT_REFUSAL
    # A freshly built error from the remembered *message*: re-raising one cached instance extends its own
    # `__traceback__` in place, measured at ~400 frames and ~53KB of rendered traceback after 200 calls.
    if _REPO_ROOT_REFUSAL is not None:
        raise ConfigError(_REPO_ROOT_REFUSAL)
    # Memoized because every tool call reaches here: re-shelling out per call multiplied the wait a
    # caller sat through under a wedged git by the number of times that call resolved the root.
    if _REPO_ROOT is None:
        try:
            # Pointer first: unlike cwd it survives a `pixi run <task>` dispatch, which resets the
            # launched process's working directory to the manifest's own.
            project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
            if project_dir:
                root = _git_toplevel(Path(project_dir))
                if root is None:
                    raise ConfigError(
                        f"CLAUDE_PROJECT_DIR is {project_dir!r}, which is not a directory inside a git "
                        "checkout, so no repository configuration can be resolved from it. Point it at the "
                        "consuming repository, or unset it to resolve from the working directory."
                    )
                _REPO_ROOT = root
            else:
                try:
                    _REPO_ROOT = _git_toplevel(Path.cwd()) or Path.cwd()
                except OSError as exc:
                    raise ConfigError(
                        f"the working directory could not be read to derive the repository root: {exc}. Set "
                        "CLAUDE_PROJECT_DIR to the consuming repository, or restart the server from a "
                        "directory that still exists."
                    ) from None
        # A timeout said nothing about the repository, so it is not an answer to remember: memoizing one
        # git hiccup made a long-lived server refuse every later tool call. The retry is not free — one
        # `validate_config` resolves the root five times (measured), each paying a fresh timeout — and a
        # request-scoped cache is deliberately not added for a lifetime this module has nowhere else.
        except _TransientConfigError:
            raise
        except ConfigError as refusal:
            _REPO_ROOT_REFUSAL = str(refusal)
            raise
    return _REPO_ROOT


def _git_toplevel(start: Path) -> Path | None:
    """The resolved root of the git checkout containing `start`, or None when there is not one.

    None means one specific thing: git ran and reported no checkout, which the cwd path treats as "not
    in a checkout". A git that cannot be run, or that hangs, is a named `ConfigError` instead —
    collapsing either into None would resolve the shipped defaults alone with nothing naming the cause.
    """
    if not start.is_dir():
        return None
    try:
        # `stdin` is closed on every git query here: this runs inside the MCP server, whose own stdin is
        # the JSON-RPC transport, and a child that inherits it can consume a frame the server was to read.
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
            stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
        )
    # Only the timeout raises the transient subclass: a hang can come out differently next call, an
    # absent binary cannot, and `repo_root` memoizes accordingly.
    except subprocess.TimeoutExpired:
        raise _TransientConfigError(
            f"git did not resolve the repository root from {start} within {GIT_TIMEOUT_SECONDS}s and "
            "was killed. A wedged git binary cannot be waited out on the path every tool call takes to "
            "a resolved value, so resolution refuses instead. This refusal is not remembered: the next "
            "call tries again, so a momentary index lock costs one failed call rather than the session."
        ) from None
    except OSError as exc:
        raise ConfigError(
            f"git could not be run to resolve the repository root from {start}: {exc}. Every "
            "configuration layer above the shipped defaults lives under <root>/.shipyard/, so "
            "without git there is no root to read them from. Install git, or put it on PATH."
        ) from None
    out = proc.stdout.strip()
    return Path(out).resolve() if proc.returncode == 0 and out else None


def _git_common_dir(start: Path) -> Path | None:
    """The absolute shared `.git` directory of the checkout containing `start`, or None if there is none.

    None means only "git ran and reported no checkout", or answered a relative path. A git that cannot
    be run, or that hangs, is a named `ConfigError` exactly as in `_git_toplevel`.
    """
    if not start.is_dir():
        return None
    try:
        # `--path-format=absolute`: without it git answers a bare relative `.git` from a main checkout,
        # whose `.parent.name` is the empty string.
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
            stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise _TransientConfigError(
            f"git did not resolve the repository's shared git directory from {start} within "
            f"{GIT_TIMEOUT_SECONDS}s and was killed. A wedged git binary cannot be waited out on the "
            "path every tool call takes to a resolved value, so resolution refuses instead."
        ) from None
    except OSError as exc:
        raise ConfigError(
            f"git could not be run to resolve the repository's scratch directory from {start}: {exc}. "
            "Install git, or put it on PATH."
        ) from None
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        return None
    candidate = Path(out)
    # Absoluteness checked, not left to `.resolve()`: that would resolve a relative answer against *this
    # process's* cwd instead of `start` — fail-soft on the one boundary these callers exist to keep.
    return candidate.resolve() if candidate.is_absolute() else None


def _configured_worktree(common: Path) -> Path | None:
    """The absolute working tree `git config core.worktree` in `common`'s own config names, or None.

    A submodule's shared git dir sets this key; an ordinary checkout and a `--separate-git-dir` one do
    not set it at all and resolve to None, which `_logical_repo` handles.
    """
    # Both files, read by `--file` path: git relocates `core.worktree` from `<common>/config` to
    # `<common>/config.worktree` once `extensions.worktreeConfig` is on (`git sparse-checkout init`
    # turns it on and never reverts it), and a bare `git config --get` run from a *linked* worktree
    # suppresses the key entirely even though both files are the same shared, worktree-independent source.
    for filename in ("config.worktree", "config"):
        try:
            proc = subprocess.run(
                ["git", "config", "--file", str(common / filename), "--get", "core.worktree"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
                stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise _TransientConfigError(
                f"git did not read core.worktree from {common / filename} within {GIT_TIMEOUT_SECONDS}s "
                "and was killed. A wedged git binary cannot be waited out on the path every tool call "
                "takes to a resolved value, so resolution refuses instead."
            ) from None
        out = proc.stdout.strip()
        if proc.returncode == 0 and out:
            resolved = (common / out).resolve()
            if resolved.is_dir():
                return resolved
    return None


def _is_resolved_working_tree(candidate: Path, common: Path) -> bool:
    """Whether `candidate` is a genuine working tree whose own shared git directory is `common`.

    A real working tree answers `--is-inside-work-tree` and its own `--git-common-dir` agrees with
    `common`; a git-internal storage directory sitting at that path answers neither.
    """
    if not candidate.is_dir():
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
            stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise _TransientConfigError(
            f"git did not answer whether {candidate} is a working tree within {GIT_TIMEOUT_SECONDS}s and "
            "was killed. A wedged git binary cannot be waited out on the path every tool call takes to a "
            "resolved value, so resolution refuses instead."
        ) from None
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        return False
    return _git_common_dir(candidate) == common


def _same_directory(a: Path, b: Path) -> bool:
    """Whether `a` and `b` name the same directory on disk, by device and inode rather than spelling.

    A missing or unreadable directory is never "the same" as one that exists.
    """
    # Not a resolved-string comparison: `Path.resolve()` normalizes symlinks, `.` and `..` but not case,
    # so on a case-insensitive filesystem (APFS's default) one directory has two unequal spellings.
    try:
        stat_a = a.stat()
        stat_b = b.stat()
    except OSError:
        return False
    return (stat_a.st_dev, stat_a.st_ino) == (stat_b.st_dev, stat_b.st_ino)


def layers(root: Path) -> list[tuple[str, Path]]:
    """The layer chain, lowest precedence first."""
    return [
        ("user-global", Path.home() / CONFIG_DIRNAME / CONFIG_FILENAME),
        ("repo-committed", root / CONFIG_DIRNAME / CONFIG_FILENAME),
        ("repo-local", root / CONFIG_DIRNAME / LOCAL_FILENAME),
    ]


def resolve() -> tuple[dict, dict[str, str]]:
    """The hot resolved values and each key's originating layer. Resolves once, then serves memory."""
    global _STATE
    if _STATE is None:
        _STATE = _resolve_uncached()
    return _STATE.values, _STATE.provenance


def reset_cache() -> None:
    """Drop everything memoized, so the next read sees the environment and the files as they are now."""
    # All three, never just `_STATE`: the root is memoized too, so clearing half of it makes a reload
    # after a `CLAUDE_PROJECT_DIR` change re-read the layers of whichever repo resolved first.
    global _STATE, _REPO_ROOT, _REPO_ROOT_REFUSAL
    _STATE = None
    _REPO_ROOT = None
    _REPO_ROOT_REFUSAL = None


def reload() -> dict:
    """Drop the hot state and re-resolve from disk. Returns a summary, never a value."""
    before = fingerprint() if _STATE is not None else None
    reset_cache()
    values, provenance = resolve()
    after = fingerprint()
    return {
        "reloaded": True,
        "changed": before != after,
        "fingerprint": after,
        "previous_fingerprint": before,
        "keys": len(_flatten(values)),
        "layers_present": sorted({label for label in provenance.values()}),
    }


_UNSET = object()


def get(path: str, *, default: object = _UNSET) -> object:
    """One resolved value by dotted path. Refuses credential-shaped keys outright.

    An unknown key is an error unless a default is supplied, so a caller that knows a key is optional
    says so. The sentinel default is what lets `None` itself be a legitimate default.
    """
    if _looks_like_secret(path.replace(".", "_")):
        raise ConfigError(
            f"refusing to read {path!r}: it is credential-shaped, and secrets are never read from "
            "a config file. Keep them in the environment."
        )
    flat = _flatten(resolve()[0])
    if path not in flat:
        if default is not _UNSET:
            return default
        near = ", ".join(sorted(k for k in flat if k.startswith(path.split(".")[0]))) or "none"
        raise ConfigError(f"unknown config key {path!r}. Keys under that prefix: {near}")
    return flat[path]


def show() -> dict:
    """Every resolved value, the layer each key came from, the digest, and the layer chain on disk.

    Keyed `values`, `provenance`, `fingerprint`, and `layers` — the last a list of
    `{label, path, present}`, in precedence order, so an absent layer is still reported.

    Refuses outright, disclosing no value at all and naming only the offending keys, when the resolved
    configuration carries a credential-shaped key: a secret returned even once is a permanent part of
    whatever transcript asked for it.
    """
    values, provenance = resolve()
    credential_keys = sorted(key for key in _flatten(values) if _looks_like_secret(key.replace(".", "_")))
    if credential_keys:
        raise ConfigError(
            "refusing to show any value — the resolved configuration declares credential-shaped "
            f"key(s): {', '.join(credential_keys)}. Secrets are never read from a config file: keep "
            "them in the environment."
        )
    return {
        "values": values,
        "provenance": provenance,
        "fingerprint": fingerprint(),
        "layers": [
            {"label": label, "path": str(path), "present": path.is_file()}
            for label, path in layers(resolved_root())
        ],
    }


def agent_binding(name: str) -> dict:
    """The dispatch-time model and effort policy for one agent, after floor clamping."""
    values, provenance = resolve()
    agents = values.get("models", {}).get("agents", {})
    if name not in agents:
        raise ConfigError(f"unknown agent {name!r}. Known agents: {', '.join(sorted(agents))}")
    tiers = values.get("models", {}).get("tiers", {})
    floors = _load_json(plugin_root() / "config" / "floors.json").get(name, {})
    requested_model = _resolve_tier(agents[name].get("model"), tiers)
    requested_effort = agents[name].get("effort")
    model, model_clamped = _clamp(requested_model, _resolve_tier(floors.get("min_model"), tiers), MODEL_ORDER)
    effort, effort_clamped = _clamp(requested_effort, floors.get("min_effort"), EFFORT_ORDER)
    return {
        "agent": name,
        "model": model,
        "effort": effort,
        "model_requested": requested_model,
        "effort_requested": requested_effort,
        "model_clamped": model_clamped,
        "effort_clamped": effort_clamped,
        "source": provenance.get(f"models.agents.{name}.model", "shipped-default"),
    }


def scratch_dir(identifier: str) -> Path:
    """The ephemeral working directory for one identifier under `scratch.dir`, created if absent.

    The root is resolved, never re-derived, so a relocated `scratch.dir` moves every caller at once,
    and it must be absolute. An identifier that does not stay inside the resolved root is refused.
    """
    root = Path(str(get("scratch.dir")))
    if not root.is_absolute():
        raise ConfigError(
            f"scratch.dir resolved to {str(root)!r}, which is not absolute. A relative scratch.dir "
            "resolves against whatever directory happens to be the current process's cwd, which can "
            "put the write sandbox this backs inside the very checkout it must stay outside of. Set "
            "scratch.dir to an absolute path, or leave it unset to use the shipped default."
        )
    refusal = ConfigError(
        f"refusing to create a scratch directory for {identifier!r}: an identifier must be a "
        "relative name that stays inside the resolved scratch root."
    )
    try:
        relative = Path(identifier)
        candidate = root / relative
        # Checked on the resolved candidate, not on the string: `"."` and `""` have no path parts, so
        # every string-shaped guard passes them and the root itself would be returned — two identifiers
        # colliding, and a caller cleaning up what it was handed deleting every other one's data.
        # Resolving also catches a `..` mid-path and a symlink `mkdir(parents=True)` would follow out.
        contained = (
            bool(relative.parts)
            and bool(identifier.strip())
            and not relative.is_absolute()
            and ".." not in relative.parts
            and candidate.resolve().is_relative_to(root.resolve())
        )
    except (ValueError, OSError):
        raise refusal from None
    if not contained:
        raise refusal
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def repo_scratch_dir(start: Path | None = None) -> Path:
    """This repository's own scratch directory, resolved identically from any worktree of it.

    `start` names the directory to resolve from — a hook passes the event's own cwd, so guard and
    guarded resolve from one cwd concept; the default is the resolved repository root, which is what a
    direct in-session caller means. Refuses when the resolved directory contains, or is, any worktree
    of this repository.
    """
    origin = Path(start) if start is not None else repo_root()
    common = _git_common_dir(origin)
    if common is None:
        raise ConfigError(
            f"{str(origin)!r} is not a directory inside a git checkout, so no repository scratch "
            "directory can be resolved from it."
        )
    # Keyed on the *logical* repository, never `repo_root().name`: Claude Code exports
    # `CLAUDE_PROJECT_DIR` to hooks and stdio servers but not to a subagent's own Bash, so inside a
    # `/sy:ship` worktree a guard would key on the main checkout while the agent it guards keyed on the
    # worktree, and the guard would deny every write the agent believed was permitted.
    logical = _logical_repo(origin)
    directory = scratch_dir(logical.name)
    # Every worktree, not just `start`'s: a `PreToolUse` hook's cwd is the main checkout in almost every
    # run, not the worktree the tool call targets, so a `scratch.dir` overlapping some other, currently
    # inactive worktree would pass a check scoped to `start` and still expose that worktree's source.
    # `start`'s own tree is added separately because a *main* worktree has no entry in the registry.
    guarded_set = _all_worktrees(common, logical)
    checkout = _git_toplevel(origin)
    if checkout is not None:
        guarded_set.append(checkout)
    for guarded in guarded_set:
        if _same_directory(directory, guarded) or any(
            _same_directory(directory, parent) for parent in guarded.parents
        ):
            raise ConfigError(
                f"the resolved scratch directory {directory.resolve()} contains a worktree of this "
                f"repository ({guarded}). scratch.dir must not resolve to that worktree or an ancestor "
                "of it — every file inside it would then satisfy the containment check that is "
                "supposed to keep hunt out of it; check for a misconfigured scratch.dir or "
                "worktree.root in a committed or local .shipyard/config.json."
            )
    return directory


def _all_worktrees(common: Path, logical: Path) -> list[Path]:
    """The main checkout plus every *linked* worktree of this repository, from git's own bookkeeping.

    Read from `<common>/worktrees/`, independent of which worktree the invocation runs from. A `gitdir`
    record that is missing, unreadable or blank raises rather than silently guarding fewer worktrees
    than exist.
    """
    worktrees = [logical]
    worktrees_dir = common / "worktrees"
    if not worktrees_dir.is_dir():
        return worktrees
    for entry in sorted(worktrees_dir.iterdir()):
        gitdir_file = entry / "gitdir"
        if not gitdir_file.is_file():
            raise ConfigError(
                f"{str(gitdir_file)!r} is missing, so this worktree's own location cannot be "
                "determined and so cannot be guarded. Run `git worktree prune` if it was removed "
                "without `git worktree remove`, or `git worktree repair` if it was relocated."
            )
        try:
            raw = gitdir_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(
                f"{str(gitdir_file)!r} could not be read ({exc}), so this worktree's own location "
                "cannot be determined and so cannot be guarded."
            ) from None
        if not raw:
            raise ConfigError(
                f"{str(gitdir_file)!r} is blank, so this worktree's own location cannot be determined "
                "and so cannot be guarded — most likely a truncated or otherwise corrupted gitdir file."
            )
        pointed = Path(raw)
        # `git worktree add --relative-paths` writes this file relative to the entry's own directory, not
        # to `common` or any process's cwd, so resolving against another base would stat the wrong path.
        if not pointed.is_absolute():
            pointed = (entry / pointed).resolve()
        # Git's own reader strips an optional trailing "/.git" (git-worktree(1)'s DETAILS section
        # documents the bare directory form for hand-repairing a relocated worktree), so requiring the
        # suffix would refuse a record git itself accepts.
        worktree = pointed.parent if pointed.name == ".git" else pointed
        worktrees.append(worktree)
    return worktrees


def _logical_repo(start: Path) -> Path:
    """The directory holding the checkout's shared `.git`, or `start` itself when there is no checkout.

    A linked worktree resolves to its main checkout, which is what every per-repository derived default
    means: keyed on the worktree instead, `worktree.root` nests a second worktrees directory inside the
    first (`<repo>-worktrees/AM-1/../AM-1-worktrees`), which is where `/sy:ship` would put a slice
    worktree created from inside a build worktree.
    """
    common = _git_common_dir(start)
    if common is None:
        # `repo_root()`'s cwd path legitimately resolves a directory in no checkout, and resolution must
        # still produce a value there.
        return start
    # From the *shared* config, so no per-checkout detection is needed: a linked worktree of a submodule
    # reports no superproject at all, and detection keyed on the checkout would miss exactly the
    # worktrees `/sy:ship` creates. Keying on `common.parent` for a submodule would key every submodule
    # on the machine to the fixed string `modules`, sharing one scratch directory and one worktree root.
    configured = _configured_worktree(common)
    if configured is not None:
        return configured
    # Verified with git rather than assumed, because pattern-matching directory names (`modules`, nesting
    # depth, `--separate-git-dir`, `vendor/`-style grouping) does not generalize — as repeated fixes to
    # this function have shown. Unverified, `common.parent` names a git-internal storage directory rather
    # than a checkout whenever `core.worktree` is unresolvable, as after `git submodule deinit`.
    if _is_resolved_working_tree(common.parent, common):
        return common.parent
    # One tier further rather than refusing, which would make an ordinary `--separate-git-dir` or bare
    # checkout unusable: `common` is absolute, identical from every worktree of one repo and distinct
    # from every other repo's, so it is always safe to key on — only less readable.
    return common


def fingerprint() -> str:
    """Digest of every resolved value *and* the plugin build, for cache invalidation and drift detection.

    The build identifier is in the digest, not only the values, because `/sy:ship`'s mid-run drift guard
    compares this across a run to catch any config-relevant change — and `config/floors.json`'s model and
    effort floors and `agents/*.md`'s `effort:` frontmatter are config-relevant without being resolved
    values, so a digest over the values alone reported no drift when either changed under a running
    session. The build identifier moves on a plugin upgrade or a checkout's own commit; it does not move
    on an in-place edit under one build, so drift detection is build-granular, not file-granular.
    """
    values, _ = resolve()
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{plugin_build()}|{canonical}".encode()).hexdigest()[:16]


def validate() -> list[str]:
    """Every reason the resolved configuration must be rejected. Side-effect-free.

    A configuration that cannot be resolved at all — an unusable repository root, a layer that cannot be
    read or parsed — is returned as one error rather than raised: an exception escaping here would reach
    the operator as a traceback instead of the report this exists to produce.
    """
    # Ordering is load-bearing. The environment check needs nothing resolved, so it runs first and
    # survives a resolution failure. The retired-name checks absorb one into an empty config and would
    # then report every retired name as disagreeing with a key that "resolves to None", burying the cause.
    errors: list[str] = list(_outranking_env_conflicts())
    try:
        values, provenance = resolve()
    except ConfigError as exc:
        return [str(exc), *errors]

    # Everything past resolution reports its own read failure as an error too, warm or cold: the server
    # resolves once per process, so a layer edited into invalid JSON afterwards raised out of the one
    # tool whose whole job is diagnosing that fault.
    try:
        errors.extend(_legacy_env_conflicts())
        errors.extend(_post_resolution_violations(values, provenance))
    except ConfigError as exc:
        errors.append(str(exc))
    return errors


def _outranking_env_conflicts() -> list[str]:
    """A variable Claude Code lets outrank this resolver: an error, never an override.

    Presence only — the value is never read — and nothing resolved is needed.
    """
    if os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"):
        return [
            "CLAUDE_CODE_SUBAGENT_MODEL is set. It outranks the per-invocation model parameter and "
            "would silently reroute every agent off the model this config resolved. Unset it."
        ]
    return []


def _legacy_env_conflicts() -> list[str]:
    """Retired `SY_*` names still set in the environment, compared against what they now resolve to.

    Needs a resolved configuration for the values to compare against, and lets a `ConfigError` reach
    `validate()`'s guard rather than reporting fewer names than exist. The names themselves come from
    every shipped adapter rather than the resolved one, because this report is the whole migration
    worklist and a migration is read before the incoming tracker is selected.
    """
    errors: list[str] = []
    flat = _flatten(resolve()[0])
    legacy = _legacy_env_map()
    for name, path in sorted(legacy.items()):
        raw = os.environ.get(name)
        if raw in (None, ""):
            continue
        resolved = flat.get(path)
        if resolved in (None, "") or str(resolved) != raw:
            errors.append(
                f"{name} is set in the environment (to {raw!r}) and disagrees with {path}, which resolves to "
                f"{resolved!r}. Environment variables are reserved for secrets, so this is an error rather than an "
                f"override: put {raw!r} in {CONFIG_DIRNAME}/{CONFIG_FILENAME} as {path} and unset {name}."
            )
        else:
            errors.append(
                f"{name} is set in the environment and agrees with {path}, but is now redundant and must be unset: "
                f"leaving it set keeps two resolution paths alive for one key."
            )
    for name in sorted(os.environ):
        if re.fullmatch(r"SY_[A-Z0-9_]+", name) and name not in legacy and not name.startswith("SY_TEST_"):
            errors.append(
                f"{name} is set but is not a Shipyard setting. Every setting now lives in "
                f"{CONFIG_DIRNAME}/{CONFIG_FILENAME}; unset it or correct the name."
            )
    return errors


def _legacy_env_map() -> dict[str, str]:
    """Tracker-neutral legacy names plus every legacy name any shipped adapter declares.

    Every adapter's, not only the selected one's, because this map is what `validate()` reports a
    migration worklist from and a migration's *starting* state is the tracker being migrated away
    from. Resolved against the shipped default, a repo exporting the incoming adapter's legacy names
    had them fall through to the unknown-`SY_*` branch and be reported as "not a Shipyard setting" —
    telling the operator to delete the two values they were migrating.
    """
    return dict(LEGACY_ENV) | _all_adapters_legacy_env()


def _all_adapters_legacy_env() -> dict[str, str]:
    """The union of every shipped adapter's `legacy_env`, so the report is adapter-agnostic."""
    # Unioned the same way `_known_secret_env_names()` unions `secret_env`, and uncached for the same
    # reason `adapter_map()` is: `validate()` runs once per report, not per read.
    merged: dict[str, str] = {}
    tracker_dir = plugin_root() / "skills" / "tracker"
    if tracker_dir.is_dir():
        for config_map in sorted(tracker_dir.glob("*/config-map.json")):
            merged.update(_load_json(config_map).get("legacy_env", {}))
    return merged


def _post_resolution_violations(values: dict, provenance: dict[str, str]) -> list[str]:
    """Every check that re-reads a file after resolution: the layers, the adapter map, the floors."""
    errors: list[str] = []
    for label, path in layers(resolved_root()):
        if path.is_file():
            errors.extend(f"{label} layer {path}: {message}" for message in _layer_violations(path))

    flat = _flatten(values)
    tracker = flat.get("tracker")
    if tracker and str(tracker) not in _known_trackers():
        errors.append(
            f"tracker {tracker!r} (from {provenance.get('tracker')}) has no adapter under skills/tracker/. "
            f"Known trackers: {', '.join(_known_trackers()) or 'none'}."
        )
    for path_key in (*REQUIRED_PATHS, *adapter_map().get("required", [])):
        # Whitespace-only is unset, because that is how the value's own consumers read it. Testing
        # `in (None, "")` reported `columns.ready: "   "` as configured — schema-valid, no `minLength` —
        # and the session then failed its first status read: validation clean, broken on first use.
        if not str(flat.get(path_key) or "").strip():
            errors.append(f"{path_key} is required and unset.")
    # Presence only: the name is reported, the value is never read into a variable or a message.
    for name in adapter_map().get("secret_env", []):
        if not os.environ.get(name):
            errors.append(
                f"{name} is required by the {tracker!r} tracker and not set in the environment. "
                "Export it — never put it in a config file."
            )
    errors.extend(_validate_models(values, provenance))
    return errors


def _known_trackers() -> list[str]:
    """Every tracker that ships a `config-map.json`: the membership test, and the list a refusal names."""
    # Enumerated, never "does `skills/tracker/<value>/` exist": `"."` and `".."` name existing
    # directories, so the path-existence form validated clean and then silently skipped every `required`
    # and `secret_env` check, and `"../tracker/<name>"` traversed to a real map under a false name.
    tracker_dir = plugin_root() / "skills" / "tracker"
    return sorted(p.parent.name for p in tracker_dir.glob("*/config-map.json")) if tracker_dir.is_dir() else []


def adapter_map() -> dict:
    """The selected adapter's own config declaration, so one tracker's vocabulary never lands here."""
    tracker = _flatten(resolve()[0]).get("tracker")
    path = plugin_root() / "skills" / "tracker" / str(tracker) / "config-map.json"
    return _load_json(path) if path.is_file() else {}


def extra_secret_words() -> frozenset[str]:
    """`redaction.extra_words`, read off the resolved values directly."""
    # Not through `get()`: `get()` consults this list to decide whether a key is credential-shaped, so
    # routing it through `get()` would recurse into the gate it extends.
    words = _flatten(resolve()[0]).get("redaction.extra_words", [])
    return frozenset(str(w).upper() for w in words) if isinstance(words, list) else frozenset()


def env_present(name: str) -> bool:
    """Whether an environment variable is set and non-empty. The value itself is never returned or logged.

    An empty string counts as absent: exported empty is indistinguishable in effect from never exported.
    """
    try:
        return bool(os.environ.get(name))
    # An unpaired surrogate such as "\ud800" cannot be exported at all, so "not present" is both the safe
    # answer and the true one; unhandled, it reached `check_env`'s caller as an internal server error.
    except UnicodeEncodeError:
        return False


def resolved_root() -> Path:
    """The consuming repository the hot configuration resolved against.

    `repo_root()` re-derives; this reports what the live values were actually resolved from, so a caller
    acting *inside* the consumer's checkout cannot pick a different one than the configuration did.
    """
    resolve()
    assert _STATE is not None
    return _STATE.root


def _resolve_uncached() -> _Resolved:
    root = repo_root()
    values = _load_json(plugin_root() / "config" / "defaults.json")
    provenance = {key: "shipped-default" for key in _flatten(values)}
    for label, path in layers(root):
        if not path.is_file():
            continue
        layer = _load_json(path)
        for key in _flatten(layer):
            provenance[key] = label
        values = _deep_merge(values, layer)
    values.pop("$schema", None)
    _apply_derived_defaults(values, provenance, root)
    return _Resolved(values=values, provenance=provenance, root=root)


def _apply_derived_defaults(values: dict, provenance: dict[str, str], root: Path) -> None:
    if values.get("worktree", {}).get("root") in (None, ""):
        logical = _logical_repo(root)
        values.setdefault("worktree", {})["root"] = str(logical.parent / f"{logical.name}-worktrees")
        provenance["worktree.root"] = "derived-default"
    if values.get("memory", {}).get("dir") in (None, ""):
        values.setdefault("memory", {})["dir"] = str(Path.home() / ".claude" / "shipyard" / "memory")
        provenance["memory.dir"] = "derived-default"
    if values.get("scratch", {}).get("dir") in (None, ""):
        values.setdefault("scratch", {})["dir"] = str(Path.home() / ".claude" / "shipyard" / "scratch")
        provenance["scratch.dir"] = "derived-default"


def _validate_models(values: dict, provenance: dict[str, str]) -> list[str]:
    errors: list[str] = []
    models = values.get("models", {})
    tiers = models.get("tiers", {})
    for tier, alias in sorted(tiers.items()):
        if alias not in MODEL_ORDER:
            errors.append(f"models.tiers.{tier} is {alias!r}, which is not a known model.")
    floors = _load_json(plugin_root() / "config" / "floors.json")
    for name, binding in sorted(models.get("agents", {}).items()):
        source = provenance.get(f"models.agents.{name}.model", "shipped-default")
        alias = _resolve_tier(binding.get("model"), tiers)
        if alias not in MODEL_ORDER:
            errors.append(f"models.agents.{name}.model is {binding.get('model')!r} (from {source}), not a known model.")
            continue
        floor = floors.get(name, {})
        min_model = _resolve_tier(floor.get("min_model"), tiers)
        if min_model in MODEL_ORDER and MODEL_ORDER.index(str(alias)) < MODEL_ORDER.index(str(min_model)):
            errors.append(
                f"models.agents.{name}.model is {alias!r} (from {source}) but {name} has a floor of "
                f"{min_model!r}. Cost-scaling may raise a floor, never lower it."
            )
        effort = binding.get("effort")
        min_effort = floor.get("min_effort")
        if effort and min_effort and EFFORT_ORDER.index(effort) < EFFORT_ORDER.index(min_effort):
            errors.append(f"models.agents.{name}.effort is {effort!r} but {name} has a floor of {min_effort!r}.")
        if effort and alias not in EFFORT_CAPABLE:
            errors.append(
                f"models.agents.{name} declares effort {effort!r} but is bound to {alias!r}, which is not "
                "effort-capable: the declared effort would be dropped silently."
            )
    return errors


_SCHEMA: dict | None = None
_JSON_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _load_schema() -> dict:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = _load_json(plugin_root() / "config" / "schema.json")
    return _SCHEMA


def _matches_type(value: object, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected not in _JSON_TYPES:
        return True
    return isinstance(value, _JSON_TYPES[expected])


def _layer_violations(path: Path) -> list[str]:
    return _schema_violations(_load_schema(), _load_json(path), "")


def _schema_violations(node: dict, value: object, path: str) -> list[str]:
    """Every way `value` disagrees with schema `node`. Messages name keys, never values of secrets."""
    violations: list[str] = []
    types = node.get("type")
    if types is not None:
        allowed = [types] if isinstance(types, str) else types
        if not any(_matches_type(value, t) for t in allowed):
            return [f"{path!r} must be one of type {allowed}, got {type(value).__name__}"]

    if "enum" in node and value not in node["enum"]:
        violations.append(f"{path!r} must be one of {node['enum']}, got {value!r}")
    if isinstance(value, str):
        if "pattern" in node and not re.fullmatch(node["pattern"], value):
            violations.append(f"{path!r} does not match the required pattern {node['pattern']!r}")
        if "minLength" in node and len(value) < node["minLength"]:
            violations.append(f"{path!r} must be at least {node['minLength']} characters")
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in node and value < node["minimum"]:
        violations.append(f"{path!r} must be >= {node['minimum']}, got {value}")
    if isinstance(value, list) and "items" in node:
        for i, item in enumerate(value):
            violations.extend(_schema_violations(node["items"], item, f"{path}[{i}]"))
    if isinstance(value, dict):
        properties = node.get("properties", {})
        additional = node.get("additionalProperties", True)
        for key, sub_value in value.items():
            sub_path = f"{path}.{key}" if path else str(key)
            if key in properties:
                violations.extend(_schema_violations(properties[key], sub_value, sub_path))
            elif _looks_like_secret(sub_path.replace(".", "_")):
                violations.append(
                    f"{sub_path!r} is credential-shaped. Secrets are never read from a config file: "
                    "keep them in the environment."
                )
            elif additional is False:
                violations.append(f"{sub_path!r} is not a key config/schema.json declares.")
            elif isinstance(additional, dict):
                violations.extend(_schema_violations(additional, sub_value, sub_path))
            elif isinstance(sub_value, dict):
                violations.extend(_schema_violations({}, sub_value, sub_path))
    return violations


_KNOWN_SECRET_ENV: frozenset[str] | None = None


def _known_secret_env_names() -> frozenset[str]:
    """Every secret environment variable name any adapter declares, unioned across all adapters."""
    global _KNOWN_SECRET_ENV
    if _KNOWN_SECRET_ENV is None:
        names: set[str] = set()
        tracker_dir = plugin_root() / "skills" / "tracker"
        if tracker_dir.is_dir():
            for config_map in sorted(tracker_dir.glob("*/config-map.json")):
                names.update(_load_json(config_map).get("secret_env", []))
        _KNOWN_SECRET_ENV = frozenset(name.upper() for name in names)
    return _KNOWN_SECRET_ENV


def _looks_like_secret(name: str) -> bool:
    """Word heuristic, OR an exact match against any adapter's declared secret variable name."""
    if name.upper() in _known_secret_env_names():
        return True
    try:
        extra = extra_secret_words()
    except ConfigError:
        extra = frozenset()
    return looks_like_secret_name(name, extra=extra)


def _resolve_tier(value: object, tiers: dict) -> object:
    return tiers.get(value, value) if isinstance(value, str) else value


def _clamp(value: object, floor: object, order: tuple[str, ...]) -> tuple[object, bool]:
    if not isinstance(value, str) or not isinstance(floor, str):
        return value, False
    if value not in order or floor not in order:
        return value, False
    if order.index(value) < order.index(floor):
        return floor, True
    return value, False


def _deep_merge(base: dict, over: dict) -> dict:
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _flatten(values: dict, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def _load_json(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"missing required file {path}") from None
    except OSError as exc:
        # An unreadable layer (a bad mode, a dead symlink target) is a configuration fault like any other,
        # and `validate()`'s contract is to report one rather than raise on it.
        raise ConfigError(f"{path} could not be read: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a JSON object, not {type(loaded).__name__}")
    return loaded
