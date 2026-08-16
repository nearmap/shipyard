#!/usr/bin/env python3
"""Validate the Shipyard plugin: structure, frontmatter, the tracker seam, and contract completeness.

Run before loading or releasing the plugin. Every check appends to one error list; main() prints them
all and exits 1 if any check failed.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_AGENTS = {
    "sweep", "seam", "trace", "slice", "hunt", "gate", "ship-start", "ship-build", "ship-gate",
    "img-inspector", "explain-author", "debate", "debater", "spec-gate",
}
EXPECTED_SKILLS = {
    "plan", "spec", "ship", "spike", "pr", "ci", "standards", "tracker", "explain", "init-repo", "help",
    "config",
}
FORBIDDEN_OLD_NAMES = {"explore-sonnet", "seam-scout", "path-tracer", "slice-builder", "bug-hunter", "rev-gate"}

CANONICAL_VERBS = {
    "preflight", "create-issue", "create-child", "get-issue", "update-issue", "find-issues",
    "set-status", "assign", "link-parent", "add-dependency", "add-label", "type-convert",
    "post-comment", "post-log", "attach-artifact", "attachment-download", "attachment-update",
    "link-pr",
}
CANONICAL_STATUSES = {"backlog", "ready", "in-progress", "in-review", "done"}
COLUMN_KEYS = {
    "columns.backlog", "columns.ready", "columns.in_progress", "columns.in_review", "columns.done",
}

# `sy_tools/config.py` is the only legal reader: a name elsewhere is a missed cut-over, or a second
# code path resolving one key.
LEGACY_CONFIG_ENV = {
    "SY_TRACKER", "SY_WORKTREE_ROOT", "SY_MEMORY_DIR", "SY_DEBUG_EVALS", "SY_CI_POLL_TIMEOUT",
    "SY_BACKLOG_COLNAME", "SY_READY_COLNAME", "SY_IN_PROGRESS_COLNAME", "SY_IN_REVIEW_COLNAME",
    "SY_DONE_COLNAME", "SY_FRONTIER_MODEL", "SY_FRONTIER_FALLBACK", "SY_IMAGE_MODEL",
    "SY_DEBATE_MODEL", "SY_GH_PROJECT", "SY_GH_REPO",
}
# Every spelling of "give this agent the whole `sy` server": the bare server name under either
# deployment prefix, and the `__*` suffix form Claude Code documents as equivalent to it.
SERVER_WILDCARD = re.compile(r"mcp__(?:sy|plugin_sy_sy)(?:__\*)?")
# The only two prefixes under which an entry names a real `sy` tool. An entry under neither -- a bare
# `check_env`, or a foreign server's -- grants nothing, however it splits, so every verb extraction
# below must read the prefix rather than the tail.
SY_PREFIXES = ("mcp__sy__", "mcp__plugin_sy_sy__")
MEMORY_WRITE_TOOLS = {"memory_add", "memory_refute"}
# Exactly the tracker-mutation verbs each `/sy:ship` worker's own procedure names: `start-resume.md`
# step 7 sets status and self-assigns, `immutable-gate.md`'s promote step sets status, and
# `implementation.md` names no tracker verb at all. Exact sets, not floors — a worker gaining or
# losing one has to touch this line and the test that pins it.
SHIP_WORKER_TRACKER_VERBS = {
    "ship-start": {"set-status", "assign"},
    "ship-build": set(),
    "ship-gate": {"set-status"},
}
SHIP_WORKER_AGENTS = frozenset(SHIP_WORKER_TRACKER_VERBS)
# Every agent must be able to ask whether a credential is present without ever reading its value;
# `sy_tools/guards/secret_guard.py` names this tool as the remedy it steers shell probes toward.
CHECK_ENV_TOOL = "check_env"
# The lowest number an adapter's `body_limit` could plausibly be. Both real limits are 32k and 64k, and
# no tracker documents anything near this floor, so a declaration under it is a typo, not a limit.
BODY_LIMIT_FLOOR = 8_192
_SCRATCH_HINT = "the `sy` server's `scratch_dir` tool"
_SCRATCH_REF_SUFFIXES = {".md", ".py", ".sh", ".json", ".yml", ".yaml", ".toml"}
_SCRATCH_REF_PATTERN = re.compile(r"(?<![\w.-])\.scratch\b")

# Exempt because each owns a name: the resolver the legacy map, the adapters their own, the docs the
# by-hand move.
CONFIG_ENV_ALLOWED = {
    "sy_tools/config.py",
    "scripts/validate.py",
    "docs/configuration.md",
    "skills/tracker/jira/config-map.json",
    "skills/tracker/github/config-map.json",
}

# Tracker vocabulary legal ONLY inside skills/tracker/ (docs and README are not scanned).
TRACKER_TOKENS = [
    re.compile(p, f) for p, f in [
        (r"\bjira\b", re.I), (r"\bacli\b", re.I), (r"\batlassian\b", re.I),
        (r"\.atlassian\.net", 0), (r"\bADF\b", re.I), (r"md_to_adf", 0),
        (r"\bgh issue\b", re.I), (r"\bgh project\b", re.I), (r"\bgh gist\b", re.I),
        (r"\bsubtask\b", re.I), (r"\bsub-issue\b", re.I), (r"issueType", 0),
        (r"--blocked-by", 0), (r"--add-blocked-by", 0),
        (r"TOOLBOX_", 0), (r"\btoolbox\b", re.I),
    ]
]

# Files documenting a `ci_poll.sh poll` invocation or asserting on one; `check_poller_argv` reads them.
POLLER_CALL_SITES = (
    "agents/ship-gate.md",
    "skills/ci/SKILL.md",
    "skills/ship/references/immutable-gate.md",
    "skills/ship/references/merge-accounting.md",
    "skills/ship/references/handoff-accounting.md",
)

REQUIRED = {
    ".claude-plugin/plugin.json",
    "hooks/hooks.json",
    "config/defaults.json",
    "config/floors.json",
    "config/schema.json",
    "docs/configuration.md",
    "skills/config/SKILL.md",
    "skills/tracker/jira/config-map.json",
    "skills/tracker/github/config-map.json",
    "scripts/ci_poll.sh",
    "scripts/plan_density_check.py",
    "sy_tools/usage.py",
    "sy_tools/eval_events.py",
    "sy_tools/memory.py",
    "sy_tools/preflight.py",
    "sy_tools/secrets.py",
    "sy_tools/guards/secret_guard.py",
    "sy_tools/guards/review_guard.py",
    "sy_tools/guards/spec_gate_cap_guard.py",
    "skills/tracker/SKILL.md",
    "skills/tracker/CONTRACT.md",
    "skills/tracker/jira/ADAPTER.md",
    "skills/tracker/jira/references/attachments.md",
    "skills/tracker/jira/references/migration.md",
    "docs/smoke_mcp.py",
    "skills/tracker/github/ADAPTER.md",
    "skills/ship/references/start-resume.md",
    "skills/ship/references/implementation.md",
    "skills/ship/references/immutable-gate.md",
    "skills/ship/references/handoff-accounting.md",
    "skills/ship/references/merge-accounting.md",
    "skills/shared/references/image-inspection.md",
    "skills/shared/references/memory.md",
    "skills/shared/references/user-interaction.md",
    "skills/shared/references/write-integrity.md",
    "skills/shared/references/scope-discipline.md",
    "skills/shared/references/preflight.md",
    "skills/shared/references/debate.md",
    "skills/shared/references/spec-gate.md",
    "skills/shared/references/model-dispatch.md",
    "skills/shared/references/config-values.md",
    "skills/shared/references/transcript-attach.md",
    "skills/shared/references/context-economy.md",
    "skills/plan/references/new-objective.md",
    "skills/plan/references/reentry.md",
    "skills/plan/references/roadmap-shaping.md",
    "skills/plan/references/checkpoint-handoff.md",
    "skills/standards/references/resolve.md",
    "skills/standards/references/review.md",
    "skills/standards/references/fallback-risk.md",
}


def fail(msg: str, errors: list[str]) -> None:
    errors.append(msg)


def frontmatter(path: Path, errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        fail(f"{path}: missing opening frontmatter delimiter", errors)
        return
    try:
        end = text.index("\n---\n", 4)
    except ValueError:
        fail(f"{path}: missing closing frontmatter delimiter", errors)
        return
    block = text[4:end]
    if not re.search(r"^name:\s*\S+", block, re.M):
        fail(f"{path}: missing name", errors)
    if not re.search(r"^description:\s*(?:>|\||[\"']|\S)", block, re.M):
        fail(f"{path}: missing description", errors)
    if re.search(r"^hooks:\s*$", block, re.M) or re.search(r"^hooks:\s", block, re.M):
        fail(f"{path}: plugin-shipped agents/skills cannot declare hooks; move them to hooks/hooks.json", errors)


def _frontmatter_field(text: str, field: str) -> str:
    """Return a frontmatter field's value, joining an indented YAML block list into one string."""
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        # "" rather than raising: `frontmatter()` already reports both delimiter failures, and a raise
        # here would abort main() before it prints the collected list.
        return ""
    block = text.split("---", 2)[1]
    match = re.search(rf"^{field}:(.*)$", block, re.M)
    if not match:
        return ""
    value = [match.group(1)]
    for line in block[match.end():].splitlines()[1:]:
        if line.strip() and not line.startswith((" ", "\t", "-")):
            break
        value.append(line)
    return " ".join(value)


def _component_md(seam_only: bool) -> list[Path]:
    """Core component markdown: agents/ + skills/, excluding the tracker legal zone when seam_only."""
    paths: list[Path] = []
    for base in ("agents", "skills"):
        for p in (ROOT / base).rglob("*.md"):
            # Excluded at any depth, as in the two seam checks below: not Shipyard's to read, and not
            # guaranteed to be UTF-8 decodable.
            if ".scratch" in p.relative_to(ROOT).parts:
                continue
            if seam_only and "skills/tracker/" in p.as_posix():
                continue
            paths.append(p)
    return paths


def check_structure(errors: list[str]) -> None:
    plugin_dir = ROOT / ".claude-plugin"
    contents = {p.name for p in plugin_dir.iterdir()} if plugin_dir.is_dir() else set()
    allowed = {"plugin.json", "marketplace.json"}
    if "plugin.json" not in contents or not contents <= allowed:
        fail(f".claude-plugin/ must contain plugin.json (and optionally marketplace.json); found {sorted(contents)}", errors)
    manifest = plugin_dir / "plugin.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(f"plugin.json invalid JSON: {exc}", errors)
        else:
            if not data.get("name"):
                fail("plugin.json missing required 'name'", errors)
    marketplace = plugin_dir / "marketplace.json"
    if marketplace.is_file():
        try:
            mkt = json.loads(marketplace.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(f"marketplace.json invalid JSON: {exc}", errors)
        else:
            if not mkt.get("name"):
                fail("marketplace.json missing required 'name'", errors)
            if not mkt.get("plugins"):
                fail("marketplace.json missing required 'plugins'", errors)


def check_no_home_paths(errors: list[str]) -> None:
    for p in _component_md(seam_only=False):
        if "~/.claude" in p.read_text(encoding="utf-8", errors="replace"):
            fail(f"{p.relative_to(ROOT)}: uses ~/.claude; bundle files must use ${{CLAUDE_PLUGIN_ROOT}}", errors)


def check_seam(errors: list[str]) -> None:
    # The check that stops the abstraction eroding under time pressure; the list below is exhaustive
    # because `sy_tools/tests/test_tracker_seam.py` scans that whole package, leaving one executable.
    scan = _component_md(seam_only=True) + [ROOT / "scripts/ci_poll.sh"]
    for p in scan:
        text = p.read_text(encoding="utf-8", errors="replace")
        for pattern in TRACKER_TOKENS:
            m = pattern.search(text)
            if m:
                line = text[: m.start()].count("\n") + 1
                fail(f"SEAM: {p.relative_to(ROOT)}:{line}: tracker token {m.group(0)!r} outside skills/tracker/", errors)
                break


def check_config_seam(errors: list[str]) -> None:
    """No file but the resolver may name a config setting's old environment variable."""
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix not in {".md", ".py", ".sh", ".json"}:
            continue
        parts = p.relative_to(ROOT).parts
        rel = "/".join(parts)
        if rel in CONFIG_ENV_ALLOWED or ".scratch" in parts or parts[0] in {".shipyard", ".git", ".pixi"}:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for var in sorted(LEGACY_CONFIG_ENV):
            if var in text:
                line = text[: text.index(var)].count("\n") + 1
                fail(
                    f"CONFIG SEAM: {rel}:{line}: names retired env var {var}; read it with "
                    "the `sy` server's `get_config` tool instead",
                    errors,
                )
                break


def check_no_repo_scratch_refs(errors: list[str]) -> None:
    """No file Shipyard ships may name a repo-relative scratch directory.

    Not cosmetic: such a path is discarded with every `/sy:ship` worktree it was written in, and for
    `review_guard.py`'s hunt sandbox the guard and the agent it guards resolve it two different ways.
    """
    # Every file on disk by suffix, not a git-tracked query, so a gitignored file is scanned too.
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix not in _SCRATCH_REF_SUFFIXES:
            continue
        parts = p.relative_to(ROOT).parts
        rel = "/".join(parts)
        # Exempt: this file holds the pattern; a scratch directory's contents belong to whoever put them
        # there, not to Shipyard; `.pixi/` is gitignored but on disk once an environment is installed.
        if rel == "scripts/validate.py" or ".scratch" in parts or parts[0] in {".shipyard", ".git", ".pixi"}:
            continue
        # `errors="replace"`: one undecodable byte under a directory nobody authored would raise out and
        # discard every error collected so far.
        text = p.read_text(encoding="utf-8", errors="replace")
        m = _SCRATCH_REF_PATTERN.search(text)
        if m:
            line = text[: m.start()].count("\n") + 1
            fail(
                f"SCRATCH SEAM: {rel}:{line}: names a repo-relative .scratch; resolve it with "
                f"{_SCRATCH_HINT} instead",
                errors,
            )


def check_agent_floors(errors: list[str]) -> None:
    """Every agent has a declared floor and a default binding, and the binding honours the floor.

    Every agent must appear in both config files, so a new one cannot ship unbounded.
    """
    order = ("haiku", "sonnet", "opus", "fable")
    efforts = ("low", "medium", "high", "xhigh", "max")
    try:
        floors = json.loads((ROOT / "config/floors.json").read_text(encoding="utf-8"))
        defaults = json.loads((ROOT / "config/defaults.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"config/floors.json or config/defaults.json unreadable: {exc}", errors)
        return
    tiers = defaults.get("models", {}).get("tiers", {})
    bindings = defaults.get("models", {}).get("agents", {})
    for agent in sorted(EXPECTED_AGENTS):
        if agent not in floors:
            fail(f"config/floors.json: no floor declared for agent {agent!r}", errors)
        if agent not in bindings:
            fail(f"config/defaults.json: no models.agents entry for agent {agent!r}", errors)
        if agent not in floors or agent not in bindings:
            continue
        floor_model = tiers.get(floors[agent].get("min_model"), floors[agent].get("min_model"))
        bound_model = tiers.get(bindings[agent].get("model"), bindings[agent].get("model"))
        # Model tier is a quality floor, not a cost dial: a default under the floor ships weaker silently.
        if floor_model in order and bound_model in order and order.index(bound_model) < order.index(floor_model):
            fail(
                f"config/defaults.json: {agent} defaults to {bound_model!r}, below its declared floor "
                f"{floor_model!r} in config/floors.json",
                errors,
            )
        floor_effort = floors[agent].get("min_effort")
        bound_effort = bindings[agent].get("effort")
        if floor_effort in efforts and bound_effort in efforts and efforts.index(bound_effort) < efforts.index(floor_effort):
            fail(
                f"config/defaults.json: {agent} defaults to effort {bound_effort!r}, below its declared floor "
                f"{floor_effort!r}",
                errors,
            )
        if not floors[agent].get("why"):
            fail(f"config/floors.json: {agent} has no `why`, so the refusal message would be unactionable", errors)
    for agent in sorted(set(floors) - EXPECTED_AGENTS):
        fail(f"config/floors.json: floor declared for unknown agent {agent!r}", errors)


def check_agent_frontmatter_tiers(errors: list[str]) -> None:
    """Every agent declares both model and effort in frontmatter, and neither sits below its floor."""
    order = ("haiku", "sonnet", "opus", "fable")
    efforts = ("low", "medium", "high", "xhigh", "max")
    try:
        floors = json.loads((ROOT / "config/floors.json").read_text(encoding="utf-8"))
        tiers = json.loads((ROOT / "config/defaults.json").read_text(encoding="utf-8"))
        tiers = tiers.get("models", {}).get("tiers", {})
    except (OSError, json.JSONDecodeError):
        return
    for p in sorted((ROOT / "agents").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        block = text[4:text.index("\n---\n", 4)] if text.startswith("---\n") and "\n---\n" in text else ""
        model = re.search(r"^model:\s*(\S+)", block, re.M)
        effort = re.search(r"^effort:\s*(\S+)", block, re.M)
        if not model:
            fail(f"{p.relative_to(ROOT)}: frontmatter declares no model", errors)
        if not effort:
            fail(f"{p.relative_to(ROOT)}: frontmatter declares no effort", errors)
        floor = floors.get(p.stem, {})
        if effort and effort.group(1) not in efforts:
            fail(f"{p.relative_to(ROOT)}: effort {effort.group(1)!r} is not one of {', '.join(efforts)}", errors)
        elif effort and floor.get("min_effort") in efforts:
            if efforts.index(effort.group(1)) < efforts.index(floor["min_effort"]):
                fail(
                    f"{p.relative_to(ROOT)}: effort {effort.group(1)!r} is below the declared floor "
                    f"{floor['min_effort']!r}; effort cannot be raised at dispatch, so frontmatter is the floor",
                    errors,
                )
        floor_model = tiers.get(floor.get("min_model"), floor.get("min_model"))
        if model and model.group(1) in order and floor_model in order:
            if order.index(model.group(1)) < order.index(floor_model):
                fail(
                    f"{p.relative_to(ROOT)}: model {model.group(1)!r} is below the declared floor {floor_model!r}",
                    errors,
                )


def _sy_verb(entry: str) -> str | None:
    """The tool name if `entry` is `mcp__sy__<verb>` or `mcp__plugin_sy_sy__<verb>`, else `None`."""
    for prefix in SY_PREFIXES:
        if entry.startswith(prefix):
            return entry[len(prefix):]
    return None


def check_agent_mcp_allowlists(errors: list[str]) -> None:
    """An agent's `tools:` allowlist reaches the server's tools under both prefixes, never by wildcard, always
    reaches `check_env`, never gives a `/sy:ship` worker a durable-memory write, and gives a `/sy:ship` worker
    exactly the tracker-mutation verbs its own procedure names."""
    for p in sorted((ROOT / "agents").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        block = text[4:text.index("\n---\n", 4)] if text.startswith("---\n") and "\n---\n" in text else ""
        # Horizontal whitespace only, then strip: `\s*(.+)` would cross the newline after a valueless
        # `tools:` and capture the next frontmatter line, validating an empty allowlist as if it named that
        # line's tool.
        declared = re.search(r"^tools:[ \t]*(.*)$", block, re.M)
        value = declared.group(1).strip() if declared else ""
        if declared and not value:
            # A valueless `tools:` is genuinely empty only when nothing follows it in the frontmatter block, or
            # the next real content is a sibling key at column 0 -- two bounded, recognisable shapes. Anything
            # else following it is an explicit allowlist the same-line-only pattern above cannot see, so every
            # check below would be skipped, for a non-ship agent silently, allowlist and all. Recognise the
            # narrow *empty* shape and refuse whatever else follows: enumerating the non-empty continuations
            # instead (block list, then blank-line-preceded, then comment-preceded, then flow sequence, then a
            # column-0 comment) turned up one more sibling every review round, because that set is open-ended
            # and this one is closed. Refusing rather than parsing matches the unrecognised glob shapes further
            # down, and every real agent already uses the single-line comma form.
            tail = block[declared.end():].splitlines()[1:]
            next_real = next((ln for ln in tail if ln.strip() and not ln.lstrip().startswith("#")), None)
            if next_real is not None and not re.match(r"[\w-]+:", next_real):
                fail(
                    f"{p.relative_to(ROOT)}: tools: names nothing on its own line but is not genuinely empty "
                    f"either ({next_real.strip()!r} follows it); rewrite it as the single-line comma-separated "
                    "form `tools: item, item, ...` so the allowlist checks below can read it",
                    errors,
                )
                continue
        if not value:
            if p.stem in SHIP_WORKER_AGENTS:
                fail(
                    f"{p.relative_to(ROOT)}: a /sy:ship worker's tools: must be an explicit, non-empty allowlist; "
                    f"an absent tools field inherits every tool including {'/'.join(sorted(MEMORY_WRITE_TOOLS))}, "
                    "and an empty value launches the worker with no tools at all",
                    errors,
                )
            continue
        named = [entry.strip() for entry in value.split(",")]
        if not all(named):
            # Every check below skips an empty name, so a leading, trailing, or doubled comma would
            # otherwise validate clean and hide the typo it came from.
            fail(
                f"{p.relative_to(ROOT)}: tools has an empty entry from a leading, trailing, or doubled comma "
                f"in {value!r}; name one tool per comma",
                errors,
            )
            named = [entry for entry in named if entry]
        granted = {verb for entry in named if (verb := _sy_verb(entry)) is not None}
        if CHECK_ENV_TOOL not in granted:
            fail(
                f"{p.relative_to(ROOT)}: tools is an explicit allowlist naming no {CHECK_ENV_TOOL!r}, so this "
                "agent cannot ask whether a credential is present without shelling out and leaking its value "
                "into the transcript; name it under both deployment prefixes",
                errors,
            )
        if p.stem in SHIP_WORKER_AGENTS:
            for entry in named:
                if _sy_verb(entry) in MEMORY_WRITE_TOOLS:
                    fail(
                        f"{p.relative_to(ROOT)}: tools names {entry!r}, but only the /sy:ship parent writes the "
                        "user-global memory store; a worker relays a MEMORY_REFUTE candidate instead",
                        errors,
                    )
            declared_verbs = SHIP_WORKER_TRACKER_VERBS[p.stem]
            granted_verbs = granted & CANONICAL_VERBS
            if granted_verbs != declared_verbs:
                missing = sorted(declared_verbs - granted_verbs)
                extra = sorted(granted_verbs - declared_verbs)
                fail(
                    f"{p.relative_to(ROOT)}: this /sy:ship worker's procedure names exactly the tracker verbs "
                    f"[{', '.join(sorted(declared_verbs)) or 'none'}], but tools grants "
                    f"[{', '.join(sorted(granted_verbs)) or 'none'}]"
                    + (f"; missing {missing} (canonical spelling is hyphenated)" if missing else "")
                    + (f"; extra {extra}" if extra else ""),
                    errors,
                )
        for entry in named:
            # Both spellings, because Claude Code documents `mcp__<server>__*` as granting every tool
            # from that server exactly as the bare server name does — and the pair
            # `mcp__sy__*, mcp__plugin_sy_sy__*` also satisfies the twin check below, so the
            # bare-name-only form of this check let a full server grant through clean.
            if SERVER_WILDCARD.fullmatch(entry):
                fail(
                    f"{p.relative_to(ROOT)}: tools names the server-level wildcard {entry!r}, which grants "
                    "every tool including the tracker's mutation verbs; name individual tools instead",
                    errors,
                )
            elif entry.startswith("mcp__") and "*" in entry:
                # Not one of the two documented `tools:` forms above (an exact name or a server-level
                # `mcp__<server>`/`mcp__<server>__*` pattern) -- refuse rather than let an unrecognised
                # glob shape reach the server unnoticed, whether it would over-grant or silently strip
                # the tool it looks like it names.
                fail(
                    f"{p.relative_to(ROOT)}: tools names {entry!r}, neither an exact tool name nor a "
                    "documented server-level pattern; name individual tools instead",
                    errors,
                )
        # `mcp__plugin_sy_sy__` is a marketplace install's prefix, `mcp__sy__` a project `.mcp.json`'s, and an
        # explicit `tools:` list grants nothing it does not name: one prefix silently strands the other.
        for entry in named:
            for prefix, twin in (SY_PREFIXES, SY_PREFIXES[::-1]):
                if entry.startswith(prefix) and twin + entry[len(prefix):] not in named:
                    fail(
                        f"{p.relative_to(ROOT)}: tools names {entry!r} but not its other-deployment twin "
                        f"{twin + entry[len(prefix):]!r}; both prefixes must be listed or one deployment "
                        "cannot reach the tool",
                        errors,
                    )


def check_contract_completeness(errors: list[str]) -> None:
    contract = (ROOT / "skills/tracker/CONTRACT.md").read_text(encoding="utf-8")
    jira = (ROOT / "skills/tracker/jira/ADAPTER.md").read_text(encoding="utf-8")
    github = (ROOT / "skills/tracker/github/ADAPTER.md").read_text(encoding="utf-8")
    for verb in sorted(CANONICAL_VERBS):
        for name, text in (("CONTRACT", contract), ("jira", jira), ("github", github)):
            if verb not in text:
                fail(f"contract completeness: verb {verb!r} missing from {name} adapter/contract", errors)
    for status in sorted(CANONICAL_STATUSES):
        for name, text in (("CONTRACT", contract), ("jira", jira), ("github", github)):
            if status not in text:
                fail(f"contract completeness: status {status!r} missing from {name} mapping", errors)
    for key in sorted(COLUMN_KEYS):
        for name, text in (("CONTRACT", contract), ("jira", jira), ("github", github)):
            if key not in text:
                fail(f"contract completeness: column config key {key} missing from {name}", errors)


def check_hooks(errors: list[str]) -> None:
    """Every pinned hook module runs under each event it is pinned to, and every hook command is runnable."""
    text = (ROOT / "hooks/hooks.json").read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        fail(f"hooks/hooks.json invalid JSON: {exc}", errors)
        return
    # Decoded command strings, not the raw file: each embeds a quoted `${CLAUDE_PLUGIN_ROOT}`, so on disk
    # the quotes are backslash-escaped and a substring test against the file text never matches.
    commands_by_event = {
        event: [hook.get("command", "") for matcher in matchers for hook in matcher.get("hooks", [])]
        for event, matchers in parsed.get("hooks", {}).items()
    }
    commands = [command for event_commands in commands_by_event.values() for command in event_commands]
    # Checked per named event, not against every command in the file joined together: a module wired under one
    # event alone satisfied a flattened substring test even while pinned to run under several.
    # Matched as `python -m <module>`, not as a bare module name: the hook modules live inside a package,
    # so a plain script path naming one would satisfy a substring test yet not be runnable.
    for module, events in (
        ("sy_tools.guards.review_guard", ("PreToolUse",)),
        ("sy_tools.guards.secret_guard", ("PreToolUse",)),
        ("sy_tools.guards.spec_gate_cap_guard", ("PreToolUse",)),
        ("sy_tools.usage", ("Stop", "SubagentStop")),
        ("sy_tools.eval_events", ("PreToolUse", "SubagentStop", "Stop")),
    ):
        for event in events:
            if not any(f"python -m {module}" in command for command in commands_by_event.get(event, [])):
                fail(f"hooks/hooks.json must wire `python -m {module}` under {event}", errors)
    # The plugin root has to reach `sys.path` for `python -m sy_tools.…` to resolve at all, and a hook runs
    # on bare `python`: `pixi run` would make every hook depend on a resolved environment in the caller's repo.
    for command in commands:
        if 'PYTHONPATH="${CLAUDE_PLUGIN_ROOT}"' not in command:
            fail(f"hooks/hooks.json: {command!r} must put the plugin root on PYTHONPATH", errors)
        if "pixi run" in command:
            fail(f"hooks/hooks.json: {command!r} must not route a hook through `pixi run`", errors)


def check_invariants(errors: list[str]) -> None:
    def read(rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    # Read out of the source rather than imported: `python scripts/validate.py` puts `scripts/` on
    # `sys.path`, not the repo root, so importing the adapters needs a `sys.path` shim this file has
    # never needed for anything else. The comment block directly above the declaration comes back with
    # it, because that block is a copy of the figure too and the same anchor already locates it.
    # `None`, not a sentinel figure: a `0` here fell through into the floor leg and reported a second,
    # fabricated fault ("this is a dropped digit") about a declaration that was never read.
    def declared_body_limit(rel: str) -> tuple[int, str] | None:
        match = re.search(r"((?:^ *#.*\n)*)^ {4}body_limit: int = ([\d_]+)$", read(rel), re.MULTILINE)
        if match is None:
            fail(f"{rel} must declare `body_limit: int = <literal>`; its ADAPTER.md figure cannot be checked", errors)
            return None
        return int(match.group(2)), match.group(1)

    # Every grouped or ungrouped spelling of a four-digit-or-longer number in `target`. Used instead of
    # substring containment, which a target satisfies for the wrong reason whenever the declared digits
    # happen to appear inside some unrelated number in its prose.
    #
    # The boundaries are deliberately asymmetric: the lookbehind rejects a leading hyphen so a citation
    # id cannot be read as a figure, but the lookahead rejects a trailing hyphen only when a digit
    # follows it. Making them symmetric loses `32,767-character`, which is how prose actually writes the
    # figure, and a target stating only that reads as figure-free — so the staleness leg passes over
    # stale provenance and the whole check goes quietly vacuous.
    def numeric_tokens(target: str) -> set[str]:
        return set(re.findall(r"(?<![\w-])\d[\d,_]{3,}(?!\w|-\d)", target))

    ship = read("skills/ship/SKILL.md")
    handoff = read("skills/ship/references/handoff-accounting.md")
    merge = read("skills/ship/references/merge-accounting.md")
    gate = read("agents/gate.md")
    spec = read("skills/spec/SKILL.md")
    start = read("skills/ship/references/start-resume.md")
    ship_start_agent = read("agents/ship-start.md")
    ship_build_agent = read("agents/ship-build.md")
    gate_ref = read("skills/ship/references/immutable-gate.md")
    contract = read("skills/tracker/CONTRACT.md")
    impl = read("skills/ship/references/implementation.md")
    img_ref = read("skills/shared/references/image-inspection.md")
    memory_ref = read("skills/shared/references/memory.md")
    plan = read("skills/plan/SKILL.md")
    spike = read("skills/spike/SKILL.md")
    pr = read("skills/pr/SKILL.md")
    explain = read("skills/explain/SKILL.md")
    user_interaction = read("skills/shared/references/user-interaction.md")
    write_integrity = read("skills/shared/references/write-integrity.md")
    scope = read("skills/shared/references/scope-discipline.md")
    preflight_ref = read("skills/shared/references/preflight.md")
    debate_ref = read("skills/shared/references/debate.md")
    spec_gate_ref = read("skills/shared/references/spec-gate.md")
    economy_ref = read("skills/shared/references/context-economy.md")
    contributing = read("CONTRIBUTING.md")
    roadmap_shaping = read("skills/plan/references/roadmap-shaping.md")
    tracker_skill = read("skills/tracker/SKILL.md")
    jira_adapter = read("skills/tracker/jira/ADAPTER.md")
    github_adapter = read("skills/tracker/github/ADAPTER.md")
    init_repo = read("skills/init-repo/SKILL.md")
    checkpoint_handoff = read("skills/plan/references/checkpoint-handoff.md")
    jira_attachments = read("skills/tracker/jira/references/attachments.md")

    if "--match-head-commit" not in merge:
        fail("merge path missing atomic head guard (--match-head-commit)", errors)
    if "decomposed/superseded closure is not delivery" not in ship.lower():
        fail("ship missing decomposed-closure-is-not-delivery rule", errors)
    if "main_plus_subagents" not in handoff or "require_agent" not in handoff:
        fail("ship accounting must aggregate and validate subagent usage", errors)
    if "standalone" not in contract.lower():
        fail("contract must require standalone machine-log comments", errors)
    if "verification obligation" not in spec or "PLAN_BASE_SHA" not in spec:
        fail("spec must define verification obligations and record PLAN_BASE_SHA", errors)
    if "plan_base_sha" not in start:
        fail("ship start/resume must carry plan_base_sha state", errors)
    pregate_fields = (
        "pregate_checkpoint_channel",
        "pregate_checkpoint_cleared_sha",
        "pregate_checkpoint_changes_requested",
        "pregate_checkpoint_gate_dispatched",
        "pregate_checkpoint_request_text",
    )
    if any(field not in start for field in pregate_fields):
        fail(
            "ship start/resume must stamp all five pre-gate checkpoint fields (channel, cleared SHA, "
            "changes-requested count, gate-dispatched flag, request text) at START; a field never written at "
            "START cannot be checked before a later GATE dispatch",
            errors,
        )
    dispatch_fields = (
        "phase_active",
        "gate_rounds_total",
        "gate_rounds_budget_base",
        # The plan pin joins the same check for the same reason: written once at START, read at resume.
        "plan_path",
        "plan_comment_id",
        "plan_version",
    )
    # Scoped to the state block itself, not the whole file: every one of these six is also discussed in the
    # prose below it, so a file-wide check stays green on a field that fell out of the block a START run
    # actually writes — which is the only place "stamped at START" is true or false.
    start_state_block = start.partition("```yaml")[2].partition("```")[0]
    if any(field not in start_state_block for field in dispatch_fields):
        fail(
            "ship start/resume's state block must stamp every per-dispatch field at START (the parent's own "
            "phase_active, GATE's gate_rounds_total and gate_rounds_budget_base, and the plan pin the plan_file call "
            "reported: plan_path, plan_comment_id, plan_version); a field never written at START is absent at "
            "resume, leaving a session that died mid-phase, a spent fix-cycle round budget, or a plan revised "
            "mid-run indistinguishable from a clean start",
            errors,
        )
    # Section-scoped: § Invariants names the tool too, and a file-wide check would stay green on that alone
    # while § State router — the one place the per-session materialisation is procedure — quietly lost it.
    ship_router = ship.partition("## State router")[2].partition("## Completion bar")[0]
    if "calls `plan_file` on the Task" not in ship_router:
        fail(
            "ship SKILL's § State router must name plan_file: materialising the plan runs there, once per session "
            "ahead of any dispatch, because a resume routing straight to BUILD or GATE passes through no phase "
            "procedure that could own it and no worker holds a tracker read",
            errors,
        )
    heading_match = re.search(r"^## Resolve start model$", start, re.MULTILINE)
    if heading_match is None:
        fail(
            "ship start/resume must carry a '## Resolve start model' heading (as its own line, not just "
            "mentioned in prose); the plan_file mention check below is scoped to the procedure text before it, "
            "and a missing or renamed heading leaves nothing correct to scope against",
            errors,
        )
    elif "plan_file" not in start[: heading_match.start()]:
        fail(
            "ship start/resume must name plan_file as where its plan comes from; without it, step 1 reads as a "
            "tracker read this worker does not hold",
            errors,
        )
    if "vN digest" in ship_start_agent:
        fail(
            "agents/ship-start.md still returns the plan as a `vN digest`; START now returns the plan file's path "
            "and pin, and a digest is not something a later phase can read a plan out of",
            errors,
        )
    if "PLAN v<N> @<comment id>; FILE <path>" not in ship_start_agent:
        fail(
            "agents/ship-start.md must return the plan's pin (comment id + version) and file path in its DONE "
            "line; a later phase resuming from this worker's return alone needs both to re-materialise or "
            "compare against state, and a return that only names a digest gives it neither",
            errors,
        )
    for named, text in (("agents/ship-build.md", ship_build_agent), ("immutable-gate.md", gate_ref)):
        if "the plan file the state brief names" not in text:
            fail(
                f"{named} must name the plan file the state brief names as how the plan reaches this phase; "
                "neither BUILD nor GATE holds a tracker read, so a phase told to consult 'the plan' with no file "
                "named has no way to",
                errors,
            )
    stale_economy_claim = "which is the only phase that reads the ticket"
    if stale_economy_claim in economy_ref:
        fail(
            f"context-economy.md still claims {stale_economy_claim!r}; no /sy:ship phase reads the ticket now — the "
            "parent materialises the plan's ship half to a file and hands later phases its path",
            errors,
        )
    if "pregate_checkpoint_channel" not in ship:
        fail(
            "ship SKILL must own the § Pre-gate checkpoint procedure and name pregate_checkpoint_channel; the "
            "pause between BUILD's done and GATE is parent-owned and lives nowhere else",
            errors,
        )
    # Section-scoped, not file-scoped: § State router's own copy of the gate-dispatched guard (below) would
    # otherwise satisfy a file-wide check on its own, leaving these two unfalsifiable. Both sections have to name
    # the fields, since either one alone silently reintroduces the contradiction between them.
    ship_pregate = ship.partition("## Pre-gate checkpoint")[2].partition("## State router")[0]
    if "pregate_checkpoint_gate_dispatched" not in ship_pregate:
        fail(
            "ship SKILL's § Pre-gate checkpoint must carry pregate_checkpoint_gate_dispatched; without that "
            "GATE-re-entry guard it and § State router contradict each other on a resumed fix-cycle commit",
            errors,
        )
    if "pregate_checkpoint_request_text" not in ship_pregate:
        fail(
            "ship SKILL's § Pre-gate checkpoint must carry pregate_checkpoint_request_text; a requested change the "
            "parent never persists leaves the BUILD continuation it dispatches with nothing to resume into",
            errors,
        )
    if "TARGET_SHA" not in gate_ref or "TARGET_SHA" not in merge:
        fail("review pin and merge revalidation must record TARGET_SHA", errors)
    if "process tier" not in handoff:
        fail("handoff must scale records by process tier", errors)
    if "design contract" not in gate or "verification obligation" not in gate:
        fail("gate must verify the design contract and verification obligations", errors)
    if 'agent_model {"name": "img-inspector"}' not in img_ref:
        fail("image-inspection reference must resolve the inspector model via the agent_model tool", errors)
    if "img-inspector" not in gate:
        fail("gate must protect the image-inspection invariant (no image Reads; delegate to sy:img-inspector)", errors)
    if "img-inspector" not in impl:
        fail("build implementation must fan figure inspection out to sy:img-inspector", errors)
    if "REVIEW_BASE_SHA" not in gate or "REVIEWED_SHA" not in gate:
        fail("gate must report immutable base/head coverage", errors)
    if "## Action needed" not in user_interaction or "Optional suggestions" not in user_interaction:
        fail("user-interaction reference must define the Action needed block and the optional-suggestion downgrade", errors)
    wi = write_integrity.lower()
    if "retroactive honesty" not in wi:
        fail("write-integrity reference must define the retroactive-honesty invariant", errors)
    if "denied-write boundary" not in wi:
        fail("write-integrity reference must define the denied-write boundary invariant", errors)
    if "gh pr ready" not in pr:
        fail("pr skill must document the Copilot trigger (gh pr ready)", errors)
    if "attach the scanned transcript" not in handoff or "attach the scanned transcript" not in merge:
        fail("merge authorization must name its follow-on mutations (merge, attach the scanned transcript, set the task done) at the consent point", errors)
    if "write-integrity.md" not in ship or "write-integrity.md" not in pr or "write-integrity.md" not in spec or "write-integrity.md" not in plan:
        fail("ship/pr/spec/plan must each cross-reference the write-integrity reference", errors)
    if "Name the mutation the approval authorizes" not in spec:
        fail("spec sign-off must name the mutations its approval authorizes", errors)
    if "name the mutations the go-ahead covers" not in plan:
        fail("plan approval must name the mutations the go-ahead covers", errors)
    for name, text in (
        ("ship", ship + start + handoff), ("spec", spec), ("plan", plan), ("spike", spike),
        ("pr", pr), ("explain", explain), ("init-repo", init_repo),
    ):
        if "AskUserQuestion" not in text:
            fail(f"{name} skill must route user decisions through AskUserQuestion per user-interaction.md", errors)

    if "## Action needed" not in preflight_ref or "docs/configuration.md" not in preflight_ref:
        fail("preflight reference must define the Action-needed failure shape and link docs/configuration.md", errors)
    # The regression these three catch: a doc describing a separate check-then-record pair rather than
    # the one `preflight` call that caches itself.
    if "`preflight` tool" not in preflight_ref or "cache" not in preflight_ref.lower():
        fail("preflight reference must describe the `preflight` tool's own cached liveness check", errors)
    if "shared cache" not in tracker_skill:
        fail("tracker skill must wire the cached liveness check into its fail-fast section", errors)
    for name, text in (("jira", jira_adapter), ("github", github_adapter)):
        if "shared cache" not in text:
            fail(f"{name} adapter must declare its preflight hook, cached inside the preflight tool", errors)
    for name, text in (("plan", plan), ("spec", spec), ("ship", ship), ("spike", spike)):
        if "preflight.md" not in text:
            fail(f"{name} must run the tracker preflight (preflight.md) before other work", errors)
    if "config.local.json" not in init_repo or "config.json" not in init_repo:
        fail("init-repo must split shared vs per-person config between config.json and config.local.json", errors)
    if "retired variable left set is a hard validation failure" not in init_repo:
        fail("init-repo must state that a retired setting variable left set is a hard failure, not an override", errors)
    if "Secrets never go in either file" not in init_repo:
        fail("init-repo must state that a secret never lands in a config file", errors)
    if "preflight.md" not in init_repo or 'preflight {"force": true}' not in init_repo:
        fail("init-repo must prove config live via the same preflight mechanism other commands use", errors)

    if "scope extension" not in scope or "justify itself" not in scope:
        fail("scope-discipline reference must define recorded scope extensions and the follow-up-justifies-itself default", errors)
    if "scope-discipline.md" not in ship:
        fail("ship invariants must reference scope-discipline (fix small adjacent findings in-branch, not as follow-ups)", errors)
    if "scope-discipline.md" not in gate_ref or "scope extension" not in gate_ref:
        fail("immutable-gate fix cycle must apply scope-discipline (fold-in vs defer, recorded scope extension)", errors)
    if "scope-discipline.md" not in impl:
        fail("build implementation must apply scope-discipline for small adjacent out-of-plan fixes", errors)
    if "scope extension" not in gate:
        fail("gate must treat a recorded scope extension like an accepted deviation, not scope-creep", errors)

    build_agent = read("agents/ship-build.md")
    if "prior-work check" not in spec or "prior-work check" not in plan:
        fail("spec and plan must run the early premise + prior-work check before deep research/shaping", errors)
    if "load-bearing plan facts" not in impl or "load-bearing plan facts" not in build_agent:
        fail("BUILD must verify load-bearing plan facts before executing (needs-decision/bail-to-spec on mismatch)", errors)
    if "content-QA" not in impl:
        fail("build implementation must include the deterministic content-QA grep for leaked wrapper tokens", errors)
    if "docs requiring updates" not in impl:
        fail("build implementation must route the plan's docs requiring updates field into a verification obligation", errors)
    if "pregate_revision" not in impl:
        fail(
            "build implementation must document the pregate_revision slice source; a request-changes continuation "
            "needs a concrete slice type to resume into, not a name only the plan knows",
            errors,
        )
    if "pregate_checkpoint_request_text" not in impl:
        fail(
            "build implementation must name pregate_checkpoint_request_text; BUILD cannot fold a requested revision "
            "into its manifest without naming the field it reads",
            errors,
        )
    if "pregate_checkpoint_gate_dispatched" in impl:
        fail(
            "build implementation must never name pregate_checkpoint_gate_dispatched; that field is "
            "parent-only, and BUILD referencing it violates the ownership boundary the design invariant states",
            errors,
        )
    # Lower-cased unlike most pins here: the sentence names a parent-owned step, so its casing is not load-bearing.
    if "pre-gate checkpoint" not in impl.lower():
        fail(
            "build implementation must state that the parent honours the plan's pre-gate checkpoint after done; "
            "a BUILD that thinks the pause is its own will run or skip a checkpoint it cannot even observe",
            errors,
        )
    for name, text in (("ship", ship), ("implementation", impl), ("ship-build agent", build_agent)):
        if "needs-trace" not in text:
            fail(f"{name} must carry the needs-trace worker return (parent dispatches sy:trace, BUILD never does)", errors)
    trigger = "a second follow-up command still chasing the same question"
    for name, text in (("spec", spec), ("implementation", impl)):
        if trigger not in text:
            fail(f"{name} must state the spot-check bound with the shared trigger clause verbatim", errors)
    for name, text in (("debate reference", debate_ref), ("roadmap-shaping", roadmap_shaping), ("spec", spec), ("spike", spike)):
        if "unconditionally" not in text.lower():
            fail(f"{name} must run the sy:debate pass unconditionally, not gated on a pre-identified fork", errors)

    spec_gate = read("agents/spec-gate.md")
    if "spec-gate.md" not in spec:
        fail("spec must run the pre-sign-off spec-gate pass and cite spec-gate.md", errors)
    if "spec-gate.md" not in spec_gate:
        fail("spec-gate agent must cite the shared checklist in spec-gate.md, never restate it", errors)
    axis_phrase = "the smallest change that delivers the goal"
    if axis_phrase not in spec_gate_ref:
        fail(f"spec-gate reference must define the Simplicity axis with {axis_phrase!r}", errors)
    if axis_phrase in spec_gate:
        fail("spec-gate agent restates an axis definition; cite spec-gate.md instead of copying it", errors)
    if axis_phrase in spec:
        fail("spec restates a spec-gate axis definition; cite spec-gate.md instead of copying it", errors)
    checkpoint_axis_phrase = "not a fact this axis checks against the diff"
    if checkpoint_axis_phrase not in spec_gate_ref:
        fail(
            f"spec-gate reference must define the pre-gate-checkpoint axis as presence-only with "
            f"{checkpoint_axis_phrase!r}",
            errors,
        )
    if checkpoint_axis_phrase in spec_gate:
        fail("spec-gate agent restates the pre-gate-checkpoint axis; cite spec-gate.md instead of copying it", errors)
    if checkpoint_axis_phrase in spec:
        fail("spec restates the pre-gate-checkpoint axis definition; cite spec-gate.md instead of copying it", errors)
    tools_value = _frontmatter_field(spec_gate, "tools").strip()
    if not tools_value:
        fail(
            "spec-gate must declare an explicit tools: list; an absent tools field inherits every tool "
            "including Agent/Skill, which is the violation this pin exists to block",
            errors,
        )
    elif re.search(r"\b(?:Agent|Skill)\b", tools_value):
        fail("spec-gate reviews a plan and dispatches nothing; it must carry no Agent/Skill tool", errors)
    completeness = ("docs requiring updates", "visual-debug obligations", "`pre-gate checkpoint: ")
    if any(field not in spec for field in completeness):
        fail(
            "spec's /sy:ship section must require the docs-sync, visual-debug, and pre-gate-checkpoint "
            "completeness fields",
            errors,
        )
    # Section-scoped on purpose: a whole-file check passes on §2's prose (which legitimately permits a
    # research-phase body edit), a whole-§7 one on Step 1's consent sentence. Widening either disables it.
    spec_s7 = spec.partition("## 7.")[2].partition("## 8.")[0]
    if "update-issue" in spec_s7:
        fail("spec §7 must not reach for update-issue; after approval it posts comments and sets status only", errors)
    if "never writes the Task body" not in spec_s7.partition("### Step 2")[2]:
        fail("spec §7's Step 2 procedure must state it never writes the Task body", errors)
    if "Step 2 — after approval" not in spec:
        fail("spec must keep the staged reveal: full plan posted only in Step 2, after approval", errors)
    # §7 Step 2's summary comment was removed: it restated, on the ticket, a summary the user had just
    # read and approved. Both spellings are pinned because the instruction lived in two places — Step 2's
    # numbered procedure and Step 1's auto-mode consent sentence — and a consent sentence still naming a
    # write the run no longer performs states a false authorization.
    if "post the Step-1 summary" in spec_s7:
        fail("spec §7 must not post the Step-1 summary back as a second comment restating the plan", errors)
    if "post this summary as a comment on the Task" in spec:
        fail("spec's sign-off consent sentence names a summary-comment write §7 Step 2 no longer performs", errors)

    # Context economy is a single copy: the two cut tests are phrased once, in the reference, and every
    # authoring surface carries a pointer to it. A consumer that spells a cut test out has forked the rule.
    cut_tests = (
        "Does removing this sentence change what its reader does?",
        "Would a pointer do the work this text is doing?",
    )
    for cut_test in cut_tests:
        if cut_test not in economy_ref:
            fail(f"context-economy.md must state the cut test {cut_test!r} verbatim", errors)
    economy_consumers = (
        ("spec", spec),
        ("plan", plan),
        ("pr", pr),
        ("handoff-accounting", handoff),
        ("CONTRIBUTING.md", contributing),
    )
    for name, text in economy_consumers:
        if "context-economy.md" not in text:
            fail(f"{name} authors an agent-facing artifact and must cite context-economy.md", errors)
        for cut_test in cut_tests:
            if cut_test in text:
                fail(f"{name} restates a context-economy cut test; cite context-economy.md instead", errors)
    economy_axis_phrase = "narrates what a cited anchor already shows"
    if economy_axis_phrase not in spec_gate_ref:
        fail(f"spec-gate reference's Simplicity axis must state the prose trigger {economy_axis_phrase!r}", errors)
    if economy_axis_phrase in spec_gate:
        fail("spec-gate agent restates the prose-economy trigger; cite spec-gate.md instead of copying it", errors)
    if economy_axis_phrase in spec:
        fail("spec restates the prose-economy trigger; cite spec-gate.md instead of copying it", errors)

    # An oversized plan is shortened, never split: `plan_file` materialises one comment's one agent-facing
    # half, so a plan spread over two comments is one no later phase can read back. The staleness leg is
    # the load-bearing one — the superseded sentence offered splitting as the remedy for every writer.
    if "Split oversized content across writes, or shorten it." in contract:
        fail("contract still offers splitting first; the remedy is shortening, and a plan is never split", errors)
    if "a plan comment is never split across comments" not in contract:
        fail("contract must state that a plan comment is never split across comments", errors)
    # Section-scoped for the same reason as the "never writes the Task body" pin above: the pass has to be
    # Step 2's own first action, and the literal carries the ordering so neither the check's invocation nor
    # its position before the SUPERSEDED edit can be deleted while the pass text survives.
    density_pin = "The rewrite and `scripts/plan_density_check.py` both complete before any tracker mutation."
    if density_pin not in spec_s7.partition("### Step 2")[2]:
        fail("spec §7's Step 2 must run the density pass and its check before any tracker mutation", errors)

    # Each adapter's body limit lives in the constant, again in the comment above it carrying that
    # figure's provenance, and again in its agent-facing ADAPTER.md prose. The Protocol docstring is no
    # longer a target: it states the contract and names no figure, because a core module may not name a
    # concrete tracker (CONTRIBUTING.md) and a figure is worthless without its tracker-specific
    # provenance. Both spellings count everywhere, because the prose groups thousands and the code does not.
    for name, source, doc_rel, doc in (
        ("jira", "sy_tools/tracker/jira/adapter.py", "skills/tracker/jira/ADAPTER.md", jira_adapter),
        ("github", "sy_tools/tracker/github/adapter.py", "skills/tracker/github/ADAPTER.md", github_adapter),
    ):
        declared = declared_body_limit(source)
        if declared is None:
            continue
        limit, note = declared
        spellings = {str(limit), f"{limit:,}", f"{limit:_}"}
        # A dropped leading digit used to survive substring containment by colliding with the grouped
        # spelling it came from — `2_767` occurs inside every target stating `32,767` — cutting the limit
        # tenfold with every doc stale. Whole-token matching closes that, and a floor closes the rest of
        # the class without parsing prose: no tracker limit is anywhere near this low.
        if limit < BODY_LIMIT_FLOOR:
            fail(
                f"{name} adapter's body_limit is {limit} ({source}), under the {BODY_LIMIT_FLOOR} floor; no "
                "tracker limit is that low, so this is a dropped digit, not a limit",
                errors,
            )
        if not spellings & numeric_tokens(doc):
            fail(f"{name} adapter's body_limit is {limit} ({source}); {doc_rel} states no such figure", errors)
        # The comment above the declaration is the copy the doc leg never reads, so it is checked for two
        # faults. Detachment: the anchor only reaches a block sitting immediately above the declaration, so
        # one blank line between them emptied `note` and left this leg passing on a stale figure. Staleness:
        # every figure the block does state is the declared one.
        if not note.strip():
            fail(
                f"{name} adapter's body_limit is {limit} ({source}) with no comment directly above it; the "
                "provenance comment must sit on the lines immediately preceding the declaration",
                errors,
            )
        stale = sorted(numeric_tokens(note) - spellings)
        if stale:
            fail(
                f"{name} adapter's body_limit is {limit} ({source}); the comment above the declaration "
                f"still states {stale[0]}",
                errors,
            )

    # `post-comment` takes `human` and `agent_detail`, both required, and assembles the boundary itself.
    # These are the highest-traffic call sites, so a reference still describing one hand-composed body
    # would have every future session write the shape the tool now refuses. The lower-traffic sites
    # (`# SEAMS`, shelve evidence, the decomposition note, the spike verdict) are covered by gate's diff
    # read instead: one hardcoded regex per prose paragraph is what makes this file unmaintainable.
    for name, text in (
        ("spec §7", spec_s7),
        ("checkpoint-handoff", checkpoint_handoff),
        ("handoff-accounting's retrospective", handoff.partition("## 1.")[2].partition("## 2.")[0]),
    ):
        if "human" not in text or "agent_detail" not in text:
            fail(f"{name} must name post-comment's human/agent_detail parts, not a hand-composed body", errors)
    # `post-log` takes the record as an object it serialises and fences: a reference still showing a
    # hand-built ```json block teaches the caller-composed shape the separate tool exists to remove.
    for name, text in (
        ("handoff-accounting's usage section", handoff.partition("## 2.")[2].partition("## 3.")[0]),
        ("handoff-accounting's metrics section", handoff.partition("## 3.")[2].partition("## 4.")[0]),
        ("merge-accounting", merge),
    ):
        if "post-log" not in text or "title" not in text or "payload" not in text:
            fail(f"{name} must post its machine log through post-log with a title and a payload object", errors)
    if "tracker ticket" not in pr:
        fail("pr description contract must require a link to the tracker ticket", errors)

    if "undispositioned actionable finding" not in gate_ref:
        fail("immutable-gate fix cycle must state the stopping rule (no undispositioned actionable finding)", errors)
    if "drift re-check" not in gate_ref.lower():
        fail("immutable-gate loop must include the periodic target-branch drift re-check", errors)
    if "ci_poll.sh" not in gate_ref:
        fail("immutable-gate must route CI waits through the shared scripts/ci_poll.sh poller", errors)
    if "gate_false_pass" not in handoff or "human backstop" not in gate:
        fail("gate human-backstop note and ship metrics gate_false_pass field are required (shadow-run backstop)", errors)

    if "config_fingerprint" not in start or "config_fingerprint" not in gate_ref:
        fail("ship state must stamp config_fingerprint at START and compare it before review", errors)

    dispatch_ref = read("skills/shared/references/model-dispatch.md")
    if "does not inherit a model override" not in dispatch_ref:
        fail("model-dispatch must state that a nested Agent call does not inherit a model override", errors)
    if "no effort parameter on the `Agent` tool" not in dispatch_ref:
        fail("model-dispatch must state plainly that effort cannot be set at dispatch time", errors)
    if 'agent_model {"name": "<agent-name>"}' not in dispatch_ref:
        fail("model-dispatch must resolve the model through the agent_model tool", errors)
    for name, text in (
        ("gate", read("agents/gate.md")), ("debate", read("agents/debate.md")),
        ("ship-build", read("agents/ship-build.md")), ("ship-gate", read("agents/ship-gate.md")),
        ("ship-start", read("agents/ship-start.md")),
    ):
        if "model-dispatch.md" not in text:
            fail(f"agent {name} dispatches subagents and must cite model-dispatch.md", errors)
    if "config/floors.json" not in read("skills/ship/SKILL.md"):
        fail("ship SKILL must point the quality-floor invariant at config/floors.json, not prose alone", errors)
    if "config/floors.json" not in read("skills/spec/SKILL.md"):
        fail("spec SKILL must point the quality-floor invariant at config/floors.json, not prose alone", errors)

    # Read side and write side check different tools on purpose: `memory_add` alone never reads memory
    # back, and `memory_list` alone never writes the lesson.
    for name, text in (("plan", plan), ("spec", spec), ("ship start", start)):
        if "memory_list" not in text or "memory.md" not in text:
            fail(f"{name} must read durable cross-session memory back (memory_list, per memory.md)", errors)
    if "memory_add" not in handoff or "memory.md" not in handoff:
        fail("ship handoff retro must distill durable lessons into cross-session memory (memory_add, per memory.md)", errors)
    if "memory_refute" not in memory_ref or "never left standing" not in memory_ref:
        fail("memory.md must document memory_refute and that a refuted anchor is never left standing", errors)
    if "delete-by-hand is deliberate friction" in memory_ref.lower():
        fail(
            "memory.md must not restore its old 'delete-by-hand is deliberate friction' line: refuting, "
            "not hand-deleting, is how a wrong lesson is retired",
            errors,
        )
    for name, text in (("plan", plan), ("spec", spec)):
        if "memory_refute" not in text:
            fail(f"{name} runs as the parent session and must refute a contradicted lesson directly", errors)
    for name, text in (("start-resume", start), ("implementation", impl), ("immutable-gate", gate_ref)):
        if "memory_refutations" not in text or "MEMORY_REFUTE" not in text:
            fail(f"{name} must relay a contradicted anchor as a MEMORY_REFUTE candidate in memory_refutations", errors)
    if "memory_refutations" not in ship or "memory_refute" not in ship:
        fail("ship SKILL must carry memory_refutations in the done payload and the parent-applies-it rule", errors)
    # Section-scoped on purpose, and for the same reason in both cases: § Worker contract's drain covers an
    # in-flight return only and § Pre-gate checkpoint is written around a fresh BUILD `done`, but a resume can
    # route straight to BUILD or GATE and passes through no phase procedure that could own either rule, so the
    # router the parent always loads must carry its own copy of both.
    ship_router = ship.partition("## State router")[2].partition("## Completion bar")[0]
    ship_worker = ship.partition("## Worker contract")[2].partition("## Pre-gate checkpoint")[0]
    _worker_pos, _pregate_pos, _router_pos, _completion_pos = (
        ship.find("## Worker contract"), ship.find("## Pre-gate checkpoint"),
        ship.find("## State router"), ship.find("## Completion bar"),
    )
    if -1 in (_worker_pos, _pregate_pos, _router_pos, _completion_pos) or not (
        _worker_pos < _pregate_pos < _router_pos < _completion_pos
    ):
        fail(
            "ship SKILL must keep § Worker contract, § Pre-gate checkpoint, § State router, and § Completion bar "
            "present and in that order; a missing or reordered section lets a section-scoped pin's slice silently "
            "widen to swallow a neighbouring section instead of failing loud",
            errors,
        )
    if "memory_refutations" not in ship_router or "memory_refute" not in ship_router:
        fail("ship SKILL's state router must drain pending memory_refutations on resume before dispatching", errors)
    if "pregate_checkpoint_cleared_sha" not in ship_router:
        fail(
            "ship SKILL's state router must re-check the pre-gate checkpoint's cleared SHA on a resume routing to GATE",
            errors,
        )
    if "pregate_checkpoint_gate_dispatched" not in ship_router:
        fail(
            "ship SKILL's state router must scope its pre-gate re-check by pregate_checkpoint_gate_dispatched; an "
            "unscoped re-check fires again on a resumed GATE fix cycle, one layer below the § Pre-gate checkpoint fix",
            errors,
        )
    if "pregate_checkpoint_request_text" not in ship_router:
        fail(
            "ship SKILL's state router must re-check pregate_checkpoint_request_text; without that override a "
            "resume mid-BUILD-continuation can misclassify to GATE instead of BUILD, double-incrementing "
            "pregate_checkpoint_changes_requested or letting an escape leave a stale request for BUILD to refold",
            errors,
        )
    # Section-scoped for the same reason again: § Worker contract owns the write half of phase_active (stamped
    # before each dispatch, cleared on every return) and § State router the resume-read half, so a file-wide pin
    # is satisfied by whichever section still names it and neither half is really checked.
    if "phase_active" not in ship_worker:
        fail(
            "ship SKILL's § Worker contract must stamp and clear phase_active around every dispatch; a field the "
            "router reads at resume but no dispatch ever writes can only ever report nothing in flight",
            errors,
        )
    if "phase_active" not in ship_router:
        fail(
            "ship SKILL's § State router must check phase_active in its pre-dispatch step; a resume routing "
            "straight to BUILD or GATE otherwise trusts a checkpoint left by a phase that never confirmed it "
            "finished, and the stale flag reports the same crash on every later resume",
            errors,
        )
    for agent in ("ship-start", "ship-build", "ship-gate"):
        if "MEMORY_REFUTE" not in read(f"agents/{agent}.md"):
            fail(f"agent {agent} must carry MEMORY_REFUTE in its return-contract status block", errors)
    if "close with evidence" not in spec.lower() or "shelve" not in spec.lower():
        fail("spec must bless the shelve terminal state (close with evidence, no plan)", errors)
    for name, text in (
        ("ship", ship), ("start-resume", start), ("implementation", impl),
        ("immutable-gate", gate_ref), ("merge-accounting", merge),
    ):
        if "worktree.root" not in text:
            fail(f"{name} must resolve the worktree root via the worktree.root config key", errors)

    gate_fm = gate.split("---", 2)[1]
    if "Skill" not in gate_fm:
        fail("gate agent must allow the Skill tool", errors)
    if re.search(r"^skills:\s*$", gate_fm, re.M):
        fail("gate must not preload standards; conformance review is invoked lazily", errors)
    if 'agent_model {"name": "gate"}' not in gate_ref:
        fail("immutable-gate must resolve the reviewer via the agent_model tool", errors)
    if "models.tiers.frontier_fallback" not in gate_ref:
        fail("immutable-gate must resolve the one-shot reviewer fallback from models.tiers.frontier_fallback", errors)
    if "START <model> / BUILD <model> / GATE <model>" not in spec:
        fail("spec ship profile must name START, BUILD, and GATE models individually (no single-word tier)", errors)
    if "Resolve START's and BUILD's models explicitly" not in ship:
        fail("ship invariants must resolve START and BUILD models explicitly as Agent model overrides", errors)
    for name, text, phase in (("start-resume", start, "start"), ("implementation", impl, "build")):
        tokens = (
            f"{phase.upper()}_MODEL", f"{phase}_model_requested", f"{phase}_model_observed",
            "Agent invocation", "model override",
        )
        missing = [t for t in tokens if t not in text]
        if missing:
            fail(f"{name} must resolve the {phase} model and pass it as the Agent invocation's model override (missing: {', '.join(missing)})", errors)

    # A value re-pasted at the site instead of resolved drifts from the shipped config silently, and
    # nothing else catches it.
    config_values_ref = read("skills/shared/references/config-values.md")
    for name, text in (
        ("ship", ship), ("spec", spec), ("spike", spike), ("gate", gate), ("plan", plan),
    ):
        if "limits.max_depth_agents" not in text:
            fail(f"{name} must name limits.max_depth_agents rather than a hardcoded depth-agent cap", errors)
    for name, text in (
        ("tracker", tracker_skill), ("plan", plan), ("roadmap-shaping", roadmap_shaping),
        ("checkpoint-handoff", checkpoint_handoff),
    ):
        if "plan.max_active_tasks" not in text:
            fail(f"{name} must name plan.max_active_tasks rather than a hardcoded active-task cap", errors)
    for name, text in (
        ("plan", plan), ("spec", spec), ("ship", ship), ("handoff-accounting", handoff),
        ("merge-accounting", merge), ("jira-attachments", jira_attachments),
    ):
        if "transcript.attach" not in text:
            fail(f"{name} must gate transcript rendering/attachment on transcript.attach", errors)
    for name, text in (("ship", ship), ("immutable-gate", gate_ref), ("pr", pr)):
        if "ship.request_ci_reviewer" not in text:
            fail(f"{name} must gate the automated-reviewer request on ship.request_ci_reviewer", errors)
    for name, text in (("pr", pr), ("merge-accounting", merge)):
        if "ship.merge_strategy" not in text:
            fail(f"{name} must resolve ship.merge_strategy rather than hardcoding a merge strategy", errors)
    escalation_keys = (
        "ship.escalation.max_needs_decision",
        "ship.escalation.max_needs_trace",
        "ship.escalation.max_gate_rounds",
    )
    if any(key not in ship for key in escalation_keys):
        fail(
            "ship must name all three escalation thresholds (ship.escalation.max_needs_decision/max_needs_trace/"
            "max_gate_rounds) rather than vague escalation thresholds",
            errors,
        )
    if "ship.escalation.max_gate_rounds" not in gate_ref:
        fail(
            "immutable-gate's fix cycle must bound its rounds on ship.escalation.max_gate_rounds; a loop whose only "
            "stopping rule is its own convergence judgment spends the user's budget without ever asking them",
            errors,
        )
    if "gate_rounds_total" not in ship or "gate_rounds_budget_base" not in ship:
        fail(
            "ship must name both gate_rounds_total and the gate_rounds_budget_base its max_gate_rounds disposition "
            "stamps; a budget raise that moves neither re-breaches the cap on the very next round, and one that "
            "resets the total instead destroys the durable metric that flags a run which never converged",
            errors,
        )
    if "spec.light_tier_max_files" not in spec:
        fail("spec must name spec.light_tier_max_files rather than an undefined 'small' threshold", errors)
    if "spec.max_spec_gate_rounds" not in spec_gate_ref or "spec_gate_cap_guard" not in spec_gate_ref:
        fail(
            "spec-gate must name both spec.max_spec_gate_rounds and the spec_gate_cap_guard that enforces it; a "
            "per-session dispatch budget with no named enforcement point holds only while the session chooses to "
            "honour it, which is the judgment the backstop exists to bound",
            errors,
        )
    for name, text in (
        ("ship", ship), ("spec", spec), ("spike", spike), ("plan", plan), ("pr", pr),
        ("immutable-gate", gate_ref), ("handoff-accounting", handoff), ("merge-accounting", merge),
        ("tracker", tracker_skill), ("roadmap-shaping", roadmap_shaping),
        ("checkpoint-handoff", checkpoint_handoff), ("jira-attachments", jira_attachments),
        ("gate", gate),
    ):
        if "config-values.md" not in text:
            fail(f"{name} names a live-resolved config value and must cite config-values.md", errors)
    if "shipped default" not in config_values_ref or "never restate" not in config_values_ref.lower():
        fail("config-values.md must forbid restating a shipped default as prose", errors)


def check_poller_argv(errors: list[str]) -> None:
    """Every documented poller invocation keeps the selector first, ahead of any flag."""
    pattern = re.compile(r"ci_poll\.sh poll\s+(\S+)")
    for rel in POLLER_CALL_SITES:
        found = pattern.findall((ROOT / rel).read_text(encoding="utf-8", errors="replace"))
        if not found:
            fail(f"{rel}: documents the CI wait and must invoke the shared poller by name", errors)
        for token in found:
            if token.startswith("-"):
                # the end-of-run hygiene assertions match `pgrep -f "ci_poll.sh poll <pr>"`, so a flag
                # slipped in front of the selector leaves a live poller undetectable rather than failing
                fail(f"{rel}: ci_poll.sh poll takes the selector first, not {token!r}", errors)


def run_self_test(rel: str, errors: list[str]) -> None:
    script = ROOT / rel
    if not script.is_file():
        return
    runner = ["bash", str(script)] if script.suffix == ".sh" else [sys.executable, str(script)]
    proc = subprocess.run(
        [*runner, "self-test"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if proc.returncode != 0:
        fail(f"{rel} self-test failed: {proc.stderr.strip()}", errors)


def main() -> int:
    errors: list[str] = []

    for rel in REQUIRED:
        if not (ROOT / rel).is_file():
            fail(f"missing required file: {rel}", errors)

    agents = {p.stem for p in (ROOT / "agents").glob("*.md")}
    skills = {p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md")}
    if agents != EXPECTED_AGENTS:
        fail(f"agent set mismatch: {sorted(agents)}", errors)
    if skills != EXPECTED_SKILLS:
        fail(f"skill set mismatch: {sorted(skills)}", errors)

    agent_paths = list((ROOT / "agents").glob("*.md"))
    skill_paths = list((ROOT / "skills").glob("*/SKILL.md"))
    for p in agent_paths + skill_paths:
        frontmatter(p, errors)

    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in {".md", ".py", ".sh"} or p.name == "validate.py":
            continue
        parts = p.relative_to(ROOT).parts
        if ".scratch" in parts or parts[0] in {".shipyard", ".git", ".pixi"}:
            continue
        rel = "/".join(parts)
        text = p.read_text(encoding="utf-8", errors="replace")
        for old in FORBIDDEN_OLD_NAMES:
            if old in text:
                fail(f"{rel}: stale agent name {old}", errors)

    for p in agent_paths:
        text = p.read_text(encoding="utf-8")
        if "Return contract" not in text:
            fail(f"{p.relative_to(ROOT)}: missing compact Return contract", errors)
        if "SPLIT_REQUIRED" not in text:
            fail(f"{p.relative_to(ROOT)}: missing SPLIT_REQUIRED overflow contract", errors)

    check_structure(errors)
    check_no_home_paths(errors)
    check_seam(errors)
    check_config_seam(errors)
    check_no_repo_scratch_refs(errors)
    check_agent_floors(errors)
    check_agent_frontmatter_tiers(errors)
    check_agent_mcp_allowlists(errors)
    check_contract_completeness(errors)
    check_hooks(errors)
    check_invariants(errors)
    check_poller_argv(errors)

    # Here rather than in pytest because none of them is reachable there: one is bash, and the others
    # sit outside the `sy_tools` tree pytest's `testpaths` collects.
    run_self_test("scripts/ci_poll.sh", errors)
    run_self_test("docs/smoke_mcp.py", errors)
    run_self_test("scripts/plan_density_check.py", errors)

    if errors:
        print("Shipyard validation FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Shipyard validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
