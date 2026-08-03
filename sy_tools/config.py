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
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return Path(project_dir)
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    )
    out = proc.stdout.strip()
    return Path(out) if proc.returncode == 0 and out else Path.cwd()


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


def fingerprint() -> str:
    """Digest of every resolved value, for cache invalidation and reload reporting."""
    values, _ = resolve()
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def validate() -> list[str]:
    """Every reason the resolved configuration must be rejected. Side-effect-free."""
    errors: list[str] = []
    try:
        values, provenance = resolve()
    except ConfigError as exc:
        return [str(exc)]

    for label, path in layers(_state_root()):
        if path.is_file():
            errors.extend(f"{label} layer {path}: {message}" for message in _layer_violations(path))

    flat = _flatten(values)
    tracker = flat.get("tracker")
    if tracker and not (plugin_root() / "skills" / "tracker" / str(tracker)).is_dir():
        errors.append(
            f"tracker {tracker!r} (from {provenance.get('tracker')}) has no adapter under skills/tracker/"
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


def _state_root() -> Path:
    """The repo root the hot state resolved against, so `validate` reports the same layer paths."""
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
        values.setdefault("worktree", {})["root"] = str(root.parent / f"{root.name}-worktrees")
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
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a JSON object, not {type(loaded).__name__}")
    return loaded
