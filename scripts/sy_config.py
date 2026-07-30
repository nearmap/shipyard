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
    if args.command == "migrate":
        return _migrate(Path(args.settings), Path(args.out) if args.out else None)
    _self_test()
    print("sy_config self-test passed")
    return 0


def get(path: str, *, default: str | None = None) -> object:
    """One resolved value by dotted path. Refuses credential-shaped keys outright.

    An unknown key is an error unless `default` is given: a key an adapter documents as optional
    has no entry to resolve, and a caller that knows it is optional says so explicitly rather than
    every unknown key silently becoming empty.
    """
    if looks_like_secret_name(path.replace(".", "_"), extra=_extra_secret_words()):
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
    """Every reason the resolved configuration must be rejected, each naming its key and source."""
    errors: list[str] = []
    errors.extend(env_conflicts())
    for label, path in layers():
        if path.is_file():
            errors.extend(_secret_keys_in(path, label))
    try:
        values, provenance = resolve()
    except SystemExit as exc:
        return errors + [str(exc)]

    flat = _flatten(values)
    tracker = flat.get("tracker")
    if tracker and not (plugin_root() / "skills" / "tracker" / str(tracker)).is_dir():
        errors.append(f"tracker {tracker!r} (from {provenance.get('tracker')}) has no adapter under skills/tracker/")
    required = list(REQUIRED_PATHS) + list(_adapter_map().get("required", []))
    for path in required:
        if flat.get(path) in (None, ""):
            errors.append(
                f"{path} is required and unset. Set it in {repo_root() / CONFIG_DIRNAME / CONFIG_FILENAME}."
            )
    errors.extend(_validate_models(values, provenance))
    errors.extend(_validate_redaction_words(flat))
    return errors


def _validate_redaction_words(flat: dict) -> list[str]:
    """Each `redaction.extra_words` entry must be a single alphanumeric word.

    `secret_words.looks_like_secret_name` matches whole split words, never substrings — a
    multi-word entry like `"ID_RSA"` would silently never match anything, which is a worse failure
    mode than refusing it loudly here.
    """
    words = flat.get("redaction.extra_words", [])
    if not isinstance(words, list):
        return []
    return [
        f"redaction.extra_words contains {word!r}, which is not a single alphanumeric word: "
        "the matcher compares whole split words, never substrings, so a multi-word entry would "
        "silently never match anything."
        for word in words
        if not (isinstance(word, str) and re.fullmatch(r"[A-Za-z0-9]+", word))
    ]


def env_conflicts() -> list[str]:
    """Config-shaped environment variables, which are an error and never an override."""
    errors: list[str] = []
    if os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"):
        errors.append(
            "CLAUDE_CODE_SUBAGENT_MODEL is set. It outranks the per-invocation model parameter and "
            "would silently reroute every agent off the model this config resolved. Unset it."
        )
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
    """The consuming repository's root, else the working directory when not in a checkout."""
    global _REPO_ROOT
    if _REPO_ROOT is None:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
        )
        _REPO_ROOT = Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else Path.cwd()
    return _REPO_ROOT


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
    path = plugin_root() / "skills" / "tracker" / str(tracker) / "config-map.json"
    return _load_json(path) if path.is_file() else {}


def _secret_keys_in(path: Path, label: str) -> list[str]:
    extra = _extra_secret_words()
    return [
        f"{label} layer {path} declares {key!r}, which is credential-shaped. Secrets are never read from a "
        f"config file: keep them in the environment, where scripts/secret_guard.py can cover them."
        for key in _flatten(_load_json(path))
        if looks_like_secret_name(key.replace(".", "_"), extra=extra)
    ]


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
    root = repo_root()
    if values.get("worktree", {}).get("root") in (None, ""):
        values.setdefault("worktree", {})["root"] = str(root.parent / f"{root.name}-worktrees")
        provenance["worktree.root"] = "derived-default"
    if values.get("memory", {}).get("dir") in (None, ""):
        values.setdefault("memory", {})["dir"] = str(Path.home() / ".claude" / "shipyard" / "memory")
        provenance["memory.dir"] = "derived-default"


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
    secret_errors: list[str] = []
    for label, path in layers():
        if path.is_file():
            secret_errors.extend(_secret_keys_in(path, label))
    if secret_errors:
        print("sy_config: refusing to show any value — a config layer declares a credential-shaped key:",
              file=sys.stderr)
        for error in secret_errors:
            print(f"  - {error}", file=sys.stderr)
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
    """Convert a legacy settings.json `env` block into config JSON, leaving secrets behind."""
    env = _load_json(settings_path).get("env", {})
    if not env:
        raise SystemExit(f"sy_config: {settings_path} has no env block to migrate")
    mapping = _legacy_env_map()
    config: dict = {"$schema": SCHEMA_URL}
    skipped: list[str] = []
    extra = _extra_secret_words()
    for name, value in sorted(env.items()):
        if looks_like_secret_name(name, extra=extra):
            skipped.append(name)
            continue
        path = mapping.get(name)
        if not path:
            skipped.append(name)
            continue
        _assign(config, path, _coerce(value))
    out = json.dumps(config, indent=2, sort_keys=True) + "\n"
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out, encoding="utf-8")
        print(json.dumps({"written": str(out_path), "migrated": len(_flatten(config)) - 1, "left_in_env": skipped}))
    else:
        sys.stdout.write(out)
    return 0


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
    m = sub.add_parser("migrate", help="convert a legacy settings.json env block into config JSON")
    m.add_argument("--settings", required=True, help="path to the settings.json holding the legacy env block")
    m.add_argument("--out", help="write here instead of stdout")
    sub.add_parser("self-test", help="offline resolution, clamping, and conflict checks; no network")
    return parser


def _self_test() -> None:
    """Offline round-trip against temporary layers, with the real shipped defaults and floors."""
    import tempfile

    saved_env = {k: os.environ.get(k) for k in ("SY_TRACKER", "SY_TEST_VAR_A", "CLAUDE_CODE_SUBAGENT_MODEL")}
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

            write_layer(repo / CONFIG_DIRNAME / LOCAL_FILENAME, {"nm_bearer": "not-flagged-yet"})
            assert not any("nm_bearer" in e for e in validate()), "a name outside the built-in word list is not flagged"
            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME, {"redaction": {"extra_words": ["BEARER"]}})
            assert any("nm_bearer" in e and "credential-shaped" in e for e in validate()), (
                "redaction.extra_words must widen the config-file secret gate"
            )
            (repo / CONFIG_DIRNAME / LOCAL_FILENAME).unlink()

            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME, {"redaction": {"extra_words": ["ID_RSA"]}})
            assert any("ID_RSA" in e and "not a single alphanumeric word" in e for e in validate()), (
                "a multi-word redaction.extra_words entry must be refused, not silently inert"
            )

            write_layer(repo / CONFIG_DIRNAME / CONFIG_FILENAME,
                        {"columns": {"ready": "Ready"}, "ci": {"poll_timeout": 90}})
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

            settings = repo / "settings.json"
            settings.write_text(json.dumps({"env": {
                "SY_TRACKER": "somethingelse", "SY_CI_POLL_TIMEOUT": "45",
                "SY_DEBUG_EVALS": "1", "ACLI_TOKEN": "secret-value-here",
            }}), encoding="utf-8")
            out = repo / CONFIG_DIRNAME / "migrated.json"
            with contextlib.redirect_stdout(io.StringIO()):
                _migrate(settings, out)
            migrated = _flatten(_load_json(out))
            assert migrated["tracker"] == "somethingelse"
            assert migrated["ci.poll_timeout"] == 45, "a numeric env string must migrate as a number"
            assert migrated["debug.evals"] is True, "a truthy env string must migrate as a boolean"
            assert not any("TOKEN" in k.upper() for k in migrated), "migration must never copy a secret into config"

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
