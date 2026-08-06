# Implement and integrate

Follow ordered plan decisions. Resolve small details consistent with plan intent yourself and record them in `accepted_deviations`. A decision you cannot ground in plan/standards/code but that does not invalidate the plan returns `needs-decision` with an updated checkpoint; a new load-bearing fork or an invalidated contract returns `bail-to-spec`. An open empirical question is different from both: when a spot-check — or a delegate's own findings — comes back inconclusive and continuing would move past what's already been pointed at (a live external system, a scratch probe script, or a second follow-up command still chasing the same question), return `needs-trace` naming the open question and its seed anchors; the parent, never this worker, dispatches `sy:trace` and resumes you from the checkpoint with the findings. Never prompt the user. Use `sy:sweep` for broad reconnaissance.

Before executing any plan step, verify its load-bearing plan facts against the current base: re-locate every cited file anchor by content (grep the surrounding phrase; never trust the plan's line numbers) and confirm each named convention still holds. A fact found false is never followed: a mismatch that leaves the plan's intent intact returns `needs-decision` with the mismatch and its bearing spans; one that invalidates the plan's contract returns `bail-to-spec`.

An adjacent issue you surface mid-build that sits outside the plan's declared file set follows the same test: fold a small, low-risk fix into this branch as a recorded scope extension in `accepted_deviations` rather than filing a follow-up that loses the context you have now; defer only when it justifies its own ticket (see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/scope-discipline.md`).

A seeded memory anchor this phase's own direct observation contradicts is handled the same way rather than silently carried forward or left to HANDOFF: author it as a `MEMORY_REFUTE` candidate in the return block and record it to `memory_refutations` in state, for the parent to apply the moment this phase returns (see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`).

## Resolve build model

Parent-owned, resolved once before BUILD is dispatched (this worker never picks its own model), from the plan's ship profile and the resolver, live (tool names resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`):

```
BUILD_MODEL          = <the plan's stated BUILD model, literally, or the value `get_config {"key": "models.tiers.frontier"}` reports when it states "frontier">
BUILD_MODEL_FALLBACK = the value `get_config {"key": "models.tiers.frontier_fallback"}` reports
```

Pass `BUILD_MODEL` as the Agent invocation's **model override** and record/reconcile it as `build_model_requested`/`build_model_observed` per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md` — clamped up to `ship-build`'s `standard` floor in `config/floors.json` (`opus` by default) rather than resolved via a generic `agent_model` call, since this phase's model comes from the plan's stated profile instead. The parent also states the resolved `BUILD_MODEL` in BUILD's dispatch prompt alongside the model override, since the worker's own state brief carries no field for it.

If BUILD cannot run at the requested model, re-dispatch once at `BUILD_MODEL_FALLBACK` clamped up to the `opus` floor and set `build_model_observed` to the model that actually ran, per model-dispatch.md's "Unavailability falls back once". A plan written in the old single-word profile format, or one whose BUILD tier is otherwise ambiguous, is never guessed upward: it resolves to the `opus` floor.

## Delegated slice protocol

Delegate only bounded, low-design-ambiguity slices:

1. create and record the dedicated slice branch/worktree from the integration base, under the resolved worktree root (`get_config {"key": "worktree.root"}`; defaults to the sibling directory beside the repo, never inside it);
2. prompt `sy:slice` with plan step, anchors, acceptance criteria, sibling interfaces, and relevant standards contract;
3. add `sy:slice` to local `agents_used` accounting state;
4. receive committed SHA and compact evidence brief;
5. inspect diff and decisive spans;
6. cherry-pick into build branch;
7. run integration-relevant tests;
8. remove only the exact recorded slice worktree/branch after successful integration.

Track build progress as a slice manifest in `phase_checkpoint` (per slice: `pending|committed|integrated`, with SHAs), updated after each integration, so a `needs-decision` or `needs-trace` return resumes at the next pending slice and re-does no integrated work. A slice's `source` is `plan` by default — its content is the cited plan step — or `pregate_revision`, whose content is the slice's own `spec` field instead (see below); nothing else about a slice's shape changes.

At the very start of any continuation, before resuming at the next pending slice, check the state brief's `pregate_checkpoint_request_text`. When it is set, the `/sy:ship` parent is relaying a pre-gate-checkpoint "request changes" reply that has not yet become a slice — by construction: the parent only ever persists that field and a `phase_checkpoint` without the matching slice together, and only ever persists it cleared alongside a `phase_checkpoint` that already has the slice, so a set value always means "not folded in yet," with no dedup needed. Fold it in first: append one new entry to your own manifest with `source: pregate_revision`, `status: pending`, and `spec` set to that text verbatim — everything else about the entry (its `id`, and its SHAs as it commits/integrates) follows however you already key a slice. From there it is an ordinary slice: run it through the delegated protocol above using `spec` in place of a plan-step citation, and your own judgment (recorded as an `accepted_deviation`, as for any small plan-consistent detail you already resolve yourself) for anchors and acceptance criteria, since a checkpoint reply names neither. `phase_checkpoint` stays entirely worker-authored either way — the parent never writes a slice itself, only the `pregate_checkpoint_request_text` field it hands off, the same way it only ever originates its own `pregate_checkpoint_*` fields and never yours.

After integration, run acceptance tests and standards-required formatter/linter/type checks; route verbose runs (full suite, linters, type checks) through logs in the task's resolved scratch directory — `scratch_dir($TASK_KEY)` throughout this reference means the `path` the `scratch_dir` tool reports for `{"identifier": "$TASK_KEY"}` — and read back only failures and summary lines, keeping raw output out of the ship context. Discharge every verification obligation with its named evidence; an undischargeable obligation returns to `/sy:spec`. Where acceptance criteria describe observable behaviour, execute the behaviour (a runner kept in that same directory is fine) and capture the output as acceptance evidence — tests alone discharge only test-shaped criteria.

When a plan step produces, regenerates, or selects among images (figures, screenshots, plots, marketing visuals), inspect them by fanning out to `sy:img-inspector` per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`: resolve `IMAGE_MODEL` as the model `agent_model {"name": "img-inspector"}` reports, dispatch the inspector with the path(s) and the inspection task, add it to `agents_used`, and record the returned text verdicts as the figure's acceptance evidence. Never `Read` a raw image into the build context; the text verdicts drive accept / regenerate / reselect.

Every doc the plan names in `docs requiring updates` is a verification obligation on the same footing: before the draft PR, either update it in this branch or read it and confirm on inspection it is already accurate, and record for each named doc which of the two applied plus that branch's evidence: the commit/diff that updated it, or — for "already accurate" — the `path:line-line` span the confirming read covered. "I checked it" with no span named is not evidence. The field is not a hint for incidental doc-sync — a named doc left neither updated nor inspected is an undischarged obligation, and an undischargeable one returns to `/sy:spec` like any other.

Doc, marketing, and other prose deliverables get a deterministic content-QA pass before the draft PR: grep every shipped prose artifact for leaked LLM wrapper tokens — the literal strings `</content>` and `</invoke>`, and internal tool/agent identifiers — and treat any hit as a build failure to fix, never a nit. When the plan declares a content deliverable this is a standing verification obligation: record the clean grep (command plus the `scratch_dir($TASK_KEY)` path its output landed in) as its named evidence.

Every load-bearing claim the brief will assert — diff scope, invariants preserved, "nothing else affected", lockfile/dependency effects, verification outcomes — carries a checkable pointer (the command run and where its output lives), never a bare assertion; a claim you cannot back is not `done`. Verify a claim about a generated or dependency artifact (lockfile hash, `depends`/`run_exports`, package moves) against the artifact itself, not against intent.

Then commit/push and open a draft PR through `/sy:pr draft`; Task remains `in-progress`. Return `done` with the updated state brief; the parent honours any plan-declared pre-gate checkpoint (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Pre-gate checkpoint) before dispatching GATE — this worker's own contract and return value are unchanged either way.
