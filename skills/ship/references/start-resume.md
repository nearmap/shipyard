# Start or resume

This phase runs as the `sy:ship-start` worker: it initializes or resumes ownership, delegates standards resolution and large Epic/plan reads, and returns the state brief per the worker contract.

1. Read Task body/comments and select the sole ACTIVE execution plan.
2. Read parent Epic only enough for sibling interfaces/blockers; use `sy:sweep` for a large tail.
3. Ship profile (the plan's explicit per-phase models, plus effort and process tier) is a parent precondition verified before dispatch; if the parent's own running session is below plan it stops and asks via `AskUserQuestion` (raise the profile / proceed at plan floor / other) per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`. That check concerns the parent's own session tier only; how each phase's model reaches its worker is the separate dispatch mechanism in `## Resolve start model` below. The profile floors worker models (may raise, never lower, so BUILD keeps its opus tier) and sets worker effort to match the work; it never lowers review effort (`sy:gate` stays max). Do not prompt the user from the worker.
4. Resolve standards in a delegate (subagent running `/sy:standards resolve <task scope>`, added to `agents_used`) that returns only the retained contract — authority, implementation contract, primitives, risk lenses; rule-file reads stay out of the ship context.
5. Read durable cross-session memory — `python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_memory.py" list` (or `search` on the tools/surfaces the task touches) per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`; a lesson that bears on the task enters the state brief as a known anchor.
6. Load `.scratch/<task>-ship-state.yaml` from main checkout if present.

Classify:

- no owned branch/PR/worktree record → fresh;
- owned branch, no PR → resume only when ownership matches;
- draft PR → resume build/gate cycle;
- ready PR → inspect coverage freshness;
- merged PR → set the task `done` if needed via the `tracker` skill and clean only recorded paths.

Suggest, as a single optional aside (not an `## Action needed` block, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`), that the user run `/rename ship <task> <slug>` once loaded.

## Resolve start model

Parent-owned, resolved once before START is dispatched (this worker never picks its own model), from the plan's ship profile and the actual process environment:

```text
START_MODEL=<the plan's stated START model, literally, or ${SY_FRONTIER_MODEL:-fable} when it states "frontier">
START_MODEL_FALLBACK=${SY_FRONTIER_FALLBACK:-opus}
```

Pass `START_MODEL` as the Agent invocation's **model override**, not merely as prompt text: with the override omitted, `sy:ship-start`'s own frontmatter model wins and START silently runs below the plan's tier — a failure only visible later through usage-transcript archaeology. Never resolve below `sonnet`, the `ship-start` floor; a lower stated model is clamped up to it. The parent also states the resolved `START_MODEL` in START's dispatch prompt alongside the model override, since the worker's own state brief carries no field for it. Record the resolved model as `start_model_requested`; the usage transcript later provides `start_model_observed`, so do not claim they match until observed.

If START cannot run at the requested model — a spend cap, a rate limit, or a `<synthetic>` refusal in place of a return — re-dispatch once at `START_MODEL_FALLBACK` clamped up to the `sonnet` floor, and set `start_model_observed` to the model that actually ran, per the same rule as gate. A plan written in the old single-word profile format, or one whose START tier is otherwise ambiguous, is never guessed upward: it resolves to the `sonnet` floor.

## Fresh run

1. fetch origin;
2. run the sibling/stacked-PR scan: list open PRs, local and remote branches, and existing worktrees that touch the same surface. Overlap with an open sibling or stacked PR is resolved before branching — coordinate with it, stack on it explicitly, or stop — and the scan result is recorded in state so later phases inherit it;
3. check plan-base freshness: diff the plan's `PLAN_BASE_SHA` against the fresh target/integration branch (`origin/main` where that is the target). Unrelated drift → continue. Drift touching plan anchors/dependencies → inspect those changes before building. Material contract or architecture drift → stop and return to `/sy:spec`;
4. branch from the fresh target/integration branch;
5. create the dedicated build worktree under the worktree root `${SY_WORKTREE_ROOT:-<repo>-worktrees}` (default: the sibling directory beside the repo; never inside it) and record its absolute path;
6. write local resume state:

```yaml
task: TASK-123
branch: task-123-example
worktree: /abs/path
plan_base_sha: <from ACTIVE plan>
ship_base_sha: <fresh origin/main>
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
accepted_deviations: []
phase_checkpoint: null
ship_session_id: <current session id if available>
ship_session_started_at: <timestamp>
sibling_scan: <step-2 scan result: branches, open PRs, worktrees>
agents_used: []
```

7. set the Task `in-progress` via the `tracker` skill, self-assign, and ensure the parent Epic is `in-progress`.

Each phase's `*_model_requested` is written when that phase is dispatched and its `*_model_observed` only once the usage transcript confirms what ran, so the `build_model_*` pair stays `null` until the parent dispatches BUILD and the `review_model_*` pair stays `null` until GATE.

The state file is local resume state, not shared truth. Never prune unrelated worktrees or paths. `phase_checkpoint` is the active worker's idempotent resume anchor (e.g. a slice manifest with per-slice status), passed to any continuation worker.

Return `done` with the state brief; the parent dispatches BUILD.
