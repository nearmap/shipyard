#!/usr/bin/env python3
"""The single reader for every non-secret Shipyard setting, resolved across an ordered layer chain.

Shipyard used to be configured through the `env` block of a repo's `.claude/settings.json`, which
put a credential and a board column name in the same mechanism with nothing but prose separating
them. Claude Code's own plugin config surface cannot replace it: `pluginConfigs` is deliberately
ignored from project-scoped settings, and Shipyard's primary axis (tracker, board, column names)
is exactly per-repo and committed. So Shipyard owns resolution, and this script is the only place
that does it.

Environment variables are now reserved for secrets. A config-shaped variable that survives in the
environment is an error rather than a silent override: silent precedence is what made the old
secret/config boundary illegible, and a loud rejection naming both values is what makes the new
one obvious.

Two knobs that look symmetrical are not. A subagent's model is resolvable at dispatch time via the
Agent invocation's model parameter, so `models.agents.<name>.model` is fully live. Effort exists
only as agent frontmatter, parsed before any substitution pass runs, and there is no effort
parameter on the Agent tool; `models.agents.<name>.effort` is therefore policy this script
validates and clamps, not something it can apply at dispatch. See docs/configuration.md.

Commands:
  get <dotted.key> [--default V]   resolved value for one key, bare on stdout
  show [--json]                    every resolved value with the layer each came from
  validate                         schema, floors, and environment conflicts; exit 1 on any error
  agent <name> [--json]            floor-clamped dispatch model for one agent
  fingerprint                      stable digest of the resolved config, for cache invalidation
  scratch-dir <identifier>         ephemeral working directory for one identifier, created if absent
  scratch-dir --repo               this repository's own scratch directory, shared by its worktrees
  migrate --settings <path>        convert a legacy settings.json env block into config JSON
  self-test

Layer chain, lowest precedence first: ~/.shipyard/config.json, <repo>/.shipyard/config.json
(committed), <repo>/.shipyard/config.local.json (gitignored).

The governing reference is docs/configuration.md; the machine-readable schema is config/schema.json.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
from secret_words import looks_like_secret_name
import subprocess
import sys
from sy_preflight import plugin_build

CONFIG_FILENAME = "config.json"
LOCAL_FILENAME = "config.local.json"
CONFIG_DIRNAME = ".shipyard"
SCHEMA_URL = "https://raw.githubusercontent.com/nearmap/shipyard/main/config/schema.json"

# Weakest to strongest. Clamping a floor needs a total order, so an alias absent here is an error
# rather than an unranked value that silently skips the floor check.
MODEL_ORDER = ("haiku", "sonnet", "opus", "fable")
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
# Effort is dropped silently for a model Claude Code does not treat as effort-capable: a subagent
# pinned to haiku records no effort at all and inherits the session's. Verified empirically on
# 2.1.220 — `effort: low` on sonnet was recorded, the same frontmatter on haiku was absent.
EFFORT_CAPABLE = frozenset({"sonnet", "opus", "fable"})

_RESOLVED: tuple[dict, dict[str, str]] | None = None
_REPO_ROOT: Path | None = None

CANONICAL_COLUMNS = ("backlog", "ready", "in_progress", "in_review", "done")
# Config keys whose absence is fatal rather than defaulted.
REQUIRED_PATHS = tuple(f"columns.{name}" for name in CANONICAL_COLUMNS) + ("tracker",)
# Legacy env var -> config path, for the conflict error and for `migrate`. Tracker-specific names
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
    args = _build_parser().parse_args(argv)
    if args.command == "get":
        print(_render(get(args.key, default=args.default)))
        return 0
    if args.command == "show":
        return _show(as_json=args.json)
    if args.command == "validate":
        errors = validate()
        if errors:
            print("sy_config: configuration is invalid:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        print(json.dumps({"valid": True, "fingerprint": fingerprint()}))
        return 0
    if args.command == "agent":
        binding = agent_binding(args.name)
        print(json.dumps(binding, indent=2, sort_keys=True) if args.json else binding["model"])
        return 0
    if args.command == "fingerprint":
        print(fingerprint())
        return 0
    if args.command == "scratch-dir":
        if args.repo == bool(args.identifier):
            raise SystemExit("sy_config: scratch-dir takes either one identifier or --repo, not both and not neither.")
        print(repo_scratch_dir() if args.repo else scratch_dir(args.identifier))
        return 0
    if args.command == "migrate":
        return _migrate(Path(args.settings), Path(args.out) if args.out else None)
    _self_test()
    print("sy_config self-test passed")
    return 0


def get(path: str, *, default: object | None = None) -> object:
    """One resolved value by dotted path. Refuses credential-shaped keys outright.

    An unknown key is an error unless `default` is given: a key an adapter documents as optional
    has no entry to resolve, and a caller that knows it is optional says so explicitly rather than
    every unknown key silently becoming empty.

    A default is any JSON-shaped value, not only a string: config values are lists and objects too,
    and `secret_guard.py` already passes `default=[]` for `redaction.extra_words`.
    """
    if _looks_like_secret(path.replace(".", "_")):
        raise SystemExit(
            f"sy_config: refusing to read {path!r}: it is credential-shaped, and secrets are never "
            "read from a config file. Keep them in the environment."
        )
    values, _ = resolve()
    flat = _flatten(values)
    if path not in flat:
        if default is not None:
            return default
        near = ", ".join(sorted(k for k in flat if k.startswith(path.split(".")[0]))) or "none"
        raise SystemExit(f"sy_config: unknown config key {path!r}. Keys under that prefix: {near}")
    return flat[path]


def scratch_dir(identifier: str) -> Path:
    """The ephemeral working directory for one identifier under `scratch.dir`, created if absent.

    The root is resolved, never re-derived, so a relocated `scratch.dir` moves every caller at once.

    Containment is checked against the resolved candidate rather than inferred from the string. An
    identifier of `"."` or `""` has no path parts at all, so every string-shaped guard passes it and
    the root itself would be returned: two identifiers would collide there, and a caller that
    cleans up what it was handed would delete every other identifier's data. Resolving also catches
    a `..` hidden mid-path and a symlink already inside the root that `mkdir(parents=True)` would
    otherwise follow straight out of it.

    `scratch.dir` itself must be absolute. A repo-committed `.shipyard/config.json` is one of the
    layers this resolves, and `review_guard.py`'s hunt-mode write sandbox is exactly `scratch_dir()`'s
    containment check — a relative value resolves against whatever the *calling process's* cwd
    happens to be rather than any fixed location, so a committed `{"scratch": {"dir": ".."}}` can
    silently put the "sandbox" root at an ancestor of the checkout itself, and every file inside the
    checkout then satisfies the containment check that was supposed to keep hunt out of it.
    """
    root = Path(str(get("scratch.dir")))
    if not root.is_absolute():
        raise SystemExit(
            f"sy_config: scratch.dir resolved to {str(root)!r}, which is not absolute. A relative "
            "scratch.dir resolves against whatever directory happens to be the current process's cwd, "
            "which can put the write sandbox this backs inside the very checkout it must stay outside "
            "of. Set scratch.dir to an absolute path, or leave it unset to use the shipped default."
        )
    refusal = SystemExit(
        f"sy_config: refusing to create a scratch directory for {identifier!r}: an identifier "
        "must be a relative name that stays inside the resolved scratch root."
    )
    try:
        relative = Path(identifier)
        candidate = root / relative
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

    Keyed on the *logical* repository — the directory holding the shared `.git` — rather than on the
    resolved checkout, for a reason that is load-bearing rather than tidy. `repo_root()` honours
    `CLAUDE_PROJECT_DIR` when a session set it and derives from the working directory when nothing
    did, and Claude Code exports that pointer to hook subprocesses but not to a subagent's own Bash
    tool. Keyed on `repo_root().name`, a `PreToolUse` guard inside a `/sy:ship` worktree would
    therefore resolve the main checkout's name while the agent it guards resolved the worktree's, and
    the guard would deny every write the agent believed was permitted. The logical repository is the
    same absolute path from either, so both sides agree without depending on `CLAUDE_PROJECT_DIR` or
    any working-directory convention (absent a `GIT_COMMON_DIR`/`GIT_DIR` override, which neither the
    hook nor the agent sets).

    `start` names the directory to resolve from — a hook passes the event's own cwd, so guard and
    guarded resolve from one cwd concept; the default is the resolved repository root, which is what
    a direct CLI or in-session caller means. Containment against the resolved *root* is `scratch_dir`'s
    own job, never restated here — but the resolved *directory* is additionally checked against every
    worktree of this repository, main and linked alike, because `scratch.dir` itself is one of the
    values a repo-committed `.shipyard/config.json` can set: `scratch_dir()` already refuses a
    non-absolute root, but nothing stops an absolute value that happens to equal or contain a checkout
    being reviewed, which would hand `review_guard.py`'s hunt-mode write sandbox that checkout's own
    source. Checking only `start`'s own working tree is not enough: a `PreToolUse` hook's cwd is the
    *main* checkout in the overwhelming majority of `sy:gate`/`sy:hunt` runs, not the build/slice/review
    worktree the tool call actually targets — `/sy:ship` names the worktree only in the dispatched
    agent's prompt text, never as the subagent's own cwd — so a `scratch.dir` that overlaps some other,
    currently-inactive worktree of the repo (for example a naturally-plausible `worktree.root` nested
    under the same root as `scratch.dir`) would pass a check scoped to `start` alone while still
    exposing whichever worktree an absolute-path write actually targets. `_all_worktrees` enumerates
    every *linked* worktree from git's own bookkeeping, independent of `start`, plus `start`'s own
    working tree explicitly (`_git_toplevel(origin)`) — the registry alone is not enough either: a
    *main* working tree (as opposed to a linked one) has no entry under `<common>/worktrees/` at all,
    so a `--separate-git-dir` or bare-plus-`worktree-add` main checkout with no resolvable
    `core.worktree` (`_logical_repo` falls back to `common`, the detached gitdir itself, in that case)
    would otherwise go unguarded even though `start` is sitting inside it right now. The comparison is
    by device and inode, not by resolved spelling: `Path.resolve()` normalizes symlinks, `.` and `..`,
    but not case, and a case-insensitive filesystem (APFS's default) lets a differently-cased spelling
    of the same ancestor stay string-unequal to a checkout path while being the identical directory on
    disk.
    """
    origin = Path(start) if start is not None else repo_root()
    common = _git_common_dir(origin)
    if common is None:
        raise SystemExit(
            f"sy_config: {str(origin)!r} is not a directory inside a git checkout, so no repository "
            "scratch directory can be resolved from it."
        )
    logical = _logical_repo(origin)
    directory = scratch_dir(logical.name)
    guarded_set = _all_worktrees(common, logical)
    checkout = _git_toplevel(origin)
    if checkout is not None:
        guarded_set.append(checkout)
    for guarded in guarded_set:
        if _same_directory(directory, guarded) or any(
            _same_directory(directory, parent) for parent in guarded.parents
        ):
            raise SystemExit(
                f"sy_config: the resolved scratch directory {directory.resolve()} contains a "
                f"worktree of this repository ({guarded}). scratch.dir must not resolve to that "
                "worktree or an ancestor of it — every file inside it would then satisfy the "
                "containment check that is supposed to keep hunt out of it; check for a misconfigured "
                "scratch.dir or worktree.root in a committed or local .shipyard/config.json."
            )
    return directory


def _all_worktrees(common: Path, logical: Path) -> list[Path]:
    """The main checkout plus every *linked* worktree of this repository, read directly from git's own
    bookkeeping under `<common>/worktrees/`, independent of which one the current invocation happens
    to be running from. (`repo_scratch_dir` separately adds `start`'s own working tree, which the
    registry alone does not cover for a main worktree — see its docstring.)

    A `PreToolUse` hook's cwd is the main checkout in the overwhelming majority of `sy:gate`/`sy:hunt`
    runs, never the build/slice/review worktree `/sy:ship` actually dispatched the tool call against —
    so a check scoped to the current invocation's own working tree misses every *other* live worktree
    of the same repository, exactly where those worktrees live.

    Each entry's own absolute path is read from `<common>/worktrees/<id>/gitdir`, not assumed to be
    `<id>`'s own name: `git worktree add --relative-paths` (or `worktree.useRelativePaths`) writes that
    file as a path relative to `<common>/worktrees/<id>/` itself, not to `common` or to any process's
    cwd, so a relative record is resolved against the entry's own directory before use — resolving it
    against the wrong base, or leaving it relative and comparing it as-is, would silently stat the
    guard process's own cwd instead of the worktree. A record git itself writes always ends in the
    literal `.git`, naming the worktree's own `.git` file, but git's own reader (`git-worktree(1)`'s
    DETAILS section) accepts the bare directory form too — it's the documented spelling for hand
    repairing a relocated worktree's `gitdir` file after moving it outside `git worktree move` — so
    only the optional `.git` suffix is stripped, the same way git strips it, rather than requiring it
    and refusing a form git itself accepts. A blank `gitdir` file, or one that is missing or
    unreadable, means this cannot determine part of the guarded set at all, which fails closed
    (raises) rather than silently guarding fewer worktrees than exist.
    """
    worktrees = [logical]
    worktrees_dir = common / "worktrees"
    if not worktrees_dir.is_dir():
        return worktrees
    for entry in sorted(worktrees_dir.iterdir()):
        gitdir_file = entry / "gitdir"
        if not gitdir_file.is_file():
            raise SystemExit(
                f"sy_config: {str(gitdir_file)!r} is missing, so this worktree's own location cannot "
                "be determined and so cannot be guarded. Run `git worktree prune` if it was removed "
                "without `git worktree remove`, or `git worktree repair` if it was relocated."
            )
        try:
            raw = gitdir_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(
                f"sy_config: {str(gitdir_file)!r} could not be read ({exc}), so this worktree's own "
                "location cannot be determined and so cannot be guarded."
            ) from None
        if not raw:
            raise SystemExit(
                f"sy_config: {str(gitdir_file)!r} is blank, so this worktree's own location cannot be "
                "determined and so cannot be guarded — most likely a truncated or otherwise corrupted "
                "gitdir file."
            )
        pointed = Path(raw)
        if not pointed.is_absolute():
            pointed = (entry / pointed).resolve()
        # Git's own reader strips an optional trailing "/.git" (git-worktree(1) documents writing the
        # bare directory path directly when hand-repairing a relocated worktree, e.g. after `mv`), so
        # both spellings name the worktree; requiring the suffix would refuse a git-accepted record.
        worktree = pointed.parent if pointed.name == ".git" else pointed
        worktrees.append(worktree)
    return worktrees


def _logical_repo(start: Path) -> Path:
    """The directory holding the checkout's shared `.git`, or `start` itself when there is no checkout.

    A linked worktree resolves to its main checkout, which is what every per-repository derived
    default means. Keyed on the worktree instead, `worktree.root` nests a second worktrees directory
    inside the first (`<repo>-worktrees/AM-1/../AM-1-worktrees`), which is where `/sy:ship` would put
    a slice worktree it created from inside a build worktree.

    A submodule's `--git-common-dir` resolves under the superproject's `.git/modules/`, whose parent
    directory name is the fixed string `modules` for every submodule on the machine; keyed on that,
    two unrelated submodules would share one scratch directory and one `worktree.root`. The shared git
    dir names the submodule's own working tree in its `core.worktree`, so `_configured_worktree` reads
    it from the *shared* config and needs no per-checkout detection — which matters because a linked
    worktree of a submodule reports no superproject at all, and so any detection keyed on the checkout
    would miss exactly the worktrees `/sy:ship` itself creates.

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


def resolve() -> tuple[dict, dict[str, str]]:
    """Deep-merge every present layer over the shipped defaults, tracking each key's origin.

    Memoized: resolution shells out to git and reads up to four files, and every consumer in a
    single process asks for it repeatedly. `reset_cache()` is the only way to re-read.
    """
    global _RESOLVED
    if _RESOLVED is None:
        _RESOLVED = _resolve_uncached()
    return _RESOLVED


def reset_cache() -> None:
    """Drop the memoized resolution, so the next read sees the files as they are on disk now."""
    global _RESOLVED, _REPO_ROOT
    _RESOLVED = None
    _REPO_ROOT = None


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


def validate() -> list[str]:
    """Every reason the resolved configuration must be rejected, each naming its key and source.

    A configuration that cannot be resolved at all — a `CLAUDE_PROJECT_DIR` naming no git checkout, a
    `git` that cannot be run, `Path.cwd()` on a deleted working directory, a layer file that cannot be
    read or parsed — is collected as an error and returned, not raised: this function exists to report
    every problem it can see, and both `repo_root()` and `resolve()` are reached from `layers()` and
    `_layer_violations()` as well, so each is asked once up front rather than allowed to exit the
    process from whichever call site got there first.

    A resolution failure is reported *first*, and only the checks that need nothing resolved run
    alongside it. `_legacy_env_conflicts()` absorbs the same failure into an empty flat config and
    then reports every legacy `SY_*` variable as disagreeing with a key that "resolves to None" — a
    derived, factually wrong line that would bury the one real cause — so it is skipped, while
    `_outranking_env_conflicts()`, which reads only the environment, still runs: a root that will not
    resolve is no reason to hide a live problem that has nothing to do with it.
    """
    errors: list[str] = list(_outranking_env_conflicts())
    try:
        root = repo_root()
    except SystemExit as exc:
        return [str(exc), *errors]
    except OSError as exc:
        return [f"sy_config: the repository root could not be resolved: {exc}", *errors]
    try:
        values, provenance = resolve()
    except SystemExit as exc:
        return [str(exc), *errors]
    errors.extend(_legacy_env_conflicts())
    for label, path in layers():
        if path.is_file():
            errors.extend(_layer_violations(path, label))

    flat = _flatten(values)
    tracker = flat.get("tracker")
    if tracker and str(tracker) not in _known_trackers():
        errors.append(
            f"tracker {tracker!r} (from {provenance.get('tracker')}) has no adapter under skills/tracker/. "
            f"Known trackers: {', '.join(_known_trackers()) or 'none'}."
        )
    required = list(REQUIRED_PATHS) + list(_adapter_map().get("required", []))
    for path in required:
        if flat.get(path) in (None, ""):
            errors.append(
                f"{path} is required and unset. Set it in {root / CONFIG_DIRNAME / CONFIG_FILENAME}."
            )
    # A presence check only: the env var's name is reported, its value never read into a variable
    # or a message. `os.environ.get(name)` here is used solely for its truthiness.
    for name in _adapter_map().get("secret_env", []):
        if not os.environ.get(name):
            errors.append(
                f"{name} is required by the {tracker!r} tracker and not set in the environment. "
                f"Export it — never put it in a config file. See docs/configuration.md."
            )
    errors.extend(_validate_models(values, provenance))
    return errors


def env_conflicts() -> list[str]:
    """Config-shaped environment variables, which are an error and never an override."""
    return [*_outranking_env_conflicts(), *_legacy_env_conflicts()]


def _outranking_env_conflicts() -> list[str]:
    """The conflicts that need nothing resolved: a variable Claude Code lets outrank this resolver.

    Split from the legacy-name checks because it depends on the environment alone, so `validate()` can
    keep reporting it when the configuration cannot be resolved at all.
    """
    if os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"):
        return [
            "CLAUDE_CODE_SUBAGENT_MODEL is set. It outranks the per-invocation model parameter and "
            "would silently reroute every agent off the model this config resolved. Unset it."
        ]
    return []


def _legacy_env_conflicts() -> list[str]:
    """Retired `SY_*` names still set in the environment, compared against what they now resolve to.

    Needs a resolved configuration on both sides — the value to compare against, and the adapter's own
    legacy names — so `validate()` only asks once resolution has succeeded.
    """
    errors: list[str] = []
    try:
        flat = _flatten(resolve()[0])
    except SystemExit:
        flat = {}
    for name, path in sorted(_legacy_env_map().items()):
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
    known = set(_legacy_env_map())
    for name in sorted(os.environ):
        if re.fullmatch(r"SY_[A-Z0-9_]+", name) and name not in known and not name.startswith("SY_TEST_"):
            errors.append(
                f"{name} is set but is not a Shipyard setting. Every setting now lives in "
                f"{CONFIG_DIRNAME}/{CONFIG_FILENAME}; unset it or correct the name."
            )
    return errors


def agent_binding(name: str) -> dict:
    """The dispatch-time model and the effort policy for one agent, after floor clamping."""
    values, provenance = resolve()
    agents = values.get("models", {}).get("agents", {})
    if name not in agents:
        raise SystemExit(f"sy_config: unknown agent {name!r}. Known agents: {', '.join(sorted(agents))}")
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


def fingerprint() -> str:
    """Digest of the plugin build plus every resolved value, for cache and ship-state invalidation."""
    values, _ = resolve()
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{plugin_build()}|{canonical}".encode()).hexdigest()[:16]


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
    shipped defaults with no layer above them. A `git` that cannot be run is a separate refusal from
    `_git_toplevel` itself, so it is never misreported as the pointer's fault and reaches the cwd
    path too, which has no pointer to blame. A working directory that can no longer be read at all —
    deleted or made inaccessible under a hook that inherited it — is a third named refusal, mirroring
    `sy_tools/config.py::repo_root`: `validate()` had its own guard, but `show`, `get`, `agent` and
    `fingerprint` reach here without one and used to traceback raw out of `Path.cwd()`.
    """
    global _REPO_ROOT
    if _REPO_ROOT is None:
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
    return _REPO_ROOT


def _git_toplevel(start: Path) -> Path | None:
    """The resolved root of the git checkout containing `start`, or None when there is not one.

    A `git` that cannot be *run* at all is refused here rather than folded into None, for the reasons
    `sy_tools/config.py::_git_toplevel` gives — None means "git ran and reported no checkout", which
    the cwd path legitimately answers with a cwd fallback. Refused *here* so that one guard covers
    every path to the repository root: `validate()` guards its own `repo_root()` call, but `resolve()`,
    `layers()` and the `show`/`get`/`agent`/`fingerprint` subcommands reach it without one, and each
    used to traceback raw on a missing binary. The claim is scoped to root resolution — `fingerprint()`
    also calls `sy_preflight.plugin_build()`, whose own `git rev-parse HEAD` is a separate, unguarded
    subprocess in another script. As this module's own `SystemExit`, the refusal arrives as one line
    for the CLI and is caught by the callers that already degrade on one — `_adapter_map()`,
    `validate()`, and `secret_guard.py`'s word-list fallback.
    """
    if not start.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
        )
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
    against *this process's* cwd instead of `start` — fail-soft on the one boundary these callers
    exist to keep. A relative answer is None, the same as no checkout.

    A `git` that cannot be run is refused by name here for the reasons `_git_toplevel` gives; None
    means only "git ran and reported no checkout", which the callers act on themselves.
    """
    if not start.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
        )
    except OSError as exc:
        raise SystemExit(
            f"sy_config: git could not be run to resolve the repository's scratch directory from {start}: "
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
    """
    for filename in ("config.worktree", "config"):
        proc = subprocess.run(
            ["git", "config", "--file", str(common / filename), "--get", "core.worktree"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
        )
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
    """
    if not candidate.is_dir():
        return False
    proc = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    )
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        return False
    return _git_common_dir(candidate) == common


def _same_directory(a: Path, b: Path) -> bool:
    """Whether `a` and `b` name the same directory on disk, by device and inode rather than spelling.

    `repo_scratch_dir`'s checkout-overlap guard cannot compare resolved paths as strings: a
    case-insensitive filesystem (APFS's default) resolves a differently-cased spelling of the same
    ancestor to a string that is unequal to the checkout's own canonical spelling, even though it
    names the identical directory — the exact gap a resolved-path comparison alone left open. A
    missing or unreadable directory can never be "the same" as one that exists.
    """
    try:
        stat_a = a.stat()
        stat_b = b.stat()
    except OSError:
        return False
    return (stat_a.st_dev, stat_a.st_ino) == (stat_b.st_dev, stat_b.st_ino)


def _validate_models(values: dict, provenance: dict[str, str]) -> list[str]:
    errors: list[str] = []
    models = values.get("models", {})
    tiers = models.get("tiers", {})
    for tier, alias in sorted(tiers.items()):
        if alias not in MODEL_ORDER:
            errors.append(
                f"models.tiers.{tier} is {alias!r} (from {provenance.get(f'models.tiers.{tier}')}), which is not a "
                f"known model. Known models: {', '.join(MODEL_ORDER)}."
            )
    floors = _load_json(plugin_root() / "config" / "floors.json")
    for name, binding in sorted(models.get("agents", {}).items()):
        source = provenance.get(f"models.agents.{name}.model", "shipped-default")
        alias = _resolve_tier(binding.get("model"), tiers)
        if alias not in MODEL_ORDER:
            errors.append(
                f"models.agents.{name}.model is {binding.get('model')!r} (from {source}), which is neither a "
                f"declared tier ({', '.join(sorted(tiers))}) nor a known model ({', '.join(MODEL_ORDER)})."
            )
            continue
        floor = floors.get(name, {})
        min_model = _resolve_tier(floor.get("min_model"), tiers)
        if min_model in MODEL_ORDER and MODEL_ORDER.index(alias) < MODEL_ORDER.index(min_model):
            errors.append(
                f"models.agents.{name}.model is {alias!r} (from {source}) but {name} has a floor of {min_model!r}: "
                f"{floor.get('why', 'this floor is a quality floor, not a cost dial')}. Cost-scaling may raise a "
                f"floor, never lower it."
            )
        effort = binding.get("effort")
        min_effort = floor.get("min_effort")
        if effort and min_effort and EFFORT_ORDER.index(effort) < EFFORT_ORDER.index(min_effort):
            errors.append(
                f"models.agents.{name}.effort is {effort!r} but {name} has a floor of {min_effort!r}: "
                f"{floor.get('why', 'this floor is a quality floor, not a cost dial')}."
            )
        if effort and alias not in EFFORT_CAPABLE:
            errors.append(
                f"models.agents.{name} declares effort {effort!r} but is bound to {alias!r}, which Claude Code does "
                f"not treat as effort-capable: the declared effort would be dropped silently and the agent would "
                f"inherit the session's. Bind it to one of {', '.join(sorted(EFFORT_CAPABLE))} instead."
            )
    return errors


def _legacy_env_map() -> dict[str, str]:
    """Tracker-neutral legacy names plus whatever the selected adapter declares as its own."""
    return dict(LEGACY_ENV) | _adapter_map().get("legacy_env", {})


def _adapter_map() -> dict:
    """The selected adapter's own config declaration, so one tracker's vocabulary never lands here."""
    try:
        tracker = _flatten(resolve()[0]).get("tracker")
    except SystemExit:
        return {}
    path = _adapter_map_path(tracker)
    return _load_json(path) if path.is_file() else {}


def _adapter_map_path(tracker: object) -> Path:
    """Where one tracker's `config-map.json` lives: one spelling for the lenient and strict readers."""
    return plugin_root() / "skills" / "tracker" / str(tracker) / "config-map.json"


def _known_trackers() -> list[str]:
    """Every tracker that ships a `config-map.json`: the membership test, and the list a refusal names.

    A configured `tracker` is checked against these enumerated names rather than by asking whether
    `skills/tracker/<value>/` exists. `".."` and `"."` both name existing directories, so the
    path-existence form passed them clean and then found no `config-map.json` for them, skipping every
    `required` and `secret_env` check that config validation exists to enforce; `"../tracker/<name>"`
    traversed to a real adapter's map under a name no adapter answers to. `sy_tools/tracker/__init__.py`
    refuses each of them at tool-call time, which is exactly the point — validation is meant to catch it
    before then.
    """
    tracker_dir = plugin_root() / "skills" / "tracker"
    return sorted(p.parent.name for p in tracker_dir.glob("*/config-map.json")) if tracker_dir.is_dir() else []


_SCHEMA: dict | None = None
_JSON_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _load_schema() -> dict:
    """`config/schema.json`, memoized. The one place every legitimate config key is declared."""
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = _load_json(plugin_root() / "config" / "schema.json")
    return _SCHEMA


def _matches_type(value: object, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected not in _JSON_TYPES:
        return True  # an unrecognized type name is a schema-authoring bug, not a data bug
    return isinstance(value, _JSON_TYPES[expected])


def _schema_violations(node: dict, value: object, path: str) -> list[tuple[str, str]]:
    """Every way `value` disagrees with schema `node`, as `(kind, message)` pairs.

    `kind` is `"credential"` for an undeclared key that also looks secret-shaped — the specific,
    actionable case `secret_guard.py` already covers by name — or `"schema"` for everything else:
    undeclared-and-not-secret-shaped (a typo or stale setting), wrong type, bad enum, pattern
    mismatch, below minimum. A caller that only cares about the secret-leak case (`show`, which
    must never print a value) filters on `kind`; `validate` reports every kind.

    Recurses through nested objects/arrays exactly as `config/schema.json` declares them: a
    `properties` key is a fixed name, `additionalProperties: false` makes every other name a
    violation, `additionalProperties: <schema>` (e.g. `models.tiers.*`, `models.agents.<name>`)
    validates an open set of names against one sub-schema, and `additionalProperties: true` (e.g.
    `tracker_config.*`, adapter-owned) admits anything with no further check.
    """
    violations: list[tuple[str, str]] = []
    types = node.get("type")
    if types is not None:
        allowed = [types] if isinstance(types, str) else types
        if not any(_matches_type(value, t) for t in allowed):
            violations.append(("schema", f"{path!r} must be one of type {allowed}, got {type(value).__name__}"))
            return violations  # further checks assume the value is already the right shape

    if "enum" in node and value not in node["enum"]:
        violations.append(("schema", f"{path!r} must be one of {node['enum']}, got {value!r}"))
    if isinstance(value, str):
        if "pattern" in node and not re.fullmatch(node["pattern"], value):
            violations.append(("schema", f"{path!r} value {value!r} does not match the required pattern {node['pattern']!r}"))
        if "minLength" in node and len(value) < node["minLength"]:
            violations.append(("schema", f"{path!r} must be at least {node['minLength']} characters"))
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in node and value < node["minimum"]:
        violations.append(("schema", f"{path!r} must be >= {node['minimum']}, got {value}"))
    if isinstance(value, list) and "items" in node:
        for i, item in enumerate(value):
            violations.extend(_schema_violations(node["items"], item, f"{path}[{i}]"))
    if isinstance(value, dict):
        properties = node.get("properties", {})
        additional = node.get("additionalProperties", True)
        for key, sub_value in value.items():
            sub_path = f"{path}.{key}" if path else key
            if key in properties:
                violations.extend(_schema_violations(properties[key], sub_value, sub_path))
            elif additional is False:
                if _looks_like_secret(sub_path.replace(".", "_")):
                    violations.append(("credential", (
                        f"{sub_path!r} is credential-shaped. Secrets are never read from a config file: "
                        "keep them in the environment, where scripts/secret_guard.py can cover them."
                    )))
                else:
                    violations.append(("schema", (
                        f"{sub_path!r} is not a key config/schema.json declares — a typo, or a stale/unused setting."
                    )))
            elif isinstance(additional, dict):
                violations.extend(_schema_violations(additional, sub_value, sub_path))
            else:
                # additional is True: adapter-owned (e.g. tracker_config.*), open to any key name
                # structurally — but a secret must never live here either, so the name is still
                # checked directly. Recurse with an open schema ({}, whose own default
                # additionalProperties is True) if the value nests further, so a secret hidden a
                # level deeper (tracker_config.nested.api_token) is caught the same way.
                if _looks_like_secret(sub_path.replace(".", "_")):
                    violations.append(("credential", (
                        f"{sub_path!r} is credential-shaped. Secrets are never read from a config file: "
                        "keep them in the environment, where scripts/secret_guard.py can cover them."
                    )))
                elif isinstance(sub_value, dict):
                    violations.extend(_schema_violations({}, sub_value, sub_path))
    return violations


def _layer_violations(path: Path, label: str, *, kinds: frozenset[str] | None = None) -> list[str]:
    """Every schema/credential violation in one layer file, formatted with the layer for attribution.

    `kinds`, when given, keeps only the requested violation kinds — `show` passes
    `{"credential"}` since a type mismatch risks nothing being printed that shouldn't be.
    """
    violations = _schema_violations(_load_schema(), _load_json(path), "")
    if kinds is not None:
        violations = [(kind, message) for kind, message in violations if kind in kinds]
    return [f"{label} layer {path}: {message}" for _, message in violations]


def _extra_secret_words() -> frozenset[str]:
    """`redaction.extra_words` read directly off resolved layers, bypassing `get()`'s own gate.

    `get()` checks `looks_like_secret_name` before it has resolved anything, so computing the
    extra-word list through `get()` would recurse into the very gate it extends. `resolve()` +
    `_flatten()` is the raw merged config with no gate — exactly what's needed here, and safe
    because `redaction.extra_words` is not itself credential-shaped.
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

    The word heuristic (`scripts/secret_words.py`) catches a secret Shipyard never named, by
    naming convention; the exact match catches one it explicitly did name even if that name
    happens not to contain a generic trigger word — `secret_env` entries are UPPER_SNAKE_CASE and
    may contain underscores the word-splitter would otherwise break apart (the same reason
    `redaction.extra_words` entries must be single words: see `_schema_violations`'s pattern check).
    """
    if name.upper() in _known_secret_env_names():
        return True
    return looks_like_secret_name(name, extra=_extra_secret_words())


def _resolve_tier(value: object, tiers: dict) -> object:
    """A tier name resolves to its alias; anything else is already concrete."""
    return tiers.get(value, value) if isinstance(value, str) else value


def _clamp(value: object, floor: object, order: tuple[str, ...]) -> tuple[object, bool]:
    if not isinstance(value, str) or not isinstance(floor, str):
        return value, False
    if value not in order or floor not in order:
        return value, False
    if order.index(value) < order.index(floor):
        return floor, True
    return value, False


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
        # other and must arrive as this module's own refusal, not as a raw traceback in a hook.
        raise SystemExit(f"sy_config: {path} could not be read: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"sy_config: {path} is not valid JSON: {exc}") from None
    if not isinstance(loaded, dict):
        raise SystemExit(f"sy_config: {path} must contain a JSON object, not {type(loaded).__name__}")
    return loaded


def _render(value: object) -> str:
    """Shell-friendly scalars: booleans lower-cased, None empty, everything else as-is."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _show(*, as_json: bool) -> int:
    """Print every resolved value, refusing outright rather than printing anything if a layer
    declares a credential-shaped key.

    `show` is the command this repo's own docs point people to first, and `/sy:config` wraps it —
    printing a secret here, even once, makes it a permanent part of whatever transcript ran the
    command. Users should never put a secret in a config layer at all; `validate()` already refuses
    to *resolve* one, and `show` must refuse to *print* the raw layer just as hard, before it has
    read a single other value.
    """
    # Names and a fixed advisory sentence only (see _schema_violations) — never a secret value.
    credential_violations: list[str] = []
    for label, path in layers():
        if path.is_file():
            credential_violations.extend(_layer_violations(path, label, kinds=frozenset({"credential"})))
    if credential_violations:
        print("sy_config: refusing to show any value — a config layer declares a credential-shaped key:",
              file=sys.stderr)
        for violation in credential_violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1

    values, provenance = resolve()
    flat = _flatten(values)
    if as_json:
        print(json.dumps({
            "values": values,
            "provenance": provenance,
            "fingerprint": fingerprint(),
            "layers": [
                {"label": label, "path": str(path), "present": path.is_file()} for label, path in layers()
            ],
        }, indent=2, sort_keys=True))
        return 0
    for label, path in layers():
        print(f"{'present' if path.is_file() else 'absent ':>7}  {label:<15} {path}")
    print()
    width = max(len(key) for key in flat)
    for key in sorted(flat):
        print(f"{key:<{width}}  {_render(flat[key]):<28}  [{provenance.get(key, 'shipped-default')}]")
    return 0


def _migrate(settings_path: Path, out_path: Path | None) -> int:
    """Convert a legacy settings.json `env` block into config JSON, leaving secrets behind.

    Resolution is forced up front so that a failure to resolve refuses the whole conversion. Half the
    legacy map is one adapter's own `legacy_env` block, and *which* adapter is the block's own answer
    (`_migrating_tracker`), never the currently resolved one: `migrate` runs at step 1b of
    skills/init-repo/SKILL.md, before step 2 resolves a tracker, so pre-migration the resolved value is
    whatever the shipped default says. Reading the adapter map from that dropped every
    `tracker_config.*` variable in a block migrating to a different tracker and still exited 0 with a
    file that looked complete. `_adapter_map()`'s best-effort `{}` degradation is right for a caller
    that only wants tracker metadata and wrong here for the same reason, so a tracker naming no adapter
    is refused rather than silently costing the adapter's half of the map.

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

    The same temp-write-then-replace pattern as `scripts/sy_memory.py::_atomic_write`, for a stronger
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("get", help="print one resolved value by dotted key")
    g.add_argument("key")
    g.add_argument("--default", help="print this instead of failing when the key is absent (optional keys only)")
    s = sub.add_parser("show", help="print every resolved value with the layer it came from")
    s.add_argument("--json", action="store_true", help="machine-readable, including the fingerprint")
    sub.add_parser("validate", help="schema, per-agent floors, and environment conflicts")
    a = sub.add_parser("agent", help="floor-clamped dispatch model for one agent (bare, for a model override)")
    a.add_argument("name")
    a.add_argument("--json", action="store_true", help="full binding: model, effort, requested values, clamp flags")
    sub.add_parser("fingerprint", help="stable digest of the resolved config")
    d = sub.add_parser("scratch-dir", help="ephemeral working directory for one identifier, created if absent")
    d.add_argument("identifier", nargs="?", help="relative name the directory is created under, e.g. a ticket key")
    d.add_argument(
        "--repo", action="store_true",
        help="this repository's own scratch directory instead, keyed so every worktree of it agrees",
    )
    m = sub.add_parser("migrate", help="convert a legacy settings.json env block into config JSON")
    m.add_argument("--settings", required=True, help="path to the settings.json holding the legacy env block")
    m.add_argument("--out", help="write here instead of stdout")
    sub.add_parser("self-test", help="offline resolution, clamping, and conflict checks; no network")
    return parser


def _self_test() -> None:
    """Offline round-trip against temporary layers, with the real shipped defaults and floors."""
    import tempfile

    saved_env = {
        k: os.environ.get(k) for k in ("SY_TRACKER", "SY_TEST_VAR_A", "CLAUDE_CODE_SUBAGENT_MODEL", "ACLI_TOKEN")
    }
    original_home = Path.home
    original_repo_root = globals()["repo_root"]
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        repo = Path(tmp) / "repo"
        (home / CONFIG_DIRNAME).mkdir(parents=True)
        (repo / CONFIG_DIRNAME).mkdir(parents=True)
        Path.home = staticmethod(lambda: home)  # type: ignore[method-assign]
        globals()["repo_root"] = lambda: repo
        reset_cache()

        def write_layer(path: Path, obj: dict) -> None:
            """Every layer write must invalidate the memoized resolution, or the next read is stale."""
            path.write_text(json.dumps(obj), encoding="utf-8")
            reset_cache()
        for name in ("SY_TRACKER", "CLAUDE_CODE_SUBAGENT_MODEL"):
            os.environ.pop(name, None)
        try:
            assert _flatten({"a": {"b": 1}, "c": 2}) == {"a.b": 1, "c": 2}
            assert _deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}}) == {"a": {"b": 1, "c": 3}}
            assert _coerce("1") is True and _coerce("no") is False and _coerce("30") == 30
            assert _render(True) == "true" and _render(None) == ""

            values, provenance = resolve()
            assert provenance["tracker"] == "shipped-default", "an absent layer must leave defaults in place"
            assert values["worktree"]["root"].endswith("repo-worktrees"), "worktree root must derive from the repo"
            assert provenance["worktree.root"] == "derived-default"
            assert values["scratch"]["dir"] == str(home / ".claude" / "shipyard" / "scratch")
            assert provenance["scratch.dir"] == "derived-default"

            created = scratch_dir("AM-1")
            assert created == home / ".claude" / "shipyard" / "scratch" / "AM-1" and created.is_dir(), (
                "a scratch directory must be created under the resolved root and returned"
            )
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (created.parent / "link").symlink_to(outside, target_is_directory=True)
            for escape in ("", ".", "./", "..", " ", "../elsewhere", "a/../../b", "link/x", "a\0b", str(home)):
                try:
                    scratch_dir(escape)
                except SystemExit as exc:
                    assert "stays inside the resolved scratch root" in str(exc)
                else:
                    raise AssertionError(f"a scratch identifier of {escape!r} must be refused")
            assert not any(outside.iterdir()), "a symlink inside the scratch root must not be followed out of it"

            checkout = Path(tmp) / "logical-repo"
            checkout.mkdir()
            git = ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "-C", str(checkout)]
            subprocess.run([*git[:1], "init", "-q", str(checkout)], check=True)
            subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
            linked = Path(tmp) / "logical-repo-worktrees" / "AM-1"
            subprocess.run([*git, "worktree", "add", "-q", str(linked), "-b", "wt"], check=True)
            scratch_root = Path(str(get("scratch.dir")))
            assert repo_scratch_dir(checkout) == scratch_root / "logical-repo", (
                "the repository's scratch directory must be keyed on the logical repo's own name"
            )
            assert repo_scratch_dir(linked) == repo_scratch_dir(checkout), (
                "a worktree and its main checkout must resolve to one scratch directory, or a hook and the "
                "agent it guards disagree about the sandbox root"
            )
            try:
                repo_scratch_dir()  # the monkeypatched repo_root() is a plain directory, not a checkout
            except SystemExit as exc:
                assert "not a directory inside a git checkout" in str(exc)
            else:
                raise AssertionError("a repo root that is not a git checkout must be refused, not keyed on ''")

            write_layer(home / CONFIG_DIRNAME / CONFIG_FILENAME, {"ci": {"poll_timeout": 60}})
            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME,
                        {"columns": {"ready": "Ready"}, "ci": {"poll_timeout": 90}})
            write_layer(repo / CONFIG_DIRNAME / LOCAL_FILENAME, {"ci": {"poll_timeout": 120}})
            values, provenance = resolve()
            flat = _flatten(values)
            assert flat["ci.poll_timeout"] == 120, "repo-local must outrank repo-committed and user-global"
            assert provenance["ci.poll_timeout"] == "repo-local"
            assert provenance["columns.ready"] == "repo-committed"
            assert flat["ci.poll_interval"] == 30, "an unset sibling must keep its shipped default"

            errors = validate()
            assert any("columns.backlog is required" in e for e in errors), "missing columns must fail loudly"
            assert not any("columns.ready is required" in e for e in errors)

            os.environ["SY_TRACKER"] = "somethingelse"
            conflicts = env_conflicts()
            assert any("SY_TRACKER is set in the environment" in c for c in conflicts), (
                "a config-shaped env var must be an error, not an override"
            )
            assert any("somethingelse" in c and "disagrees with tracker" in c for c in conflicts), (
                "the error must name both the env value and the resolved value"
            )
            os.environ["SY_TRACKER"] = str(_flatten(resolve()[0])["tracker"])
            assert any("agrees with tracker" in c and "redundant" in c for c in env_conflicts()), (
                "an env var that merely agrees is still a second resolution path, so it must still be an error"
            )
            del os.environ["SY_TRACKER"]

            os.environ["CLAUDE_CODE_SUBAGENT_MODEL"] = "sonnet"
            assert any("CLAUDE_CODE_SUBAGENT_MODEL" in c for c in env_conflicts()), (
                "a var that outranks the resolver must fail preflight, not warn"
            )
            del os.environ["CLAUDE_CODE_SUBAGENT_MODEL"]

            gate = agent_binding("gate")
            assert gate["model"] == "fable", "the frontier tier must resolve to a concrete alias"
            assert gate["effort"] == "max"

            write_layer(repo / CONFIG_DIRNAME / LOCAL_FILENAME,
                        {"models": {"agents": {"gate": {"model": "sonnet", "effort": "low"}}}})
            errors = validate()
            assert any("models.agents.gate.model is 'sonnet'" in e and "floor of 'fable'" in e for e in errors), (
                "dropping the reviewer below its floor must be refused by name"
            )
            assert any("models.agents.gate.effort is 'low'" in e for e in errors)
            clamped = agent_binding("gate")
            assert clamped["model"] == "fable" and clamped["model_clamped"], "a below-floor model must clamp up"
            assert clamped["effort"] == "max" and clamped["effort_clamped"]

            write_layer(repo / CONFIG_DIRNAME / LOCAL_FILENAME,
                        {"models": {"agents": {"sweep": {"model": "haiku", "effort": "low"}}}})
            assert any("not treat as effort-capable" in e for e in validate()), (
                "a declared effort on a non-effort-capable model must be refused, not silently dropped"
            )

            write_layer(repo / CONFIG_DIRNAME / LOCAL_FILENAME,
                        {"models": {"agents": {"hunt": {"model": "cheap"}}}})
            assert not any("hunt" in e for e in validate()), "economizing an agent above its floor must be allowed"
            assert agent_binding("hunt")["model"] == "sonnet"

            write_layer(repo / CONFIG_DIRNAME / LOCAL_FILENAME, {"api_token": "should-never-be-here"})
            assert any("credential-shaped" in e for e in validate()), "a secret in a config layer must be refused"

            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = _show(as_json=False)
            assert code == 1, "show must refuse outright, not print anything, when a layer declares a secret"
            assert "should-never-be-here" not in out.getvalue(), "show must never print the secret value itself"
            assert out.getvalue() == "", "show must print nothing at all on stdout when refusing"
            assert "credential-shaped" in err.getvalue()

            out_json = io.StringIO()
            with contextlib.redirect_stdout(out_json), contextlib.redirect_stderr(io.StringIO()):
                code = _show(as_json=True)
            assert code == 1 and out_json.getvalue() == "", "--json mode must refuse the same way, not just text mode"

            (repo / CONFIG_DIRNAME / LOCAL_FILENAME).unlink()
            reset_cache()

            # An undeclared key is refused either way, but the *reason* sharpens once it also
            # looks secret-shaped: generic "not a key schema.json declares" beforehand,
            # "credential-shaped" once redaction.extra_words widens the match.
            write_layer(repo / CONFIG_DIRNAME / LOCAL_FILENAME, {"nm_bearer": "not-flagged-as-a-secret-yet"})
            errors = validate()
            assert any("nm_bearer" in e and "not a key config/schema.json declares" in e for e in errors), (
                "an undeclared key must be refused generically when it doesn't look like a secret"
            )
            assert not any("nm_bearer" in e and "credential-shaped" in e for e in errors)
            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME, {"redaction": {"extra_words": ["BEARER"]}})
            assert any("nm_bearer" in e and "credential-shaped" in e for e in validate()), (
                "redaction.extra_words must widen the config-file secret gate to the sharper reason"
            )
            (repo / CONFIG_DIRNAME / LOCAL_FILENAME).unlink()

            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME, {"redaction": {"extra_words": ["ID_RSA"]}})
            assert any("extra_words[0]" in e and "does not match the required pattern" in e for e in validate()), (
                "a multi-word redaction.extra_words entry must be refused by the schema's pattern check, not silently inert"
            )

            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME, {"ci": {"poll_timeout": "big"}})
            assert any("ci.poll_timeout" in e and "must be one of type ['integer']" in e for e in validate()), (
                "a wrong-typed value must be refused by name, not left to crash whatever reads it later"
            )

            # tracker_config.* has additionalProperties: true (adapter-owned, open to any key) --
            # that openness must never extend to admitting a secret. A non-secret adapter key must
            # still pass through untouched.
            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME,
                        {"tracker_config": {"workspace": "fine", "api_token": "sk-should-be-refused"}})
            errors = validate()
            assert not any("workspace" in e for e in errors), "an ordinary open-section key must pass through"
            assert any("tracker_config.api_token" in e and "credential-shaped" in e for e in errors), (
                "additionalProperties: true must not be a blind spot for a secret hidden inside it"
            )
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = _show(as_json=False)
            assert code == 1 and "sk-should-be-refused" not in out.getvalue(), (
                "show must refuse rather than print a secret hidden inside an open (additionalProperties: true) section"
            )

            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME, {"ship": {"merge_strategy": "sqash"}})
            assert any("ship.merge_strategy" in e and "must be one of ['squash', 'merge', 'rebase']" in e for e in validate()), (
                "a value outside the declared enum must be refused by name"
            )

            assert _schema_violations(_load_schema(), _load_json(plugin_root() / "config" / "defaults.json"), "") == [], (
                "the shipped defaults.json must itself be clean against the schema it ships alongside"
            )

            # A tracker naming no adapter must be refused by the enumerated names, not by testing a
            # path built from the value: "." and ".." are existing directories, so the path form
            # reported a clean config and then skipped every adapter-specific required/secret_env
            # check, and a traversal loaded a different adapter's map than the string names.
            traversal = f"../tracker/{_known_trackers()[0]}"  # reaches a real adapter's map sideways
            for bogus in (".", "..", traversal):
                write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME, {"tracker": bogus})
                assert any(repr(bogus) in e and "has no adapter" in e for e in validate()), (
                    f"tracker {bogus!r} names no adapter and must be refused, not validated clean"
                )
            for known in _known_trackers():
                write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME, {"tracker": known})
                assert not any("has no adapter" in e for e in validate()), f"{known} is a shipped adapter"

            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME,
                        {"columns": {"ready": "Ready"}, "ci": {"poll_timeout": 90}})
            reset_cache()  # tracker resolves to the shipped default ("jira") for the checks below

            assert "ACLI_TOKEN" in _known_secret_env_names(), "jira's declared secret_env must be discovered"
            assert _looks_like_secret("ACLI_TOKEN"), "an adapter-declared secret_env name is credential-shaped by exact match"
            assert not _known_secret_env_names().isdisjoint({"ACLI_TOKEN"})

            os.environ.pop("ACLI_TOKEN", None)
            reset_cache()
            assert any("ACLI_TOKEN" in e and "required by the 'jira' tracker" in e for e in validate()), (
                "a tracker's declared secret_env must be required present in the environment, not just in config"
            )
            os.environ["ACLI_TOKEN"] = "placeholder-value-for-self-test"
            reset_cache()
            assert not any("ACLI_TOKEN" in e and "required by" in e for e in validate()), (
                "a present secret_env value must satisfy the requirement"
            )
            os.environ.pop("ACLI_TOKEN", None)
            reset_cache()
            reset_cache()

            assert get("no.such.setting", default="") == "", "an explicit default must cover an optional key"
            try:
                get("no.such.setting")
            except SystemExit as exc:
                assert "unknown config key" in str(exc)
            else:
                raise AssertionError("an unknown key with no default must fail loudly")
            try:
                get("some.api_key")
            except SystemExit as exc:
                assert "credential-shaped" in str(exc)
            else:
                raise AssertionError("reading a credential-shaped key must be refused")

            # The block migrates to a tracker the pre-migration config does *not* resolve — the
            # documented order, since `migrate` runs before the tracker is ever chosen. The adapter's
            # own names are read from its map rather than spelled: they are its vocabulary, not this
            # script's map's.
            settings = repo / "settings.json"
            resolved_tracker = str(_flatten(resolve()[0])["tracker"])
            target = next(t for t in _known_trackers() if t != resolved_tracker)
            target_legacy = _load_json(_adapter_map_path(target))["legacy_env"]
            settings.write_text(json.dumps({"env": {
                _TRACKER_ENV: target, "SY_CI_POLL_TIMEOUT": "45",
                "SY_DEBUG_EVALS": "1", "ACLI_TOKEN": "secret-value-here",
                "SY_NOT_A_SETTING": "stale",
                **{name: f"legacy-value-{i}" for i, name in enumerate(sorted(target_legacy))},
            }}), encoding="utf-8")
            out = repo / CONFIG_DIRNAME / "migrated.json"
            out.write_text(json.dumps({"columns": {"done": "Already Here"}}), encoding="utf-8")
            summary_out = io.StringIO()
            with contextlib.redirect_stdout(summary_out):
                _migrate(settings, out)
            migrated = _flatten(_load_json(out))
            assert migrated["tracker"] == target
            assert migrated["ci.poll_timeout"] == 45, "a numeric env string must migrate as a number"
            assert migrated["debug.evals"] is True, "a truthy env string must migrate as a boolean"
            assert not any("TOKEN" in k.upper() for k in migrated), "migration must never copy a secret into config"
            for adapter_path in sorted(target_legacy.values()):
                assert adapter_path in migrated, (
                    f"{adapter_path} was dropped: the adapter half of the map must come from the tracker the "
                    "block being migrated names, not from whatever the pre-migration config resolved"
                )
            assert migrated["columns.done"] == "Already Here", (
                "migrating onto an existing config file must merge into it, never truncate it"
            )
            summary = json.loads(summary_out.getvalue())
            assert summary["preserved"] == ["columns.done"]
            assert any("TOKEN" in n.upper() for n in summary["secrets_left_in_env"]), (
                "a credential left behind on purpose must be reported as such"
            )
            assert summary["unmapped_and_not_migrated"] == ["SY_NOT_A_SETTING"], (
                "a name with no config key at all is a different report from a credential left on purpose"
            )

            # An `--out` naming no file must arrive as this module's own refusal. A path with an empty
            # basename (`.`, `/`) never reached the `OSError` guard at all: deriving the sibling
            # temporary name from it raises `ValueError`, so the very case that refusal names first —
            # an `--out` pointed at a directory — escaped as a raw traceback.
            for bogus in (Path("."), repo / CONFIG_DIRNAME):
                try:
                    _migrate(settings, bogus)
                except SystemExit as exc:
                    assert "could not be written" in str(exc) and "Nothing was migrated" in str(exc), str(exc)
                else:
                    raise AssertionError(f"migrate --out {bogus} names no file to write and must refuse")

            settings.write_text(json.dumps({"env": {_TRACKER_ENV: "jria"}}), encoding="utf-8")
            try:
                _migrate(settings, out)
            except SystemExit as exc:
                assert "names no adapter" in str(exc)
            else:
                raise AssertionError("a tracker naming no adapter must refuse, not drop the adapter's own keys")

            assert len(fingerprint()) == 16
            before = fingerprint()
            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME, {"columns": {"ready": "Next"}})
            assert fingerprint() != before, "a changed value must invalidate the fingerprint"
        finally:
            Path.home = original_home  # type: ignore[method-assign]
            globals()["repo_root"] = original_repo_root
            reset_cache()
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
