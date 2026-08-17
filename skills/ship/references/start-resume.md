# Start or resume

This phase runs as the `sy:ship-start` worker: it initializes or resumes ownership, delegates standards resolution, and returns the state brief per the worker contract.

1. The parent supplies the plan file's absolute path and its pin (`plan_comment_id`, `plan_version`) in this worker's dispatch prompt, having already materialised it with `plan_file` before dispatching anything (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § State router). Read `PLAN_BASE_SHA` and the `pre-gate checkpoint` channel out of that file. This worker holds no tracker read and never selects a plan itself: the version selection and its refusals are the tool's (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Invariants).
2. Sibling interfaces and blockers off the parent Epic, when the parent's dispatch prompt states them, land here rather than via a fresh read: neither this worker nor `sy:sweep` holds a tracker read, so an instruction to go read the Epic here would be dead. Unlike `START_MODEL`, nothing obliges the parent to supply this: a dispatch that omits it leaves this worker with none, which is not a resume failure.
3. Ship profile (the plan's explicit per-phase models, plus effort and process tier) is a parent precondition verified before dispatch; if the parent's own running session is below plan it stops and asks via `AskUserQuestion` (raise the profile / proceed at plan floor / other) per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`. That check concerns the parent's own session tier only; how each phase's model reaches its worker is the separate dispatch mechanism in `## Resolve start model` below. The profile floors worker models (may raise, never lower, so BUILD keeps its opus tier) and sets worker effort to match the work; it never lowers review effort (`sy:gate` stays max). Do not prompt the user from the worker.
4. Resolve standards in a delegate (subagent running `/sy:standards resolve <task scope>`, added to `agents_used`) that returns only the retained contract — authority, implementation contract, primitives, risk lenses; rule-file reads stay out of the ship context.
5. Read durable cross-session memory — `memory_list` (or `memory_search` on the tools/surfaces the task touches) per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`; a lesson that bears on the task enters the state brief as a known anchor. A known anchor this phase's own direct observation already contradicts is never carried forward as if it still held: author it as a `MEMORY_REFUTE` candidate in the return block and record it to `memory_refutations` in state, for the parent to apply — this worker holds no memory write itself.
6. Load `ship-state.yaml` from the task's resolved scratch directory — `scratch_dir {"identifier": "$TASK_KEY"}`, whose reported `path` is that directory — if present. Draining any `memory_refutations` the loaded state still carries is the parent's own pre-dispatch step (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § State router), never this worker's, which holds no memory write.

Classify:

- no owned branch/PR/worktree record → fresh;
- owned branch, no PR → resume only when ownership matches;
- draft PR → resume build/gate cycle;
- ready PR → inspect coverage freshness;
- merged PR → set the task `done` if needed via the `tracker` skill and clean only recorded paths.

Suggest, as a single optional aside (not an `## Action needed` block, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`), that the user run `/rename ship <task> <slug>` once loaded.

## Resolve start model

Parent-owned, resolved once before START is dispatched (this worker never picks its own model), from the plan's ship profile and the actual process environment:

```
START_MODEL          = <the plan's stated START model, literally, or the value `get_config {"key": "models.tiers.frontier"}` reports when it states "frontier">
START_MODEL_FALLBACK = the value `get_config {"key": "models.tiers.frontier_fallback"}` reports
```

Pass `START_MODEL` as the Agent invocation's **model override** and record/reconcile it as `start_model_requested`/`start_model_observed` per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md` — clamped up to `ship-start`'s `cheap` floor in `config/floors.json` (`sonnet` by default) rather than resolved via a generic `agent_model` call, since this phase's model comes from the plan's stated profile instead. The parent also states the resolved `START_MODEL` in START's dispatch prompt alongside the model override, since the worker's own state brief carries no field for it.

If START cannot run at the requested model, re-dispatch once at `START_MODEL_FALLBACK` clamped up to the `sonnet` floor and set `start_model_observed` to the model that actually ran, per model-dispatch.md's "Unavailability falls back once". A plan written in the old single-word profile format, or one whose START tier is otherwise ambiguous, is never guessed upward: it resolves to the `sonnet` floor.

## Fresh run

1. fetch origin;
2. run the sibling/stacked-PR scan: list open PRs, local and remote branches, and existing worktrees that touch the same surface. Overlap with an open sibling or stacked PR is resolved before branching — coordinate with it, stack on it explicitly, or stop — and the scan result is recorded in state so later phases inherit it;
3. check plan-base freshness: diff the plan's `PLAN_BASE_SHA` against the fresh target/integration branch (`origin/main` where that is the target). Unrelated drift → continue. Drift touching plan anchors/dependencies → inspect those changes before building. Material contract or architecture drift → stop and return to `/sy:spec`;
4. branch from the fresh target/integration branch;
5. create the dedicated build worktree under the resolved worktree root (`get_config {"key": "worktree.root"}`; defaults to the sibling directory beside the repo, never inside it) and record its absolute path;
6. write local resume state, stamping the resolved-config fingerprint alongside the pinned SHAs — pinned and compared rather than re-read, because a setting that changes mid-run silently changes what later phases do:

```yaml
task: TASK-123
branch: task-123-example
worktree: /abs/path
plan_base_sha: <from the highest plan version>
plan_path: <absolute path the parent's `plan_file` call reported>
plan_comment_id: <the comment id that call reported>
plan_version: <the version that call reported>
ship_base_sha: <fresh origin/main>
config_fingerprint: <the `fingerprint` that `fingerprint_config {}` reports>
process_tier: full
pr: null
head_sha: null
ci_green_sha: null
review_base_sha: null
reviewed_sha: null
target_sha: null
review_model_requested: null
review_model_observed: null
review_effort: max
start_model_requested: <resolved START_MODEL>
start_model_observed: null
build_model_requested: null
build_model_observed: null
pregate_checkpoint_channel: <the plan's declared channel — draft-pr | running-preview — or null when the plan declares none>
pregate_checkpoint_cleared_sha: null
pregate_checkpoint_changes_requested: 0
pregate_checkpoint_gate_dispatched: false
pregate_checkpoint_request_text: null
accepted_deviations: []
memory_refutations: []
phase_checkpoint: null
phase_active: null
gate_rounds_total: 0
gate_rounds_budget_base: 0
ship_session_id: <current session id if available>
ship_session_started_at: <timestamp>
sibling_scan: <step-2 scan result: branches, open PRs, worktrees>
agents_used: []
```

7. set the Task `in-progress` via the `tracker` skill, self-assign, and ensure the parent Epic is `in-progress`. The tracker skill's own `validate_config`/`preflight` preamble is already discharged for this run by the parent's pre-dispatch tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Invariants), so call `set-status` and `assign` directly rather than re-running that preamble — those two verbs are the whole of this worker's granted tracker access.

Each phase's `*_model_requested` is written when that phase is dispatched and its `*_model_observed` only once the usage transcript confirms what ran, so the `build_model_*` pair stays `null` until the parent dispatches BUILD and the `review_model_*` pair stays `null` until GATE. `pregate_checkpoint_channel` is stamped once here too, from the plan's `pre-gate checkpoint` field in the normalized form the block above shows, alongside `pregate_checkpoint_gate_dispatched` and `pregate_checkpoint_request_text` at their initial values; `${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Pre-gate checkpoint owns how all five are later set and re-checked.

The state file is local resume state, not shared truth. Never prune unrelated worktrees or paths. `phase_checkpoint` is the active worker's idempotent resume anchor (e.g. a slice manifest with per-slice status), passed to any continuation worker. `phase_active` is the parent's own, never a worker's, and `gate_rounds_total`/`gate_rounds_budget_base` are GATE's live fix-cycle round accounting; `${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Worker contract and `references/immutable-gate.md` § Fix cycle own both. All three are stamped once here so a resume reads them rather than inferring them, and an older state file carrying none of them reads as `null`/`0`/`0` — nothing in flight, no rounds yet spent, no budget yet raised. `plan_path`, `plan_comment_id` and `plan_version` are stamped here for the same reason, from what the parent's own `plan_file` call reported, and read as `null` on a state file written before they existed. A `null` pin is not a resume failure and needs no migration: there is nothing recorded for the fresh pin to be compared against, so the comparison is skipped rather than failed (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § State router).

Return `done` with the state brief; the parent dispatches BUILD.
