"""Resolved Shipyard configuration, held hot in memory for the life of the server process.

`scripts/sy_config.py` is a CLI: every read is a fresh process that re-reads up to four JSON
files and shells out to git. A long-lived MCP server resolves the same layer chain once and
serves every tool call from memory; `reload()` (the `reload_config` tool) is the only way to
re-read, so a config edit is picked up deliberately rather than racing mid-call.

Values resolve identically to `sy_config.py` — same layer chain, same deep merge, same derived
defaults, same floor clamping — and `sy_tools/tests/test_config.py` asserts that parity against
`sy_config.py show --json` rather than trusting the reimplementation.

Two pieces of `sy_config.py` are deliberately absent. The retired-environment-variable conflict
report is one: it can only be written by naming those variables, and `scripts/validate.py`'s
config-seam check treats any file but the resolver naming one as a second resolution path for a
key. `migrate` is the other — it is a one-time CLI affordance with no meaning in a server.
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

@dataclasses.dataclass(frozen=True)
class _Resolved:
    """One complete resolution: the merged values, each key's layer, and the repo it resolved for."""

    values: dict
    provenance: dict[str, str]
    root: Path


_STATE: _Resolved | None = None


class ConfigError(RuntimeError):
    """The configuration could not be resolved, or a caller asked for something it may not have."""


def plugin_root() -> Path:
    """The plugin checkout: the session's own pointer when set, else this package's parent."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    return Path(root) if root else Path(__file__).resolve().parent.parent


def repo_root() -> Path:
    """The consuming repository's root: Claude Code's own pointer when set, else derived from cwd.

    `CLAUDE_PROJECT_DIR` is authoritative when present — Claude Code sets it for every MCP stdio
    server it launches (matching the pointer it already gives hooks), and unlike cwd it survives a
    `pixi run <declared-task>` dispatch, which resets the launched process's working directory to
    the manifest's own directory rather than inheriting the caller's. Falling back to `git
    rev-parse --show-toplevel` from cwd keeps every non-Claude-Code invocation working exactly as
    before (manual `pixi run sy-server`, `docs/smoke_mcp.py`, the pytest suite).

    Both paths go through the same `git rev-parse`, so a pointer at a subdirectory resolves to the
    checkout root exactly as cwd does, and a pointer that names no checkout is a `ConfigError`
    rather than a silent fall-through: every layer above the shipped defaults lives under
    `<root>/.shipyard/`, so an unusable root resolves to the shipped defaults with no tracker, no
    column names and nothing said about why. A `git` that cannot be run is a third, separately named
    `ConfigError` raised by `_git_toplevel` itself, so it reaches both paths — including the cwd
    fallback, which has no pointer to blame — and is never reported as the pointer's fault. A working
    directory that can no longer be read at all — deleted or made inaccessible under a long-lived
    server process — is a fourth named `ConfigError`, so it reaches `validate()` as a reportable fault
    rather than a raw `FileNotFoundError` out of whichever tool call resolved first.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        root = _git_toplevel(Path(project_dir))
        if root is None:
            raise ConfigError(
                f"CLAUDE_PROJECT_DIR is {project_dir!r}, which is not a directory inside a git checkout, "
                "so no repository configuration can be resolved from it. Point it at the consuming "
                "repository, or unset it to resolve from the working directory."
            )
        return root
    try:
        return _git_toplevel(Path.cwd()) or Path.cwd()
    except OSError as exc:
        raise ConfigError(
            f"the working directory could not be read to derive the repository root: {exc}. Set "
            "CLAUDE_PROJECT_DIR to the consuming repository, or restart the server from a directory "
            "that still exists."
        ) from None


def _git_toplevel(start: Path) -> Path | None:
    """The resolved root of the git checkout containing `start`, or None when there is not one.

    A `git` that cannot be *run* at all raises rather than returning None, and it raises from here so
    that every caller is covered by one guard instead of each call site growing its own. None means
    one specific thing — git ran and reported no checkout — and the callers act on it accordingly:
    the cwd path treats it as "not in a checkout" and falls back to cwd, which is a legitimate way to
    run the server. Collapsing a missing binary into that same None would take the fallback, resolve
    the shipped defaults alone, and leave every tool call reporting no tracker and unset columns with
    nothing naming the cause; under `CLAUDE_PROJECT_DIR` it would instead blame the pointer for not
    being a checkout, which is false when the pointer is fine and the binary is absent. A missing
    binary is an environment fault like the missing scanner in `secrets.py`, so it is refused by name.

    `stdin` is closed for the same reason the tracker adapters close it on their own subprocesses: this
    runs inside the MCP server, whose stdin is the JSON-RPC transport, and a child that inherits it can
    consume a frame the server was going to read.
    """
    if not start.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
            stdin=subprocess.DEVNULL,
        )
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

    `--path-format=absolute` is not decoration: without it git answers a bare relative `.git` from a
    main checkout, whose `.parent.name` is the empty string. Absoluteness is therefore checked
    explicitly rather than left to `.resolve()`, which would silently resolve a relative answer
    against *this process's* cwd instead of `start` — fail-soft on the one boundary these callers
    exist to keep. A relative answer is None, the same as no checkout.

    A `git` that cannot be run is a named `ConfigError` for the reasons `_git_toplevel` gives, and
    `stdin` is closed for the same reason: this can run inside the MCP server, whose stdin is the
    JSON-RPC transport. None means only "git ran and reported no checkout".
    """
    if not start.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ConfigError(
            f"git could not be run to resolve the repository's scratch directory from {start}: {exc}. "
            "Install git, or put it on PATH."
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

    `stdin` is closed for the same reason the sibling resolvers close it: this can run inside the MCP
    server, whose stdin is the JSON-RPC transport.
    """
    for filename in ("config.worktree", "config"):
        proc = subprocess.run(
            ["git", "config", "--file", str(common / filename), "--get", "core.worktree"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
            stdin=subprocess.DEVNULL,
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

    `stdin` is closed for the same reason the sibling resolvers close it: this can run inside the MCP
    server, whose stdin is the JSON-RPC transport.
    """
    if not candidate.is_dir():
        return False
    proc = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        return False
    return _git_common_dir(candidate) == common


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


def reload() -> dict:
    """Drop the hot state and re-resolve from disk. Returns a summary, never a value."""
    global _STATE
    before = fingerprint() if _STATE is not None else None
    _STATE = None
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

    An unknown key is an error unless a default is supplied: a key an adapter documents as
    optional has no entry to resolve, and a caller that knows it is optional says so. The
    sentinel default is what lets `None` itself be a legitimate default.
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

    The root is resolved, never re-derived, so a relocated `scratch.dir` moves every caller at once.

    Containment is checked against the resolved candidate rather than inferred from the string. An
    identifier of `"."` or `""` has no path parts at all, so every string-shaped guard passes it and
    the root itself would be returned: two identifiers would collide there, and a caller that
    cleans up what it was handed would delete every other identifier's data. Resolving also catches
    a `..` hidden mid-path and a symlink already inside the root that `mkdir(parents=True)` would
    otherwise follow straight out of it.
    """
    root = Path(str(get("scratch.dir")))
    refusal = ConfigError(
        f"refusing to create a scratch directory for {identifier!r}: an identifier must be a "
        "relative name that stays inside the resolved scratch root."
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
    did, and Claude Code exports that pointer to hook subprocesses and stdio servers but not to a
    subagent's own Bash tool. Keyed on `repo_root().name`, a `PreToolUse` guard inside a `/sy:ship`
    worktree would therefore resolve the main checkout's name while the agent it guards resolved the
    worktree's, and the guard would deny every write the agent believed was permitted. The logical
    repository is the same absolute path from either, so both sides agree without depending on
    `CLAUDE_PROJECT_DIR` or any working-directory convention (absent a `GIT_COMMON_DIR`/`GIT_DIR`
    override, which neither the hook nor the agent sets).

    `start` names the directory to resolve from — a hook passes the event's own cwd, so guard and
    guarded resolve from one cwd concept; the default is the resolved repository root, which is what
    a direct in-session caller means. Containment is left to `scratch_dir()`, never restated.
    """
    origin = Path(start) if start is not None else repo_root()
    common = _git_common_dir(origin)
    if common is None:
        raise ConfigError(
            f"{str(origin)!r} is not a directory inside a git checkout, so no repository scratch "
            "directory can be resolved from it."
        )
    return scratch_dir(_logical_repo(origin).name)


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
    at that path. When it is false, this refuses by name rather than silently keying on whatever
    `common.parent`'s name happens to be.

    Falls back rather than refusing when there is no checkout at all, because `repo_root()`'s own cwd
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
    raise ConfigError(
        f"{str(start)!r}'s repository has no resolvable working tree: its shared git directory is "
        f"{common}, and {common.parent} is not itself a checkout of it. This is most often a "
        "submodule whose own identity git cannot currently resolve — for example after `git "
        "submodule deinit` without a later `git submodule update --init`, or a nested or detached "
        "submodule layout git records no `core.worktree` for. Run `git submodule update --init` for "
        "the affected submodule and retry; resolving anyway risks two unrelated repositories sharing "
        "one scratch directory."
    )


def fingerprint() -> str:
    """Digest of every resolved value, for cache invalidation and reload reporting."""
    values, _ = resolve()
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def validate() -> list[str]:
    """Every reason the resolved configuration must be rejected. Side-effect-free.

    A configuration that cannot be resolved at all — an unusable repository root, a layer file that
    cannot be read or parsed — is returned as one error rather than raised, exactly as
    `sy_config.validate()` does it: `validate_config`'s contract is to report a broken config, and an
    exception escaping here reaches the operator as a traceback string instead of a report. Resolution
    is therefore asked before the per-layer schema pass.

    Guarding `resolve()` alone is not enough here, and that is the difference between this deployment
    and the CLI's. The CLI resolves once per process, so a layer the resolver just read successfully
    reads again in the schema pass. This server resolves once per *process lifetime* and then serves
    memory, so once the hot copy is warm the schema pass — plus `adapter_map()`, the floors and the
    schema — are the only things still touching disk, and a layer edited into invalid JSON after that
    point raised out of the one tool whose whole job is diagnosing exactly this fault. Everything after
    resolution therefore reports its own read failure as an error too, on any call, warm or cold.

    The one check that needs nothing resolved — an environment variable Claude Code lets outrank this
    resolver — runs first and survives a resolution failure, exactly as in the CLI: a root that will
    not resolve is no reason to hide a live problem that has nothing to do with it.
    """
    errors: list[str] = list(_outranking_env_conflicts())
    try:
        values, provenance = resolve()
    except ConfigError as exc:
        return [str(exc), *errors]

    try:
        errors.extend(_post_resolution_violations(values, provenance))
    except ConfigError as exc:
        errors.append(str(exc))
    return errors


def _outranking_env_conflicts() -> list[str]:
    """A variable Claude Code lets outrank this resolver: an error, never an override.

    Mirrors `scripts/sy_config.py::_outranking_env_conflicts`, message included, because the CLI's
    `validate` and the `validate_config` tool are one contract read two ways — and this is now the
    only path a session takes, so a fault reported by the CLI alone is a fault nobody sees. Unlike the
    retired-`SY_*` half of the CLI's report (see the module docstring), this names no config key, so it
    opens no second resolution path for one. It reads only the environment: the value is never read,
    only its presence.
    """
    if os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"):
        return [
            "CLAUDE_CODE_SUBAGENT_MODEL is set. It outranks the per-invocation model parameter and "
            "would silently reroute every agent off the model this config resolved. Unset it."
        ]
    return []


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
        if flat.get(path_key) in (None, ""):
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
    """Every tracker that ships a `config-map.json`: the membership test, and the list a refusal names.

    Mirrors `scripts/sy_config.py::_known_trackers`. A configured `tracker` is checked against these
    enumerated names rather than by asking whether `skills/tracker/<value>/` exists: `".."` and `"."`
    both name existing directories, so the path-existence form reported a clean config and then found no
    `config-map.json` for them, silently skipping every `required` and `secret_env` check this validator
    exists to enforce, while `"../tracker/<name>"` traversed to a real adapter's map under a name no
    adapter answers to. `sy_tools/tracker/__init__.py` refuses all three at tool-call time, which is the
    point: `validate_config` is what is supposed to catch them before a tool call ever runs.
    """
    tracker_dir = plugin_root() / "skills" / "tracker"
    return sorted(p.parent.name for p in tracker_dir.glob("*/config-map.json")) if tracker_dir.is_dir() else []


def adapter_map() -> dict:
    """The selected adapter's own config declaration, so one tracker's vocabulary never lands here."""
    tracker = _flatten(resolve()[0]).get("tracker")
    path = plugin_root() / "skills" / "tracker" / str(tracker) / "config-map.json"
    return _load_json(path) if path.is_file() else {}


def extra_secret_words() -> frozenset[str]:
    """`redaction.extra_words`, read off the resolved values directly.

    Read raw rather than through `get()`: `get()` consults this list to decide whether a key is
    credential-shaped, so routing it through `get()` would recurse into the gate it extends.
    """
    words = _flatten(resolve()[0]).get("redaction.extra_words", [])
    return frozenset(str(w).upper() for w in words) if isinstance(words, list) else frozenset()


def resolved_root() -> Path:
    """The consuming repository the hot configuration resolved against.

    `repo_root()` re-derives; this reports what the live values were actually resolved from, so
    `validate` names the same layer paths it read and a caller that must act *inside* the consumer's
    checkout — a subprocess whose own working directory would otherwise decide which repository it
    talks to — cannot pick a different one than the configuration did.
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
        # An unreadable layer (a bad mode, a dead symlink target) is a configuration fault like any
        # other, and `validate()`'s whole contract is to report one rather than raise on it.
        raise ConfigError(f"{path} could not be read: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a JSON object, not {type(loaded).__name__}")
    return loaded
