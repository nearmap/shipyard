# Roadmap shaping

## Debate before the ladder

Pressure-test the objective's shape and its load-bearing premise with `sy:debate` before building the ladder — unconditionally, not only when the shape presents as a genuine fork; a shape that looks obvious is the case this catches. See `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/debate.md` for dispatch, and let the user steer the disagreement before it gets baked into the ladder below.

## Find seams

Locate low-coupling cut points and real dependency order. Use parallel `sy:sweep` briefs for large surfaces; escalate only genuinely unresolved boundaries to `sy:seam`.

Hunt hidden serialization: shared schemas/tables/config, generated artifacts, migrations, deployment order, or API/format compatibility that make apparently parallel work sequential.

## Build the ladder backwards

- Name the capabilities composing the North Star; let conceptual fan-out follow reality.
- Sequence the critical path and identify the parallel-safe set.
- Pick the near horizon using critical-path pressure and uncertainty reduction.
- Existing active work counts against the resolved `plan.max_active_tasks` cap (resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`).

## Create only near executable leaves

Create only PR-sized Tasks/Bugs ready for `/sy:spec`. Each needs goal, boundary, adjacent interfaces, blockers/dependencies, and enough project context to explain how it fits. Create direct under the Epic in `backlog`; record dependencies with the tracker's `add-dependency`.

If the near horizon would exceed `plan.max_active_tasks` active leaves, create only the critical/uncertainty-reducing subset and keep the rest conceptual in the Epic.

## Plan stacks on purpose

A stack is a chain of leaves where each one builds on the code of the one below it, and each ships as its own PR on top of the PR below.

- Plan a stack when a later leaf needs an earlier leaf's code before that code merges, and reviewing the two PRs apart is cheaper than reviewing one large PR. Leaves that share no code stay parallel; never chain them to make the map look tidy.
- Record each link with `add-dependency`: the upper leaf is blocked by the leaf directly below it, and by nothing else that is not done. `/sy:ship` stacks a Task on its blocker's open PR only when that blocker is its one not-done blocker (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Invariants). A leaf with two open blockers waits.
- Every layer counts against `plan.max_active_tasks`. When a chain is longer than the cap, activate the bottom layers and keep the rest conceptual.
- Layers ship bottom up: `/sy:ship` on a layer needs the PR of the layer below to exist, and its GATE starts only after that PR leaves draft. Each person who ships a layer needs `gh extension install github/gh-stack`, and the stack lands as one `gh stack merge`.

When shape decisions are settled, load `checkpoint-handoff.md`.
