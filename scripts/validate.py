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
    "img-inspector", "explain-author", "debate", "debater",
}
EXPECTED_SKILLS = {
    "plan", "spec", "ship", "spike", "pr", "ci", "standards", "tracker", "explain", "init-repo", "help",
}
FORBIDDEN_OLD_NAMES = {"explore-sonnet", "seam-scout", "path-tracer", "slice-builder", "bug-hunter", "rev-gate"}

CANONICAL_VERBS = {
    "create-issue", "create-child", "get-issue", "update-issue", "find-issues", "set-status",
    "assign", "link-parent", "add-dependency", "add-label", "post-comment", "post-log",
    "attach-artifact", "link-pr",
}
CANONICAL_STATUSES = {"backlog", "ready", "in-progress", "in-review", "done"}
COLUMN_ENV = {
    "SY_BACKLOG_COLNAME", "SY_READY_COLNAME", "SY_IN_PROGRESS_COLNAME",
    "SY_IN_REVIEW_COLNAME", "SY_DONE_COLNAME",
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
    "scripts/session_usage.py",
    "scripts/review_guard.py",
    "scripts/eval_events.py",
    "scripts/ci_poll.sh",
    "scripts/sy_memory.py",
    "scripts/sy_preflight.py",
    "scripts/scrub_known_secrets.py",
    "scripts/secret_guard.py",
    "skills/tracker/SKILL.md",
    "skills/tracker/CONTRACT.md",
    "skills/tracker/jira/ADAPTER.md",
    "skills/tracker/jira/md_to_adf.py",
    "skills/tracker/jira/jira_rest.py",
    "skills/tracker/jira/adf-requirements.lock",
    "skills/tracker/jira/references/accounting.md",
    "skills/tracker/jira/references/attachments.md",
    "skills/tracker/jira/references/adf.md",
    "skills/tracker/jira/references/acli-cookbook.md",
    "skills/tracker/jira/references/migration.md",
    "skills/tracker/github/ADAPTER.md",
    "skills/tracker/github/gh_project.py",
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


def _component_md(seam_only: bool) -> list[Path]:
    """Core component markdown: agents/ + skills/, excluding the tracker legal zone when seam_only."""
    paths: list[Path] = []
    for base in ("agents", "skills"):
        for p in (ROOT / base).rglob("*.md"):
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
        if "~/.claude" in p.read_text(encoding="utf-8"):
            fail(f"{p.relative_to(ROOT)}: uses ~/.claude; bundle files must use ${{CLAUDE_PLUGIN_ROOT}}", errors)


def check_seam(errors: list[str]) -> None:
    scan = _component_md(seam_only=True) + [
        ROOT / "scripts/session_usage.py", ROOT / "scripts/review_guard.py", ROOT / "scripts/ci_poll.sh",
        ROOT / "scripts/sy_memory.py", ROOT / "scripts/sy_preflight.py", ROOT / "scripts/eval_events.py",
    ]
    for p in scan:
        text = p.read_text(encoding="utf-8")
        for pattern in TRACKER_TOKENS:
            m = pattern.search(text)
            if m:
                line = text[: m.start()].count("\n") + 1
                fail(f"SEAM: {p.relative_to(ROOT)}:{line}: tracker token {m.group(0)!r} outside skills/tracker/", errors)
                break


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
    for var in sorted(COLUMN_ENV):
        for name, text in (("CONTRACT", contract), ("jira", jira), ("github", github)):
            if var not in text:
                fail(f"contract completeness: column env var {var} missing from {name}", errors)


def check_hooks(errors: list[str]) -> None:
    text = (ROOT / "hooks/hooks.json").read_text(encoding="utf-8")
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        fail(f"hooks/hooks.json invalid JSON: {exc}", errors)
        return
    if "review_guard.py" not in text:
        fail("hooks/hooks.json must wire scripts/review_guard.py (PreToolUse)", errors)
    if "secret_guard.py" not in text:
        fail("hooks/hooks.json must wire scripts/secret_guard.py (PreToolUse)", errors)
    if "session_usage.py" not in text:
        fail("hooks/hooks.json must wire scripts/session_usage.py (Stop/SubagentStop)", errors)
    if "eval_events.py" not in text:
        fail("hooks/hooks.json must wire scripts/eval_events.py (PreToolUse/SubagentStop/Stop)", errors)


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
    roadmap_shaping = read("skills/plan/references/roadmap-shaping.md")
    tracker_skill = read("skills/tracker/SKILL.md")
    jira_adapter = read("skills/tracker/jira/ADAPTER.md")
    github_adapter = read("skills/tracker/github/ADAPTER.md")
    init_repo = read("skills/init-repo/SKILL.md")

    if "--match-head-commit" not in merge:
        fail("merge path missing atomic head guard (--match-head-commit)", errors)
    if "decomposed/superseded closure is not delivery" not in ship.lower():
        fail("ship missing decomposed-closure-is-not-delivery rule", errors)
    if "main_plus_subagents" not in handoff or "--require-agent" not in handoff:
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
    if "SY_IMAGE_MODEL" not in img_ref or "img-inspector" not in img_ref:
        fail("image-inspection reference must resolve SY_IMAGE_MODEL and route to sy:img-inspector", errors)
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

    if "## Action needed" not in preflight_ref or "docs/settings.md" not in preflight_ref:
        fail("preflight reference must define the Action-needed failure shape and link docs/settings.md", errors)
    if "sy_preflight.py" not in preflight_ref or "cache" not in preflight_ref.lower():
        fail("preflight reference must describe the cached liveness check (sy_preflight.py)", errors)
    if "sy_preflight.py" not in tracker_skill:
        fail("tracker skill must wire the cached liveness check (sy_preflight.py) into its fail-fast section", errors)
    for name, text in (("jira", jira_adapter), ("github", github_adapter)):
        if "sy_preflight.py" not in text:
            fail(f"{name} adapter must declare its preflight hook, cached through sy_preflight.py", errors)
    for name, text in (("plan", plan), ("spec", spec), ("ship", ship), ("spike", spike)):
        if "preflight.md" not in text:
            fail(f"{name} must run the tracker preflight (preflight.md) before other work", errors)
    if "settings.local.json" not in init_repo or "settings.json" not in init_repo:
        fail("init-repo must split shared vs secret config between settings.json and settings.local.json", errors)
    if "preflight.md" not in init_repo or "sy_preflight.py" not in init_repo:
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
    if "undispositioned actionable finding" not in gate_ref:
        fail("immutable-gate fix cycle must state the stopping rule (no undispositioned actionable finding)", errors)
    if "drift re-check" not in gate_ref.lower():
        fail("immutable-gate loop must include the periodic target-branch drift re-check", errors)
    if "ci_poll.sh" not in gate_ref:
        fail("immutable-gate must route CI waits through the shared scripts/ci_poll.sh poller", errors)
    if "gate_false_pass" not in handoff or "human backstop" not in gate:
        fail("gate human-backstop note and ship metrics gate_false_pass field are required (shadow-run backstop)", errors)

    for name, text in (("plan", plan), ("spec", spec), ("ship start", start)):
        if "sy_memory.py" not in text or "memory.md" not in text:
            fail(f"{name} must read durable cross-session memory back (sy_memory.py, per memory.md)", errors)
    if "sy_memory.py" not in handoff or "memory.md" not in handoff:
        fail("ship handoff retro must distill durable lessons into cross-session memory (sy_memory.py, per memory.md)", errors)
    if "close with evidence" not in spec.lower() or "shelve" not in spec.lower():
        fail("spec must bless the shelve terminal state (close with evidence, no plan)", errors)
    for name, text in (
        ("ship", ship), ("start-resume", start), ("implementation", impl),
        ("immutable-gate", gate_ref), ("merge-accounting", merge),
    ):
        if "SY_WORKTREE_ROOT" not in text:
            fail(f"{name} must resolve the worktree root via SY_WORKTREE_ROOT (sibling-directory default)", errors)

    gate_fm = gate.split("---", 2)[1]
    if "Skill" not in gate_fm:
        fail("gate agent must allow the Skill tool", errors)
    if re.search(r"^skills:\s*$", gate_fm, re.M):
        fail("gate must not preload standards; conformance review is invoked lazily", errors)
    if "SY_FRONTIER_MODEL" not in gate_ref:
        fail("immutable-gate must resolve the reviewer via SY_FRONTIER_MODEL", errors)
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
        text = p.read_text(encoding="utf-8")
        for old in FORBIDDEN_OLD_NAMES:
            if old in text:
                fail(f"{p.relative_to(ROOT)}: stale agent name {old}", errors)

    for p in agent_paths:
        text = p.read_text(encoding="utf-8")
        if "Return contract" not in text:
            fail(f"{p.relative_to(ROOT)}: missing compact Return contract", errors)
        if "SPLIT_REQUIRED" not in text:
            fail(f"{p.relative_to(ROOT)}: missing SPLIT_REQUIRED overflow contract", errors)

    check_structure(errors)
    check_no_home_paths(errors)
    check_seam(errors)
    check_contract_completeness(errors)
    check_hooks(errors)
    check_invariants(errors)

    run_self_test("scripts/review_guard.py", errors)
    run_self_test("scripts/secret_guard.py", errors)
    run_self_test("scripts/session_usage.py", errors)
    run_self_test("scripts/eval_events.py", errors)
    run_self_test("scripts/sy_memory.py", errors)
    run_self_test("scripts/sy_preflight.py", errors)
    run_self_test("scripts/scrub_known_secrets.py", errors)
    run_self_test("skills/tracker/github/gh_project.py", errors)
    run_self_test("skills/tracker/jira/jira_rest.py", errors)
    run_self_test("scripts/ci_poll.sh", errors)

    if errors:
        print("Shipyard validation FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Shipyard validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
