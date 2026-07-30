# Implement and integrate

Follow ordered plan decisions. Resolve small details consistent with plan intent yourself and record them in `accepted_deviations`. A decision you cannot ground in plan/standards/code but that does not invalidate the plan returns `needs-decision` with an updated checkpoint; a new load-bearing fork or an invalidated contract returns `bail-to-spec`. An open empirical question is different from both: when a spot-check — or a delegate's own findings — comes back inconclusive and continuing would move past what's already been pointed at (a live external system, a probe script under `.scratch/`, or a second follow-up command still chasing the same question), return `needs-trace` naming the open question and its seed anchors; the parent, never this worker, dispatches `sy:trace` and resumes you from the checkpoint with the findings. Never prompt the user. Use `sy:sweep` for broad reconnaissance.

Before executing any plan step, verify its load-bearing plan facts against the current base: re-locate every cited file anchor by content (grep the surrounding phrase; never trust the plan's line numbers) and confirm each named convention still holds. A fact found false is never followed: a mismatch that leaves the plan's intent intact returns `needs-decision` with the mismatch and its bearing spans; one that invalidates the plan's contract returns `bail-to-spec`.

An adjacent issue you surface mid-build that sits outside the plan's declared file set follows the same test: fold a small, low-risk fix into this branch as a recorded scope extension in `accepted_deviations` rather than filing a follow-up that loses the context you have now; defer only when it justifies its own ticket (see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/scope-discipline.md`).

## Resolve build model

Parent-owned, resolved once before BUILD is dispatched (this worker never picks its own model), from the plan's ship profile and the actual process environment:

```text
BUILD_MODEL=<the plan's stated BUILD model, literally, or ${SY_FRONTIER_MODEL:-fable} when it states "frontier">
BUILD_MODEL_FALLBACK=${SY_FRONTIER_FALLBACK:-opus}
```

Pass `BUILD_MODEL` as the Agent invocation's **model override**, not merely as prompt text: with the override omitted, `sy:ship-build`'s own frontmatter model wins rather than the plan's stated tier. Never resolve below `opus`, the `ship-build` floor; a lower stated model is clamped up to it. The parent also states the resolved `BUILD_MODEL` in BUILD's dispatch prompt alongside the model override, since the worker's own state brief carries no field for it. Record the resolved model as `build_model_requested`; the usage transcript later provides `build_model_observed`, so do not claim they match until observed.

If BUILD cannot run at the requested model — a spend cap, a rate limit, or a `<synthetic>` refusal in place of a return — re-dispatch once at `BUILD_MODEL_FALLBACK` clamped up to the `opus` floor, and set `build_model_observed` to the model that actually ran, per the same rule as gate. A plan written in the old single-word profile format, or one whose BUILD tier is otherwise ambiguous, is never guessed upward: it resolves to the `opus` floor.

## Delegated slice protocol

Delegate only bounded, low-design-ambiguity slices:

1. create and record the dedicated slice branch/worktree from the integration base, under the worktree root `${SY_WORKTREE_ROOT:-<repo>-worktrees}` (default: the sibling directory beside the repo; never inside it);
2. prompt `sy:slice` with plan step, anchors, acceptance criteria, sibling interfaces, and relevant standards contract;
3. add `sy:slice` to local `agents_used` accounting state;
4. receive committed SHA and compact evidence brief;
5. inspect diff and decisive spans;
6. cherry-pick into build branch;
7. run integration-relevant tests;
8. remove only the exact recorded slice worktree/branch after successful integration.

Track build progress as a slice manifest in `phase_checkpoint` (per slice: `pending|committed|integrated`, with SHAs), updated after each integration, so a `needs-decision` or `needs-trace` return resumes at the next pending slice and re-does no integrated work.

After integration, run acceptance tests and standards-required formatter/linter/type checks; route verbose runs (full suite, linters, type checks) through `.scratch/` logs and read back only failures and summary lines, keeping raw output out of the ship context. Discharge every verification obligation with its named evidence; an undischargeable obligation returns to `/sy:spec`. Where acceptance criteria describe observable behaviour, execute the behaviour (a `.scratch/` runner is fine) and capture the output as acceptance evidence — tests alone discharge only test-shaped criteria.

When a plan step produces, regenerates, or selects among images (figures, screenshots, plots, marketing visuals), inspect them by fanning out to `sy:img-inspector` per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`: resolve `IMAGE_MODEL=${SY_IMAGE_MODEL:-sonnet}`, dispatch the inspector with the path(s) and the inspection task, add it to `agents_used`, and record the returned text verdicts as the figure's acceptance evidence. Never `Read` a raw image into the build context; the text verdicts drive accept / regenerate / reselect.

Doc, marketing, and other prose deliverables get a deterministic content-QA pass before the draft PR: grep every shipped prose artifact for leaked LLM wrapper tokens — the literal strings `</content>` and `</invoke>`, and internal tool/agent identifiers — and treat any hit as a build failure to fix, never a nit. When the plan declares a content deliverable this is a standing verification obligation: record the clean grep (command plus `.scratch/` output path) as its named evidence.

Every load-bearing claim the brief will assert — diff scope, invariants preserved, "nothing else affected", lockfile/dependency effects, verification outcomes — carries a checkable pointer (the command run and where its output lives), never a bare assertion; a claim you cannot back is not `done`. Verify a claim about a generated or dependency artifact (lockfile hash, `depends`/`run_exports`, package moves) against the artifact itself, not against intent.

Then commit/push and open a draft PR through `/sy:pr draft`; Task remains `in-progress`. Return `done` with the updated state brief; the parent dispatches GATE.
