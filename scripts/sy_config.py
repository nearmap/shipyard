#!/usr/bin/env python3
"""One-time conversion of a legacy `env`-block configuration into Shipyard's config JSON.

Shipyard used to be configured through the `env` block of a repo's `.claude/settings.json`, which put
a credential and a board column name in the same mechanism with nothing but prose separating them.
Settings now live in `<repo>/.shipyard/`, environment variables are reserved for secrets, and a
config-shaped variable left in the environment is an error rather than a silent override — silent
precedence is what made the old boundary illegible. A repo still carrying the old block therefore
needs its non-secret variables lifted across and its credentials left exactly where they are.
`migrate` is that lift, and it is the only mechanic here: every *read* of the resolved configuration
is an MCP tool over `sy_tools/config.py`, which is the resolver of record.

Resolution is duplicated here rather than imported from `sy_tools.config` because of where this runs.
`migrate` is step 1b of skills/init-repo/SKILL.md, invoked as
`python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" migrate` — bare python from the plugin install
root, with `sys.path[0]` at `scripts/` rather than the package root, on a repo that by definition has
not been configured yet. It resolves the layer chain because the conversion depends on it: the
adapter half of the legacy map and the derived defaults both come from resolved values.

Commands:
  migrate --settings <path> [--out <path>]   convert a legacy settings.json env block into config JSON

Layer chain, lowest precedence first: ~/.shipyard/config.json, <repo>/.shipyard/config.json
(committed), <repo>/.shipyard/config.local.json (gitignored).

The governing reference is docs/configuration.md; the machine-readable schema is config/schema.json.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

CONFIG_FILENAME = "config.json"
LOCAL_FILENAME = "config.local.json"
CONFIG_DIRNAME = ".shipyard"
SCHEMA_URL = "https://raw.githubusercontent.com/nearmap/shipyard/main/config/schema.json"
# Every subprocess this module runs is a git query, and all four call sites are bounded by this:
# `rev-parse --show-toplevel` (`_git_toplevel`), `rev-parse --git-common-dir` (`_git_common_dir`),
# `config --get core.worktree` (`_configured_worktree`) and `rev-parse --is-inside-work-tree`
# (`_is_resolved_working_tree`). Generous for a local rev-parse under load, short of anything an
# operator would sit through on a one-shot bootstrap command — but a bound per subprocess, not per
# invocation: only root resolution is memoized (`repo_root()` remembers its refusal as well as its
# answer), while the other three sites are resolved fresh every time, so a cold `migrate` reaches six
# git subprocesses in an ordinary checkout (one root resolution, plus five for the `worktree.root`
# derived default's own `_logical_repo` — measured, not estimated). A git slow enough to answer just
# under the bound every time therefore costs 6x this number, where a *wedged* one refuses at the first
# site it reaches. What is bounded is the git subprocess and nothing else — `layers()` and `_load_json`
# read `~/.shipyard/config.json` and the repo's own layers with no bound at all, so a home directory
# that has stopped answering hangs there whatever this says. Same shape and number as
# `sy_tools/config.py::GIT_TIMEOUT_SECONDS`, which carries the accounting for the resolver on the MCP
# server's hot path, and as `sy_tools/secrets.py::SCANNER_TIMEOUT_SECONDS`.
GIT_TIMEOUT_SECONDS = 5

_RESOLVED: tuple[dict, dict[str, str]] | None = None
_REPO_ROOT: Path | None = None
_REPO_ROOT_REFUSAL: SystemExit | None = None

# Legacy env var -> config path, which is the whole of what `migrate` converts. Tracker-specific names
# are not here: the selected adapter declares its own in skills/tracker/<name>/config-map.json,
# so this script never needs to know one tracker's vocabulary from another's.
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
# The retired name for `tracker` specifically. `migrate` reads it out of the block it is converting to
# decide whose adapter names to migrate; derived from the map above so the two cannot drift apart.
_TRACKER_ENV = next(name for name, path in LEGACY_ENV.items() if path == "tracker")


def main(argv: list[str] | None = None) -> int:
    """Run one `migrate` invocation and return its process exit code."""
    args = _build_parser().parse_args(argv)
    return _migrate(Path(args.settings), Path(args.out) if args.out else None)


def _build_parser() -> argparse.ArgumentParser:
    """The command line. `migrate` stays a named subcommand, not bare flags, because that is the
    spelling skills/init-repo/SKILL.md and docs/configuration.md both document."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("migrate", help="convert a legacy settings.json env block into config JSON")
    m.add_argument("--settings", required=True, help="path to the settings.json holding the legacy env block")
    m.add_argument("--out", help="write here instead of stdout")
    return parser


def _migrate(settings_path: Path, out_path: Path | None) -> int:
    """Convert a legacy settings.json `env` block into config JSON, leaving secrets behind.

    Resolution is forced up front so that a failure to resolve refuses the whole conversion. Half the
    legacy map is one adapter's own `legacy_env` block, and *which* adapter is the block's own answer
    (`_migrating_tracker`), never the currently resolved one: `migrate` runs at step 1b of
    skills/init-repo/SKILL.md, before step 2 resolves a tracker, so pre-migration the resolved value is
    whatever the shipped default says. Reading the adapter map from that dropped every
    `tracker_config.*` variable in a block migrating to a different tracker and still exited 0 with a
    file that looked complete. A best-effort `{}` degradation is right for a caller that only wants
    tracker metadata and wrong here for the same reason, so a tracker naming no adapter is refused
    rather than silently costing the adapter's half of the map.

    An `--out` that already exists is merged into, not overwritten. The documented flow points `--out`
    straight at `.shipyard/config.json` and SKILL.md's own instruction for that file is to "preserve
    every existing key rather than overwriting"; docs/configuration.md treats a config file coexisting
    with a lingering `env` block as a real state, so truncating it destroyed exactly the keys the run
    before it had written. Migrated values win on conflict — that is the point of running `migrate` —
    which is the same precedence `_deep_merge` gives a higher layer, and a destination that cannot be
    parsed is a refusal from `_load_json` rather than a file this command overwrites blind. The write
    itself goes through `_write_atomically`, so a merge that cannot be written leaves the destination
    exactly as it was rather than truncated mid-value.
    """
    env = _load_json(settings_path).get("env", {})
    if not env:
        raise SystemExit(f"sy_config: {settings_path} has no env block to migrate")
    resolve()  # see the docstring: refuse loudly rather than migrate a partial map
    tracker = _migrating_tracker(env, settings_path)
    mapping = dict(LEGACY_ENV) | _load_json(_adapter_map_path(tracker)).get("legacy_env", {})
    config: dict = {"$schema": SCHEMA_URL}
    # Two different reasons a variable stays behind, reported separately: a credential belongs in the
    # environment and is *meant* to stay, while an unmapped name is a typo or a stale setting nothing
    # will read again. Collapsing them read as "these are fine" for both.
    secrets: list[str] = []
    unmapped: list[str] = []
    for name, value in sorted(env.items()):
        if _looks_like_secret(name):
            secrets.append(name)
        elif path := mapping.get(name):
            _assign(config, path, _coerce(value))
        else:
            unmapped.append(name)
    summary = {
        "tracker": tracker,
        "migrated": sorted(k for k in _flatten(config) if k != "$schema"),
        "secrets_left_in_env": secrets,
        "unmapped_and_not_migrated": unmapped,
    }
    if out_path:
        existing = _load_json(out_path) if out_path.is_file() else {}
        merged = _deep_merge(existing, config)
        _write_atomically(out_path, json.dumps(merged, indent=2, sort_keys=True) + "\n")
        preserved = sorted(set(_flatten(existing)) - set(_flatten(config)))
        print(json.dumps({"written": str(out_path), **summary, "preserved": preserved}))
    else:
        sys.stdout.write(json.dumps(config, indent=2, sort_keys=True) + "\n")
        # stdout carries the config alone so it stays pipeable; the summary is the same either way.
        print(json.dumps(summary), file=sys.stderr)
    return 0


def _write_atomically(path: Path, text: str) -> None:
    """Write `text` to `path` through a sibling temporary file and one `os.replace`, or refuse by name.

    The same temp-write-then-replace pattern as `sy_tools/memory.py::_atomic_write`, for a stronger
    reason: `migrate --out` is pointed straight at a repo's `.shipyard/config.json` by the documented
    flow, and a plain `write_text` truncates the destination before it writes a byte. A write that then
    fails partway — a full disk, a quota, a file-size limit — left that file cut off mid-value and
    unparseable, so every later read of it was a `_load_json` refusal, while the operator saw a raw
    `OSError` traceback that said nothing about the destination now being broken. Replacing onto the
    destination is atomic on POSIX, so it either carries the whole merge or is untouched, and any
    `OSError` (including an `--out` that names a directory, which `os.replace` refuses) arrives as this
    module's own refusal. The partial temporary file is removed, so a failed run leaves nothing behind.

    A destination with no filename at all — `--out .`, `--out /` — is refused before anything is
    derived from it. It never reached the `OSError` guard: deriving the sibling temporary name from an
    empty basename raises `ValueError`, so the one case this refusal names first (an `--out` that is a
    directory) escaped as a raw traceback instead of the refusal itself.
    """
    def refuse(cause: str) -> SystemExit:
        return SystemExit(
            f"sy_config: {path} could not be written: {cause}. Nothing was migrated — the destination is "
            "still exactly as it was before this run, so fix the cause and run migrate again."
        )

    if not path.name:
        raise refuse("it names a directory rather than a file, so there is no config file to write")
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise refuse(str(exc)) from None


def _migrating_tracker(env: dict, settings_path: Path) -> str:
    """The tracker this `env` block is migrating *to*, refused unless it names a shipped adapter.

    Checked against the enumerated adapter names rather than by testing a path built from the value, so
    a name carrying path separators cannot address anything outside `skills/tracker/`.
    """
    tracker = str(env.get(_TRACKER_ENV) or _flatten(resolve()[0]).get("tracker") or "")
    if tracker not in _known_trackers():
        raise SystemExit(
            f"sy_config: refusing to migrate {settings_path}: tracker {tracker!r} names no adapter under "
            f"skills/tracker/, so any adapter-specific variable in this env block has no config key to "
            f"migrate into and would be dropped silently from a file that looked complete. Correct the "
            f"tracker name — known trackers: {', '.join(_known_trackers()) or 'none'}."
        )
    return tracker


def _assign(target: dict, path: str, value: object) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def _coerce(raw: str) -> object:
    if raw.strip().lower() in {"1", "true", "yes"}:
        return True
    if raw.strip().lower() in {"0", "false", "no"}:
        return False
    if re.fullmatch(r"-?\d+", raw.strip()):
        return int(raw.strip())
    return raw


def resolve() -> tuple[dict, dict[str, str]]:
    """Deep-merge every present layer over the shipped defaults, tracking each key's origin.

    Memoized: resolution shells out to git and reads up to four files, and one `migrate` asks for it
    from three places (its own upfront call, the migrating tracker, the extra-word list).
    `reset_cache()` is the only way to re-read.
    """
    global _RESOLVED
    if _RESOLVED is None:
        _RESOLVED = _resolve_uncached()
    return _RESOLVED


def reset_cache() -> None:
    """Drop the memoized resolution, so the next read sees the files as they are on disk now."""
    global _RESOLVED, _REPO_ROOT, _REPO_ROOT_REFUSAL
    _RESOLVED = None
    _REPO_ROOT = None
    _REPO_ROOT_REFUSAL = None


def _resolve_uncached() -> tuple[dict, dict[str, str]]:
    values = _load_json(plugin_root() / "config" / "defaults.json")
    provenance = {key: "shipped-default" for key in _flatten(values)}
    for label, path in layers():
        if not path.is_file():
            continue
        layer = _load_json(path)
        for key in _flatten(layer):
            provenance[key] = label
        values = _deep_merge(values, layer)
    values.pop("$schema", None)
    _apply_derived_defaults(values, provenance)
    return values, provenance


def layers() -> list[tuple[str, Path]]:
    """The layer chain, lowest precedence first."""
    root = repo_root()
    return [
        ("user-global", Path.home() / CONFIG_DIRNAME / CONFIG_FILENAME),
        ("repo-committed", root / CONFIG_DIRNAME / CONFIG_FILENAME),
        ("repo-local", root / CONFIG_DIRNAME / LOCAL_FILENAME),
    ]


def plugin_root() -> Path:
    """The plugin checkout, from the environment when a session set it, else this script's parent."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    return Path(root) if root else Path(__file__).resolve().parent.parent


def repo_root() -> Path:
    """The consuming repository's root: the session's own pointer when set, else derived from cwd.

    `CLAUDE_PROJECT_DIR` wins for the same reason the MCP server's resolver honours it — Claude Code
    sets it for every hook and stdio server it launches, and it survives a dispatch that resets the
    working directory. Both resolvers must agree on it or the same key resolves two ways: a
    worktree-local `.shipyard/config.local.json` would be read by one and invisible to the other.

    Both paths go through the same `git rev-parse`, so a pointer at a subdirectory resolves to the
    checkout root, and a pointer naming no checkout is refused rather than silently resolving the
    shipped defaults with no layer above them — which for `migrate` means converting a block against
    the wrong repo's layers. A `git` that cannot be run is a separate refusal from `_git_toplevel`
    itself, so it is never misreported as the pointer's fault and reaches the cwd path too, which has
    no pointer to blame. A working directory that can no longer be read at all — deleted or made
    inaccessible under a process that inherited it — is a third named refusal, mirroring
    `sy_tools/config.py::repo_root`, rather than a raw traceback out of `Path.cwd()`.

    A refusal is memoized alongside an answer, so root resolution runs git at most once per process:
    the same contract `_RESOLVED` already has, and nothing re-reads until `reset_cache()` says to. The
    sibling `sy_tools/config.py::repo_root` carries the fuller accounting of why the refusal is
    remembered and not only the answer.
    """
    global _REPO_ROOT, _REPO_ROOT_REFUSAL
    if _REPO_ROOT_REFUSAL is not None:
        raise _REPO_ROOT_REFUSAL
    if _REPO_ROOT is None:
        try:
            project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
            if project_dir:
                root = _git_toplevel(Path(project_dir))
                if root is None:
                    raise SystemExit(
                        f"sy_config: CLAUDE_PROJECT_DIR is {project_dir!r}, which is not a directory inside a "
                        "git checkout, so no repository configuration can be resolved from it. Point it at the "
                        "consuming repository, or unset it to resolve from the working directory."
                    )
                _REPO_ROOT = root
            else:
                try:
                    _REPO_ROOT = _git_toplevel(Path.cwd()) or Path.cwd()
                except OSError as exc:
                    raise SystemExit(
                        f"sy_config: the working directory could not be read to derive the repository root: "
                        f"{exc}. Set CLAUDE_PROJECT_DIR to the consuming repository, or run from a directory "
                        "that still exists."
                    ) from None
        except SystemExit as refusal:
            _REPO_ROOT_REFUSAL = refusal
            raise
    return _REPO_ROOT


def _git_toplevel(start: Path) -> Path | None:
    """The resolved root of the git checkout containing `start`, or None when there is not one.

    A `git` that cannot be *run* at all is refused here rather than folded into None, for the reasons
    `sy_tools/config.py::_git_toplevel` gives — None means "git ran and reported no checkout", which
    the cwd path legitimately answers with a cwd fallback. Refused *here* so that one guard covers
    every path to the repository root: `resolve()` and `layers()` reach it with nothing between them
    and the subprocess, and each used to traceback raw on a missing binary.

    A git that *hangs* is refused the same way, which needs `timeout=` and nothing else can substitute
    for it: no `except` clause catches a subprocess that simply does not return, and an operator running
    the one-shot bootstrap conversion against a half-migrated repo learns nothing from a command that
    never comes back. What the bound closes is this one subprocess — a `git` binary that does not
    return, a git wrapper or credential helper that waits on something. Reading the configuration layers
    themselves is not bounded by it or by anything else (`layers()`, `_load_json`), so a home directory
    that has stopped answering hangs there instead; bounding an arbitrary local file read needs a
    watchdog thread and a design of its own, and there is no evidence of that happening to warrant one.

    `stdin` is closed for the reason `sy_tools/config.py::_git_toplevel` closes its own: a child that
    inherits the caller's stdin can consume input the caller was going to read.
    """
    if not start.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
            stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"sy_config: git did not resolve the repository root from {start} within "
            f"{GIT_TIMEOUT_SECONDS}s and was killed. A wedged git binary cannot be waited out here, so "
            "resolution refuses instead."
        ) from None
    except OSError as exc:
        raise SystemExit(
            f"sy_config: git could not be run to resolve the repository root from {start}: {exc}. "
            "Every configuration layer above the shipped defaults lives under <root>/.shipyard/, so "
            "without git there is no root to read them from. Install git, or put it on PATH."
        ) from None
    out = proc.stdout.strip()
    return Path(out).resolve() if proc.returncode == 0 and out else None


def _git_common_dir(start: Path) -> Path | None:
    """The absolute shared `.git` directory of the checkout containing `start`, or None if there is none.

    `--path-format=absolute` is not decoration: without it git answers a bare relative `.git` from a
    main checkout, whose `.parent.name` is the empty string. Absoluteness is therefore checked
    explicitly rather than left to `.resolve()`, which would silently resolve a relative answer
    against *this process's* cwd instead of `start` — fail-soft on the one boundary this exists to
    keep. A relative answer is None, the same as no checkout.

    A `git` that cannot be run is refused by name here for the reasons `_git_toplevel` gives, and one
    that *hangs* is bounded and refused for those same reasons. None means only "git ran and reported no
    checkout", which the callers act on themselves. `stdin` is closed as `_git_toplevel` closes its own.
    """
    if not start.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
            stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"sy_config: git did not resolve the repository's shared git directory from {start} within "
            f"{GIT_TIMEOUT_SECONDS}s and was killed. A wedged git binary cannot be waited out here, so "
            "resolution refuses instead."
        ) from None
    except OSError as exc:
        raise SystemExit(
            f"sy_config: git could not be run to resolve the repository's shared git directory from {start}: "
            f"{exc}. Install git, or put it on PATH."
        ) from None
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        return None
    candidate = Path(out)
    return candidate.resolve() if candidate.is_absolute() else None


def _configured_worktree(common: Path) -> Path | None:
    """The absolute working tree `git config core.worktree` in `common`'s own config names, or None.

    A submodule's shared git dir (`<super>/.git/modules/<name>`) sets `core.worktree` to the relative
    path back to the submodule's own working tree — normally in `<common>/config`, but git relocates it
    to `<common>/config.worktree` the moment `extensions.worktreeConfig` is turned on, which
    `git sparse-checkout init` does inside the submodule and never reverts on `sparse-checkout
    disable`. Both files are read directly by path with `--file` rather than a bare `git config
    --get`, because a bare query run from a *linked* worktree of the submodule suppresses
    `core.worktree` entirely (git treats it as belonging only to the main worktree's own per-worktree
    config) even though both files themselves are the same shared, worktree-independent source either
    way. `--separate-git-dir` checkouts do not set this key at all: an ordinary checkout, and a plain
    `--separate-git-dir` one, both fall through to `None` here, and `_logical_repo` falls back to
    `common.parent` for those, once verified — which can still collide if multiple such checkouts'
    git-dirs are deliberately colocated under one shared parent directory, the same class of
    collision as two ordinary same-named repos elsewhere on the machine, and out of scope for the
    same reason.

    Each read is bounded and `stdin` is closed for the same reasons the sibling resolvers bound and
    close their own.
    """
    for filename in ("config.worktree", "config"):
        try:
            proc = subprocess.run(
                ["git", "config", "--file", str(common / filename), "--get", "core.worktree"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
                stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(
                f"sy_config: git did not read core.worktree from {common / filename} within "
                f"{GIT_TIMEOUT_SECONDS}s and was killed. A wedged git binary cannot be waited out here, "
                "so resolution refuses instead."
            ) from None
        out = proc.stdout.strip()
        if proc.returncode == 0 and out:
            resolved = (common / out).resolve()
            if resolved.is_dir():
                return resolved
    return None


def _is_resolved_working_tree(candidate: Path, common: Path) -> bool:
    """Whether `candidate` is a genuine working tree whose own shared git directory is `common`.

    `_logical_repo` falls back to `common.parent` as `candidate` when `core.worktree` cannot be
    read; that guess is right for an ordinary checkout (`common` is `<repo>/.git`, so `common.parent`
    is the repo itself) and wrong for a git-internal storage directory that happens to sit at that
    path — a submodule's `.git/modules/<name>`, a nested submodule's
    `.git/modules/<outer>/modules/<inner>`, a `--separate-git-dir` superproject's own detached gitdir,
    or any future layout with the same shape. None of those are themselves a working tree at all.
    Settled by asking git directly rather than pattern-matching directory names: a real working tree
    answers `--is-inside-work-tree` and its own `--git-common-dir` agrees with `common`; a storage
    directory answers neither.

    The question is bounded and `stdin` is closed for the same reasons the sibling resolvers bound and
    close their own.
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
        raise SystemExit(
            f"sy_config: git did not answer whether {candidate} is a working tree within "
            f"{GIT_TIMEOUT_SECONDS}s and was killed. A wedged git binary cannot be waited out here, so "
            "resolution refuses instead."
        ) from None
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        return False
    return _git_common_dir(candidate) == common


def _logical_repo(start: Path) -> Path:
    """The directory holding the checkout's shared `.git`, or `start` itself when there is no checkout.

    A linked worktree resolves to its main checkout, which is what every per-repository derived
    default means. Keyed on the worktree instead, `worktree.root` nests a second worktrees directory
    inside the first (`<repo>-worktrees/AM-1/../AM-1-worktrees`), which is where `/sy:ship` would put
    a slice worktree it created from inside a build worktree.

    A submodule's `--git-common-dir` resolves under the superproject's `.git/modules/`, whose parent
    directory name is the fixed string `modules` for every submodule on the machine; keyed on that,
    two unrelated submodules would share one `worktree.root`. The shared git dir names the submodule's
    own working tree in its `core.worktree`, so `_configured_worktree` reads it from the *shared* config
    and needs no per-checkout detection — which matters because a linked worktree of a submodule reports
    no superproject at all, and so any detection keyed on the checkout would miss exactly the worktrees
    `/sy:ship` itself creates.

    When `core.worktree` cannot be resolved at all (`git submodule deinit`, which clears it from both
    config files while leaving `.git/modules/<name>` itself in place and any of the submodule's own
    linked worktrees checked out and healthy), the naive fallback of `common.parent` is verified with
    git itself rather than assumed: pattern-matching directory names (`modules`, nesting depth,
    `--separate-git-dir`, `vendor/`-style grouping, and whatever shape comes after those) does not
    generalize, as repeated fixes to this exact function have shown. `common.parent` is used only when
    it is itself a real working tree whose own shared git directory is `common` — true for an ordinary
    checkout, false for every git-internal storage directory this or a future git layout might produce
    at that path (a submodule's own internal storage, a `--separate-git-dir` or bare checkout's
    detached gitdir folder). Refusing there would make an ordinary `--separate-git-dir` or bare
    checkout — nothing at all like the transient, self-inflicted deinit state this exists to guard
    against — unusable outright, so instead this falls back one tier further: `common` itself. `common`
    is an absolute, resolved path that is by construction identical from every worktree of one repo and
    distinct from every other repo's, so it is always safe to key on even though it is sometimes less
    readable than a checkout's own directory name (`.git/modules/<name>` for an otherwise-unresolvable
    submodule still ends in that submodule's own name, which is what a resolvable one would have given
    too).

    Falls back to `start` itself only when there is no checkout at all, because `repo_root()`'s own cwd
    path legitimately resolves a directory that is in no checkout at all, and resolution must still
    produce a value there.
    """
    common = _git_common_dir(start)
    if common is None:
        return start
    configured = _configured_worktree(common)
    if configured is not None:
        return configured
    if _is_resolved_working_tree(common.parent, common):
        return common.parent
    return common


def _known_trackers() -> list[str]:
    """Every tracker that ships a `config-map.json`: the membership test, and the list a refusal names.

    A tracker name is checked against these enumerated names rather than by asking whether
    `skills/tracker/<value>/` exists. `".."` and `"."` both name existing directories, so the
    path-existence form passed them clean and then found no `config-map.json` for them; a
    `"../tracker/<name>"` traversed to a real adapter's map under a name no adapter answers to. For
    `migrate` either one silently costs the adapter's half of the legacy map (see `_migrating_tracker`).
    """
    tracker_dir = plugin_root() / "skills" / "tracker"
    return sorted(p.parent.name for p in tracker_dir.glob("*/config-map.json")) if tracker_dir.is_dir() else []


def _adapter_map_path(tracker: object) -> Path:
    """Where one tracker's `config-map.json` lives."""
    return plugin_root() / "skills" / "tracker" / str(tracker) / "config-map.json"


# The credential-name word set and its matcher, copied rather than imported. `sy_tools/secrets.py`
# carries an identical copy for the same reason and `sy_tools/guards/secret_guard.py` reads it from
# there; this script cannot, because `migrate` is invoked as bare
# `python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py"` with `sys.path[0]` at `scripts/`, so `sy_tools`
# is not importable, and it is the bootstrap command that has to work before anything is configured.
# Keep the two copies in step: a word added to one belongs in the other.
SECRET_WORDS = frozenset({
    "TOKEN", "SECRET", "SECRETS", "KEY", "KEYS", "APIKEY", "PASSWORD", "PASSWD",
    "CREDENTIAL", "CREDENTIALS", "PAT", "AUTH",
})


def looks_like_secret_name(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """True when a variable or config key name is credential-shaped, by word rather than substring.

    Word-split so `ACLI_TOKEN` matches while `TOKENIZER_PATH` does not. `extra` merges in org-specific
    fragments (the `redaction.extra_words` config key) on top of the built-in set, for a credential
    name this list was never going to guess. A deliberate copy of `sy_tools/secrets.py`'s function —
    see the comment on `SECRET_WORDS`.
    """
    words = re.split(r"[^A-Za-z0-9]+", name.upper())
    all_words = SECRET_WORDS if not extra else SECRET_WORDS | extra
    return any(word in all_words for word in words if word)


def _extra_secret_words() -> frozenset[str]:
    """`redaction.extra_words` read directly off the resolved layers, bypassing any gated accessor.

    Mirrors `sy_tools/config.py::extra_secret_words`, and for its reason: this list is what decides
    whether a name is credential-shaped, so reading it through the gate it extends would recurse into
    that gate. A resolution failure degrades to no extra words rather than refusing here — `_migrate`
    calls `resolve()` up front, so a real resolution fault has already refused the conversion by name.
    """
    try:
        values, _ = resolve()
    except SystemExit:
        return frozenset()
    words = _flatten(values).get("redaction.extra_words", [])
    return frozenset(str(w).upper() for w in words) if isinstance(words, list) else frozenset()


_KNOWN_SECRET_ENV: frozenset[str] | None = None


def _known_secret_env_names() -> frozenset[str]:
    """Every secret env var name any tracker adapter declares, unioned across every adapter.

    Adapter-explicit, not a guess: each adapter's `config-map.json` names its own secret(s) under
    `secret_env` (jira: `ACLI_TOKEN`). Checked across every adapter rather than only the selected
    one, so a name left over from switching trackers is still caught. Memoized: this depends only
    on `plugin_root()`, which never changes mid-process.
    """
    global _KNOWN_SECRET_ENV
    if _KNOWN_SECRET_ENV is None:
        names: set[str] = set()
        tracker_dir = plugin_root() / "skills" / "tracker"
        if tracker_dir.is_dir():
            for config_map_path in sorted(tracker_dir.glob("*/config-map.json")):
                names.update(_load_json(config_map_path).get("secret_env", []))
        _KNOWN_SECRET_ENV = frozenset(name.upper() for name in names)
    return _KNOWN_SECRET_ENV


def _looks_like_secret(name: str) -> bool:
    """Word-heuristic OR exact match against any tracker adapter's declared `secret_env` name.

    The word heuristic catches a secret Shipyard never named, by naming convention; the exact match
    catches one it explicitly did name even if that name happens not to contain a generic trigger
    word — `secret_env` entries are UPPER_SNAKE_CASE and may contain underscores the word-splitter
    would otherwise break apart (the same reason `redaction.extra_words` entries must be single words,
    which `config/schema.json`'s own pattern check on that key enforces).

    What `migrate` does with the answer is leave the variable in the environment: a credential belongs
    there and must never be copied into a config file, whatever else the block carries.
    """
    if name.upper() in _known_secret_env_names():
        return True
    return looks_like_secret_name(name, extra=_extra_secret_words())


def _apply_derived_defaults(values: dict, provenance: dict[str, str]) -> None:
    """Defaults that depend on the repo, so they live here instead of in ten prose copies."""
    if values.get("worktree", {}).get("root") in (None, ""):
        root = _logical_repo(repo_root())
        values.setdefault("worktree", {})["root"] = str(root.parent / f"{root.name}-worktrees")
        provenance["worktree.root"] = "derived-default"
    if values.get("memory", {}).get("dir") in (None, ""):
        values.setdefault("memory", {})["dir"] = str(Path.home() / ".claude" / "shipyard" / "memory")
        provenance["memory.dir"] = "derived-default"
    if values.get("scratch", {}).get("dir") in (None, ""):
        values.setdefault("scratch", {})["dir"] = str(Path.home() / ".claude" / "shipyard" / "scratch")
        provenance["scratch.dir"] = "derived-default"


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
        raise SystemExit(f"sy_config: missing required file {path}") from None
    except OSError as exc:
        # An unreadable layer (a bad mode, a dead symlink target) is a configuration fault like any
        # other and must arrive as this module's own refusal, not as a raw traceback mid-conversion.
        raise SystemExit(f"sy_config: {path} could not be read: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"sy_config: {path} is not valid JSON: {exc}") from None
    if not isinstance(loaded, dict):
        raise SystemExit(f"sy_config: {path} must contain a JSON object, not {type(loaded).__name__}")
    return loaded


if __name__ == "__main__":
    raise SystemExit(main())
