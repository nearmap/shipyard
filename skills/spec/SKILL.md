---
name: spec
description: >-
  Turn a fuzzy goal or existing Task into one self-contained, execution-ready
  Task plan. Scope the surface, resolve standards, research load-bearing behaviour,
  remove ambiguity, and maintain exactly one ACTIVE versioned execution plan.
argument-hint: "[a goal to scope, or an existing task key (<task>) to deepen]"
disable-model-invocation: true
effort: max
---

Turn this goal or work item into a **Task** (or **Bug** for a defect fix) containing everything a fresh `/sy:ship` session needs to land one coherent PR. Code work is read-only; tracker writes use the `tracker` skill (`/sy:tracker`). End at the approved active plan — or, when research invalidates the premise, at a shelve-with-evidence closure (§6); do not implement.

Plan against fresh `origin/main` unless the user names another immutable base.

Before anything else — before the surface scan below spends any research — run the tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`). A failure stops here with its single `## Action needed` block, not partway through the plan.

$ARGUMENTS

## Scope before routing

- small cohesive module/doc/diff ⇒ read directly;
- large repetitive surface ⇒ `sy:sweep` breadth brief;
- one load-bearing end-to-end behaviour ⇒ `sy:trace`, one path per agent, at most 3 depth agents in flight.

Agent output is a lead. Verify decisive spans and own the plan. Seed every agent prompt with known anchors — paths, symbols, entry points, keys — and name ground already covered; agents must not rediscover what the caller knows. Resolve standards early (in a delegate, per step 3) so the plan reflects authoritative repository policy and risk lenses.

Ask one question at a time, via `AskUserQuestion`, only when research cannot settle a decision that changes scope, design, or acceptance — see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`.

## 1. Surface scan and interview

- Fetch and inspect the intended base.
- Run the premise + prior-work check before deep archaeology: confirm the goal's premise still holds on the intended base, and search for existing, shipped, duplicate, or sibling work — tracker `find-issues` over summary/label plus a code/PR search. A premise already delivered, invalidated, or owned by an open sibling stops here with that evidence (correct or close the item via the `tracker` skill) rather than producing a plan for work that should not ship.
- Map entry points directly or through `sy:sweep` according to size.
- Establish goal, definition of done, boundaries, constraints, and priorities.
- Suggest, as a single optional aside (not a gate), that the user run `/rename spec: <goal-slug>` or `/rename spec <task> <slug>` once nameable.

## 2. Create or load the Task

### New goal

Draft Summary, Context/constraints, and Out of scope. Use Bug for a defect fix, Task otherwise. Every Task/Bug must be parented to an Epic. Confirm parent and draft before creation via the `tracker` skill.

### Existing Task

Read its body/comments directly and preserve settled decisions. Delegate only large parent-Epic or PR tails to `sy:sweep`. Edit the body only when research changes framing. Ensure the parent Epic is `in-progress` when active work begins; the Task stays in `backlog` until its plan is approved (then `ready`, per step 7).

## 3. Resolve standards and deep research

- Resolve standards in a delegate (subagent running `/sy:standards resolve <scope>`) that returns only the compact contract — authority, task-relevant constraints, primitives, risk lenses; the raw rule and doc reads stay out of the spec context, where standards loaded early would be re-paid on every later turn.
- Deep research starts only after the §1 premise + prior-work check has survived; evidence against the premise found later still stops the spec (see §6, shelving with evidence) rather than merely reshaping it.
- Read durable cross-session memory early — `python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_memory.py" list` (or `search` on the tools/surfaces the task touches) per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`; a lesson that bears on the task enters the plan as a known anchor.
- Trace every load-bearing claim to code, current primary docs, or real data.
- Use `sy:sweep` for breadth and `sy:trace` for one end-to-end path; verify decisive spans directly.
- Verifying a decisive span means confirming one already-cited pointer with a read or a single targeted command; when that check — or a delegate's own findings — comes back inconclusive and continuing would move past what's already been pointed at (a live external system, a probe script under `.scratch/`, or a second follow-up command still chasing the same question), stop and dispatch a fresh, foreground `sy:trace` for it instead of continuing turn-by-turn; the dispatch draws on the same "at most 3 depth agents in flight" budget set above, not a separate one.
- Pull representative data when shape/frequency matters.
- Actively look for breaking cases and evidence against the preferred approach.
- Before the plan reaches sign-off (§7), pressure-test its core design decision with `sy:debate` — unconditionally, not only when this search happened to surface a two-sided fork: `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/debate.md`. When research does surface a genuine fork, debate it here, as soon as the choice is stated in one sentence, rather than carrying it unresolved into the plan.

Record standards compactly, for example:

```text
Standards authority
- /repo-standards skill

Task-specific constraints / risk lenses
- public response schema remains backward compatible
- migration needs rollback evidence
```

Convert every activated risk lens into a **verification obligation** — a claim plus named evidence `/sy:ship` must produce and `sy:gate` will verify:

```text
Verification obligations
- lens: concurrency; claim: duplicate delivery is idempotent;
  evidence: deterministic duplicate-delivery test, concurrent-update test
- lens: migration; claim: old and new versions coexist safely;
  evidence: expand/contract sequence, compatibility test
```

An obligation with no realistic evidence is a plan risk to surface, not a silent drop.

When the task generates or reviews images (figures, screenshots, plots, marketing visuals), add the standing image-inspection invariant to the plan's design invariants and a verification obligation whose named evidence is a `sy:img-inspector` text verdict: visual inspection is delegated to a short-lived inspector and never `Read` into a long-running context. See `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`.

## 4. Resolve ambiguity as it surfaces

Ask immediately, via `AskUserQuestion`, when research reaches a real owner decision that changes the plan. Record answers durably so `/sy:ship` does not re-ask.

## 5. Too big for one PR? Return to `/sy:plan`

Do not split an oversized Task ad hoc.

For an existing `/sy:plan` leaf, post a `# SEAMS` comment describing pieces, interfaces, and dependencies, then stop with `/sy:plan <epic>`. `/sy:plan` performs the tracker's canonical decomposition (see the `tracker` skill).

For a standalone objective, confirm via `AskUserQuestion` before promoting it to an Epic, then post the seams report and stop with `/sy:plan <epic>`.

## 6. Premise gone? Shelve: close with evidence, no plan

Not every spec ends in a plan. When research shows the premise is already delivered, invalidated, or superseded — whether at the §1 prior-work check or from evidence surfacing later — the blessed terminal state is a shelve: the Task closes with evidence instead of acquiring a plan for work that should not ship. This is distinct from §5, where a sound premise is merely too big for one PR.

1. Present the evidence as a status update, then close the turn with a single `AskUserQuestion` (shelve as described / keep researching / other), naming the mutations the go-ahead covers: post the evidence comment and set the Task's terminal status.
2. Post a durable evidence comment on the Task: what was found, the decisive pointers (commits, PRs, work items, spans), and why no plan should exist.
3. Set an **existing** status via the `tracker` skill — `done` when the premise was already delivered or the item should close, `backlog` when it is merely premature — never a new status; the evidence comment is what distinguishes this closure from delivery (decomposed/superseded/invalidated closure is not delivery).
4. Capture the session per §8 as on every run.

## 7. Capture exactly one active versioned plan

Present the full plan for sign-off — only after the mandatory `sy:debate` pass from §3 has run — structured in two clearly labeled parts, so a human reviewer and a fresh `/sy:ship` session each get only what they need without wading through the other's:

**For your sign-off** (rationale and judgment calls):

- approach and why;
- strongest rejected alternative and why — the adversary's strongest objection from the §3 debate plus the user's steer, not a restatement invented after the fact;
- risks/edge cases;
- unverified assumptions;
- out of scope — what this plan deliberately excludes; note it is a default contract, not a wall: small, adjacent, low-risk issues surfaced during ship may be folded in as recorded scope extensions rather than always spawning siblings (see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/scope-discipline.md`).

**For `/sy:ship`** (mechanical and self-contained):

- ordered concrete changes with file anchors/key signatures;
- existing primitives to reuse;
- standards authority and task-specific constraints/risk lenses;
- verification obligations (lens → claim → named evidence);
- design invariants — the deliberately small load-bearing list `sy:gate` must protect;
- tests and acceptance criteria;
- plan base: `PLAN_BASE_SHA` of the inspected base.

Present that content as a status update, then close the turn with a single `AskUserQuestion` call — approve as-is / request changes / other — per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`. Name the mutation the approval authorizes: on approval the run will post the ACTIVE plan comment (and, when superseding, mark the prior plan SUPERSEDED) and set the Task `ready`. Under auto-mode this sign-off is the consent point for those writes, so it states them rather than implying them. This is the plan's sign-off gate: do not infer approval from a reply that doesn't answer it.

After approval (marking a superseded plan SUPERSEDED rather than leaving two ACTIVE is the retroactive-honesty invariant in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`: an overruled record is corrected on its own surface, never left standing):

1. if an older plan is ACTIVE, edit its comment to:

```text
# Execution Plan v<N-1>
Status: SUPERSEDED
Superseded by: v<N>
```

2. append the new comment:

```text
# Execution Plan v<N>
Status: ACTIVE
Supersedes: v<N-1>   # omit for v1
```

3. verify by rereading plan headings/statuses that **exactly one** plan is ACTIVE.
4. set the Task to `ready` via the `tracker` skill — the plan is approved and it is now shippable.

End the plan with `/sy:ship <task>` and a one-line ship profile that names every phase's model explicitly: `START <model> / BUILD <model> / GATE <model> / effort <tier> / process <full|light>`, such as `START opus / BUILD opus / GATE frontier / effort high / process full`. Naming the phases individually leaves `/sy:ship` nothing to infer — a single-word tier forced it to guess which phases the word applied to, and `/sy:ship` passes each stated model straight through as that phase's model override.

Model tier is a quality floor, not a cost lever. Each phase's floor is declared in `config/floors.json` — `ship-start` cheap, `ship-build` standard, `sy:gate` frontier (frontier is absolute and cost-scaling-exempt, and the `GATE` model names the reviewer's tier rather than the lightweight GATE controller's) — and a plan may state a higher model for a phase when its own judgment calls for it. A stated model below a phase's floor is clamped up to the floor, never honored downward, by the resolver rather than by anyone remembering to. See `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`. Tune cost through **effort**: request lower effort only with evidence the work is mechanical end to end, and never lower review effort. Process tier `light` (no transcript attachment at handoff) is allowed only when no risk lenses are activated and the change is small; default `full`.

The ship profile never lowers review or build: `sy:gate` remains frontier tier and max effort, BUILD remains at least opus (the profile may raise it, never lower it), and immutable CI/review coverage is identical in both process tiers.

The bar: a fresh session reading the Task and sole ACTIVE plan can implement and open the PR without missing design decisions.

## 8. Capture the session

Delegate a subagent to render this `/sy:spec` session's transcript and attach it to the Task, every run, following the `tracker` skill's attachment flow (`$KIND=spec`). The reasoning trail behind the plan lands on the ticket with no manual `/export`, and the rendered text stays out of this context. Subagent delegation is primary; when the delegation itself is denied under auto-mode, the identical render-and-attach may run inline as an explicit permitted fallback — the same authorized-alternate-route case of the denied-write boundary in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md` — with the rendered text still handled by path only and never read back into this context. That inline path is deterministic-scan-only (no contextual review, to keep the transcript out of this context), so treat a clean scan there as evidence, not proof, per the `tracker` skill's attachment flow. If neither path completes, surface it loudly rather than skipping the attachment.
