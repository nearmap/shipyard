#!/usr/bin/env python3
"""Validate the Shipyard plugin: structure, frontmatter, the tracker seam, and contract completeness.

Run before loading or releasing the plugin. The seam check is the load-bearing one: it fails if any
core file (outside skills/tracker/) names a specific tracker, which is what stops the abstraction
eroding the first time something gets patched in a hurry.
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

# Settings that used to be environment variables. The two resolvers are now the only readers; any
# other file naming one is either a missed cut-over or a second resolution path for one key.
LEGACY_CONFIG_ENV = {
    "SY_TRACKER", "SY_WORKTREE_ROOT", "SY_MEMORY_DIR", "SY_DEBUG_EVALS", "SY_CI_POLL_TIMEOUT",
    "SY_BACKLOG_COLNAME", "SY_READY_COLNAME", "SY_IN_PROGRESS_COLNAME", "SY_IN_REVIEW_COLNAME",
    "SY_DONE_COLNAME", "SY_FRONTIER_MODEL", "SY_FRONTIER_FALLBACK", "SY_IMAGE_MODEL",
    "SY_DEBATE_MODEL", "SY_GH_PROJECT", "SY_GH_REPO",
}
# The resolvers own the legacy map; the adapters own their own names; the docs explain the
# migration. Everything else must go through a resolver.
_SCRATCH_HINT = "the `sy` server's `scratch_dir` tool"
_SCRATCH_REF_SUFFIXES = {".md", ".py", ".sh", ".json", ".yml", ".yaml", ".toml"}
_SCRATCH_REF_PATTERN = re.compile(r"(?<![\w.-])\.scratch\b")

CONFIG_ENV_ALLOWED = {
    "scripts/sy_config.py",
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
        (r"\.atlassian\.net", 0), (r"\bADF\b", 0), (r"md_to_adf", 0),
        (r"\bgh issue\b", re.I), (r"\bgh project\b", re.I), (r"\bgh gist\b", re.I),
        (r"\bsubtask\b", re.I), (r"\bsub-issue\b", re.I), (r"issueType", 0),
        (r"--blocked-by", 0), (r"--add-blocked-by", 0),
        (r"TOOLBOX_", 0), (r"\btoolbox\b", re.I),
    ]
]

REQUIRED = {
    ".claude-plugin/plugin.json",
    "hooks/hooks.json",
    "config/defaults.json",
    "config/floors.json",
    "config/schema.json",
    "docs/configuration.md",
    "scripts/sy_config.py",
    "skills/config/SKILL.md",
    "skills/tracker/jira/config-map.json",
    "skills/tracker/github/config-map.json",
    "scripts/ci_poll.sh",
    "sy_tools/usage.py",
    "sy_tools/eval_events.py",
    "sy_tools/memory.py",
    "sy_tools/preflight.py",
    "sy_tools/secrets.py",
    "sy_tools/guards/secret_guard.py",
    "sy_tools/guards/review_guard.py",
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
    """Return a frontmatter field's value, joining an indented YAML block list into one string.

    Malformed frontmatter yields "" rather than raising: `frontmatter()` already records those two
    delimiter failures as errors, and a raise here would abort main() before it prints the list.
    """
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
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
    """Core component markdown: agents/ + skills/, excluding the tracker legal zone when seam_only.

    `.scratch` is excluded at any depth for the same reason `check_config_seam` and
    `check_no_repo_scratch_refs` exclude it: it is not Shipyard's to read, and its content is not
    guaranteed to be UTF-8 decodable.
    """
    paths: list[Path] = []
    for base in ("agents", "skills"):
        for p in (ROOT / base).rglob("*.md"):
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
    # `scripts/ci_poll.sh` is the only executable left to name here: every other script this list once
    # carried now lives under `sy_tools/`, where `sy_tools/tests/test_tracker_seam.py` scans the whole
    # package rather than an enumerated list, so naming them again would be a second, weaker check.
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
    """No file but the resolver may name a config setting's old environment variable.

    Same shape as check_seam: one mechanical rule replacing a promise that a settings value is
    read in exactly one place. A stray name here means two code paths resolve one key, which is
    what made the old secret/config boundary illegible.
    """
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

    Scratch lives under the resolved `scratch.dir` now, keyed per identifier. A reintroduced
    repo-relative path in Shipyard's own agents/scripts/skill docs is not cosmetic: it is fragile
    exactly where it is used most, discarded with every `/sy:ship` worktree it was written in, and
    — for `review_guard.py`'s hunt sandbox — a boundary the guard and the agent it guards would
    resolve two different ways.

    This walks every file in the checkout by suffix — not a git-tracked-files query, so a gitignored
    file is scanned too — and never asks whether a `.scratch/` directory exists at all: that
    directory is not Shipyard's to police. Something else on this machine may depend on it for
    entirely unrelated reasons, so `.gitignore` keeps excluding it (it is not this migration's to
    remove either) and this check does not scan its contents, at any depth: whatever is in there
    belongs to whoever put it there, not to Shipyard.

    `.pixi/` is skipped because it is gitignored but materialised on disk once an environment is
    installed, and `errors="replace"` is not decoration either: an undecodable byte anywhere under a
    directory nobody authored would raise out of this check and discard every error the whole run had
    already collected.
    """
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix not in _SCRATCH_REF_SUFFIXES:
            continue
        parts = p.relative_to(ROOT).parts
        rel = "/".join(parts)
        if rel == "scripts/validate.py" or ".scratch" in parts or parts[0] in {".shipyard", ".git", ".pixi"}:
            continue
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
    """Every agent has a declared floor and a default binding, and its frontmatter honours both.

    Shipyard's central invariant — model tier is a quality floor, not a cost dial — lived only in
    prose across six files. This is the deterministic half: the shipped default may not sit below
    the shipped floor, and every agent must appear in both files so a new agent cannot ship
    unbounded.
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
    """Every agent declares both model and effort, and neither sits below its floor.

    Frontmatter is the load-time floor: model is overridden at dispatch from resolved config, but
    effort cannot be, so a missing or below-floor `effort:` is the one that silently ships weaker.
    """
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


def check_agent_mcp_allowlists(errors: list[str]) -> None:
    """An agent's `tools:` allowlist reaches the server's tools under both prefixes, and never by wildcard.

    A subagent whose definition declares an explicit `tools:` list gets no MCP tool that is not named in
    it, so an agent that resolves its own config needs the tool spelled out. The exposed name carries a
    deployment-dependent prefix — `mcp__plugin_sy_sy__<tool>` for a marketplace install, `mcp__sy__<tool>`
    where a project-level `.mcp.json` provides the server — and naming only one silently breaks the other
    deployment, with config resolution simply unreachable rather than failing loudly. Hence the pairing
    rule. The wildcard rule is the sharper one: a server-level `mcp__sy` grants every tool including the
    tracker's mutation verbs, which would hand `create-issue` and `set-status` to the read-only review
    agents whose whole contract is that they cannot write.
    """
    for p in sorted((ROOT / "agents").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        block = text[4:text.index("\n---\n", 4)] if text.startswith("---\n") and "\n---\n" in text else ""
        declared = re.search(r"^tools:\s*(.+)$", block, re.M)
        if not declared:
            continue
        named = [entry.strip() for entry in declared.group(1).split(",")]
        for entry in named:
            if entry in {"mcp__sy", "mcp__plugin_sy_sy"}:
                fail(
                    f"{p.relative_to(ROOT)}: tools names the server-level wildcard {entry!r}, which grants "
                    "every tool including the tracker's mutation verbs; name individual tools instead",
                    errors,
                )
        for entry in named:
            for prefix, twin in (("mcp__sy__", "mcp__plugin_sy_sy__"), ("mcp__plugin_sy_sy__", "mcp__sy__")):
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
    text = (ROOT / "hooks/hooks.json").read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        fail(f"hooks/hooks.json invalid JSON: {exc}", errors)
        return
    # Checked against the decoded command strings, not the raw file: every command embeds a quoted
    # `${CLAUDE_PLUGIN_ROOT}`, so on disk the quotes are backslash-escaped and a substring test against
    # the file text silently never matches.
    commands = [
        hook.get("command", "")
        for matchers in parsed.get("hooks", {}).values()
        for matcher in matchers
        for hook in matcher.get("hooks", [])
    ]
    joined = "\n".join(commands)
    # Matched as `python -m <module>` rather than as a filename: the hook modules live inside a package,
    # so a bare `review_guard` substring would also match a leftover pre-package script path under
    # `scripts/` and pass on a registration that cannot run.
    for module, where in (
        ("sy_tools.guards.review_guard", "PreToolUse"),
        ("sy_tools.guards.secret_guard", "PreToolUse"),
        ("sy_tools.usage", "Stop/SubagentStop"),
        ("sy_tools.eval_events", "PreToolUse/SubagentStop/Stop"),
    ):
        if f"python -m {module}" not in joined:
            fail(f"hooks/hooks.json must wire `python -m {module}` ({where})", errors)
    # The plugin root has to reach `sys.path` for `python -m sy_tools.…` to resolve at all, and a hook
    # runs on bare `python` with no environment of its own — never through `pixi run`, which would make
    # every hook depend on a resolved pixi environment in the consuming repo.
    for command in commands:
        if 'PYTHONPATH="${CLAUDE_PLUGIN_ROOT}"' not in command:
            fail(f"hooks/hooks.json: {command!r} must put the plugin root on PYTHONPATH", errors)
        if "pixi run" in command:
            fail(f"hooks/hooks.json: {command!r} must not route a hook through `pixi run`", errors)


def check_invariants(errors: list[str]) -> None:
    def read(rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    ship = read("skills/ship/SKILL.md")
    handoff = read("skills/ship/references/handoff-accounting.md")
    merge = read("skills/ship/references/merge-accounting.md")
    gate = read("agents/gate.md")
    spec = read("skills/spec/SKILL.md")
    start = read("skills/ship/references/start-resume.md")
    gate_ref = read("skills/ship/references/immutable-gate.md")
    contract = read("skills/tracker/CONTRACT.md")
    impl = read("skills/ship/references/implementation.md")
    img_ref = read("skills/shared/references/image-inspection.md")
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
    # The cache moved inside the `preflight` tool, so what these three assert moved with it: not that a
    # doc names a helper script, but that it says the one call caches itself. A doc that describes a
    # separate check-then-record pair is the specific regression here, and none of these strings survive it.
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
    if "migrate --settings" not in init_repo:
        fail("init-repo must offer the legacy env-block migration step (sy_config.py migrate)", errors)
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

    # The spec-gate checklist has exactly one copy and its reviewer dispatches nothing; both were
    # prose invariants until asserted here, and a restated checklist drifts invisibly.
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
    tools_value = _frontmatter_field(spec_gate, "tools").strip()
    if not tools_value:
        fail(
            "spec-gate must declare an explicit tools: list; an absent tools field inherits every tool "
            "including Agent/Skill, which is the violation this pin exists to block",
            errors,
        )
    elif re.search(r"\b(?:Agent|Skill)\b", tools_value):
        fail("spec-gate reviews a plan and dispatches nothing; it must carry no Agent/Skill tool", errors)
    if "docs requiring updates" not in spec or "visual-debug obligations" not in spec:
        fail("spec's /sy:ship section must require the docs-sync and visual-debug completeness fields", errors)
    # Section-scoped on purpose: §2 legitimately permits a research-phase body edit, so a whole-file
    # check passes on §2's prose alone after the guarantee is dropped from the post-approval procedure.
    # The guarantee is asserted in Step 2 (where the writes happen) rather than across all of §7,
    # where Step 1's consent sentence would satisfy it on its own.
    spec_s7 = spec.partition("## 7.")[2].partition("## 8.")[0]
    if "update-issue" in spec_s7:
        fail("spec §7 must not reach for update-issue; after approval it posts comments and sets status only", errors)
    if "never writes the Task body" not in spec_s7.partition("### Step 2")[2]:
        fail("spec §7's Step 2 procedure must state it never writes the Task body", errors)
    if "Step 2 — after approval" not in spec:
        fail("spec must keep the staged reveal: full plan posted only in Step 2, after approval", errors)

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

    # Read side and write side check different tools on purpose: a doc that names only `memory_add` is
    # not reading memory back, and one that names only `memory_list` is not writing the lesson.
    for name, text in (("plan", plan), ("spec", spec), ("ship start", start)):
        if "memory_list" not in text or "memory.md" not in text:
            fail(f"{name} must read durable cross-session memory back (memory_list, per memory.md)", errors)
    if "memory_add" not in handoff or "memory.md" not in handoff:
        fail("ship handoff retro must distill durable lessons into cross-session memory (memory_add, per memory.md)", errors)
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

    # Every site that could hardcode one of these behaviours must name the live config key instead,
    # mirroring the worktree.root pattern above: a literal number or behaviour re-pasted at the site
    # rather than resolved drifts from the config silently, and nothing else would catch it.
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
    if "ship.escalation.max_needs_decision" not in ship or "ship.escalation.max_needs_trace" not in ship:
        fail("ship must name ship.escalation.max_needs_decision/max_needs_trace rather than vague escalation thresholds", errors)
    if "spec.light_tier_max_files" not in spec:
        fail("spec must name spec.light_tier_max_files rather than an undefined 'small' threshold", errors)
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

    # Nine calls left with the scripts they tested. Everything they covered is now `sy_tools/` code,
    # where the convention is pytest under `sy_tools/tests/` mirroring the source path and
    # `test_layout.py` enforces the mirroring — so re-running those assertions here would be a second
    # runner for one body of tests. `scripts/sy_config.py` is the odd one out: it survives, thinned to
    # `migrate`, but its `self-test` subcommand went with the resolution it no longer exposes, and
    # `migrate` is covered by the CLI-driving tests in `sy_tools/tests/test_config.py`.
    run_self_test("scripts/ci_poll.sh", errors)
    run_self_test("docs/smoke_mcp.py", errors)

    if errors:
        print("Shipyard validation FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Shipyard validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
