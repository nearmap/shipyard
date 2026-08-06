---
name: ship
description: >-
  Execute one fully planned Task to a reviewable PR with isolated writers,
  immutable independent review, fresh CI/review coverage, and durable accounting.
argument-hint: "<task>"
disable-model-invocation: true
---

Execute one planned Task to a reviewable PR. Never merge automatically. Explicit merge authorization enters `references/merge-accounting.md`.

$ARGUMENTS

## Invariants

- Before classifying state or dispatching any worker, the parent runs the tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`); a failure stops here with its single `## Action needed` block — no worker starts against an unusable tracker.
- Exactly one `# Execution Plan vN` is ACTIVE; otherwise stop for `/sy:spec`.
- Check plan-base freshness before building: material drift between `PLAN_BASE_SHA` and the ship base returns to `/sy:spec`.
- Resolve standards before code.
- The process tier (`full|light`) scales accounting records, never CI/review coverage.
- Resolve tracker blockers before branching; decomposed/superseded closure is not delivery.
- Read small cohesive surfaces directly; delegate large/verbose work — standards resolution, verbose verification (test/lint/type runs), and CI triage all count as verbose, and every added delegate enters `agents_used`. At most the resolved `limits.max_depth_agents` cap depth agents in flight — resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`.
- START, BUILD, and GATE each run as disposable autonomous worker subagents (`sy:ship-start`/`sy:ship-build`/`sy:ship-gate`); the parent is a thin dispatcher owning durable state (state file, worktrees, PR/tracker identity), the HANDOFF retro/accounting, MERGE, and all user interaction.
- Workers never prompt the user; each returns `done`, `needs-decision`, `bail-to-spec`, or `blocked` per the worker contract, and BUILD additionally returns `needs-trace`. The parent resolves `needs-decision` from plan/standards/code first and asks you via `AskUserQuestion` only when genuinely ambiguous, and resolves `needs-trace` by dispatching the trace itself without asking you; either way it then dispatches a fresh continuation worker from the checkpoint.
- Any phase return may carry `MEMORY_REFUTE` candidates — `{title, evidence, correction}`, an empty correction meaning tombstone — for a seeded memory anchor this phase's own direct observation contradicted, durably mirrored in `ship-state.yaml`'s `memory_refutations`. No ship worker agent gains `memory_add`/`memory_refute` tool access: the parent holds every write against the user's global, cross-repo store, and applies each candidate via `memory_refute` per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`.
- Checkpoints are slice/step-granular and idempotent: a continuation re-does no committed, pushed, or tracker-posted work and re-creates no recorded worktree. A phase exceeding the resolved `ship.escalation.max_needs_decision` threshold of `needs-decision` returns escalates to `/sy:spec` as underspecified; a phase exceeding the resolved `ship.escalation.max_needs_trace` threshold of `needs-trace` returns without reaching `done` escalates the same way on its own separate count, since a trace detour is missing evidence rather than ambiguity-driven underspecification. Resolve both per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`.
- A review or build finding that is small, adjacent, and low-risk is fixed in-branch as a recorded scope extension, not deferred to a follow-up: the plan's declared scope is a default contract, and a follow-up must justify itself against the in-branch fix (see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/scope-discipline.md`).
- Every writable delegate gets a caller-created, recorded worktree.
- Worktrees live under `<resolved worktree root>/<branch>`, where the root is the value `get_config {"key": "worktree.root"}` reports — by default the sibling directory beside the repo (e.g. `/path/to/myrepo` → `/path/to/myrepo-worktrees/<branch>`), overridable via `worktree.root` in config — never nested inside the working tree; an in-tree worktree pollutes the main checkout's status, search, and diffs.
- `sy:gate` reviews an isolated worktree pinned to exact base/head SHAs.
- Current PR HEAD must equal CI-green SHA and reviewed SHA before handoff or merge.
- When the plan declares a pre-gate checkpoint, current PR HEAD must also equal that checkpoint's cleared SHA before GATE is dispatched (§ Pre-gate checkpoint). A plan that declares none — the default — never blocks here and leaves today's BUILD→GATE handoff exactly as it is.
- Model tier is a quality floor, and the parent wires it mechanically: START's and BUILD's dispatches each carry an explicit `Agent` invocation model override, never omitted, read from the plan's per-phase declaration and clamped up to that worker's own floor. The parent never infers a phase's model from a single word and never assumes a worker inherits the parent session's model, because a worker's own frontmatter model always wins over an omitted override. The profile's `START` and `BUILD` models are what those workers run at; the `GATE` worker's own dispatch from the parent stays at its fixed `sonnet` tier regardless of profile, and it is the GATE worker itself — not the parent — that consumes the profile's `GATE` tier via its own Resolve-gate-model procedure (`references/immutable-gate.md` § Resolve gate model) to set the model override for its nested `sy:gate` call. Each phase worker runs at least its declared tier (BUILD stays at least opus) and the ship profile may raise but never lower it — and this is now a deterministic check rather than an instruction: every floor lives in `config/floors.json`, the resolver clamps to it, and a config that tries to drop a worker below its floor is refused by name. The full dispatch rule, including requested-vs-observed reconciliation and why effort is not symmetrical with model, is `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`.
- Cost-scaling never touches the reviewer: for Trivial-priority tickets the ship profile may scale BUILD effort and the process tier down, but `sy:gate` keeps its frontier tier, max effort, and full coverage on every path — including the trivial-diff and bounded-fix paths. `config/floors.json` pins `gate` at `min_model: frontier` / `min_effort: max`, so this is enforced by the resolver, not only by this sentence.
- After merge authorization, one small bounded fix may land through the authorized bounded-fix → focused-delta-gate → merge sub-flow in `references/merge-accounting.md`: the delta review is valid only when the prior reviewed head is the immutable base and the new head is the immutable head; anything broader re-enters GATE.
- Resolve gate model explicitly and pass it as the Agent invocation's actual model override; record requested and transcript-observed models separately.
- Resolve START's and BUILD's models explicitly from the plan's stated per-phase tiers and pass each as its Agent invocation's actual model override, mirroring gate; record requested and transcript-observed models separately per phase (`references/start-resume.md` § Resolve start model, `references/implementation.md` § Resolve build model).
- Token accounting must aggregate the main ship transcript **and all nested subagent transcripts**.
- Images stay out of the long-running context: figures/screenshots/plots are inspected only through short-lived `sy:img-inspector` subagents that return text verdicts, and no image `Read` appears in a BUILD or GATE transcript (see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`).
- Tracker machine logs are small standalone JSON comments; never bury usage or metrics JSON inside retrospectives or plan comments.
- Talking to you follows exactly one of three modes per turn — status update, `AskUserQuestion`, or an isolated `## Action needed` block — never blended; see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`.
- Mandated external writes obey write integrity: a posted record later overruled or found wrong is corrected on its own surface, and a denied write is never rerouted through another tool/path to force it through — surfaced loudly instead. Under auto-mode these are the operator's only safeguard against a stale or forced write; see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`.

## Compression boundary

Agent-to-agent returns use each agent's compact return contract, and a return backs its load-bearing claims with checkable evidence pointers. The parent trusts an evidence-backed brief and spot-checks decisive spans rather than re-deriving ground truth; it re-verifies in full only a load-bearing claim the brief leaves unbacked. Seed every agent prompt with known anchors — paths, symbols, plan step, keys — and name ground already covered; agents must not rediscover what the caller knows. Human-facing tracker/PR artifacts remain clear prose; machine logs remain compact JSON.

## Worker contract

The parent dispatches each of START, BUILD, and GATE (and any resumed segment) to its worker agent (`sy:ship-start`/`sy:ship-build`/`sy:ship-gate`), seeded with the phase procedure reference and the compact state brief; it never runs those procedures inline. HANDOFF and MERGE run in the parent — the retro needs cross-phase knowledge and MERGE needs the live worktrees and your authorization — with their verbose reads, including the transcript render, delegated. A worker runs autonomously and never blocks on long external state (CI, deploys) by polling in-context or self-resuming a monitor: it waits with a token-free background poller or returns `blocked` with a checkpoint. It returns exactly one:

- `done` — phase complete; updated state brief (SHAs, PR, `agents_used`, `accepted_deviations`, `memory_refutations`) with checkable evidence backing every load-bearing claim.
- `needs-decision` — an idempotent checkpoint plus a question brief: what was attempted, why blocked, the options, and the plan/standards spans bearing on it.
- `needs-trace` (BUILD only) — an idempotent checkpoint plus a bounded empirical question: what is still unresolved, its seed anchors, and why it crossed the spot-check bound (the same bound as `/sy:spec` §3) instead of staying a direct read. No candidate options — only a question a trace can resolve.
- `bail-to-spec` — the plan's contract or architecture is invalidated; reason and offending anchors. Also the escape hatch when a phase keeps returning `needs-trace` without converging.
- `blocked` — external cause (merge authorization, infrastructure); what is required.

On any return — `done`, `needs-decision`, `needs-trace`, `bail-to-spec`, or `blocked` alike — the parent first applies every pending `MEMORY_REFUTE` candidate via `memory_refute`, before doing anything else with that return: a correction has to land the moment it is discovered rather than waiting for the HANDOFF retro, which a bail or a crash would never reach. Applying a candidate removes it from `memory_refutations` in state, and each worker emits only the candidates its own segment newly observed — never an accumulated history — so a continuation worker cannot re-apply an already-applied candidate over a newer correction that superseded it. Then the parent advances on `done`; resolves `needs-decision` from plan/standards/code and dispatches a continuation worker from the checkpoint, asking via `AskUserQuestion` only when the choice is genuinely ambiguous; resolves `needs-trace` by dispatching a bounded, foreground `sy:trace` itself — mechanical, not a judgment call, so it never reaches you as a question — and resuming the worker from the checkpoint with the findings folded in as a new anchor; stops for `/sy:spec` on `bail-to-spec`; and surfaces `blocked` to you as an `## Action needed` block naming the external cause. Parent-owned throughout: user interaction, durable-state ownership, the START profile guard, and HANDOFF and MERGE orchestration.

## Pre-gate checkpoint

Honouring the plan's declared `pre-gate checkpoint` field is parent-owned and never a worker's, resolved the way the ship profile already is — `references/start-resume.md` § Resolve start model is the precedent: a plan-declared value the parent reads, resolves, and stamps before dispatch, never something the worker picks up for itself. START stamps the declared channel into `pregate_checkpoint_channel` once at dispatch; `pregate_checkpoint_cleared_sha` starts `null` and `pregate_checkpoint_changes_requested` starts `0`. A plan that declares nothing leaves the channel `null`, and a `null` channel skips this section entirely — including on a resume from an older state file that predates these fields, where an absent channel reads the same as a declared-none one.

Before GATE is dispatched — fresh after BUILD's `done`, or on a resume classifying straight to GATE — the parent checks that the channel is set and that `pregate_checkpoint_cleared_sha` equals the current `head_sha`. Set and equal means this checkpoint is already cleared on this exact commit, so GATE goes out unchanged. Set and unequal is the one case that pauses: the parent presents the draft PR URL (and, for a `running-preview` channel, a reminder that this is a local look you take yourself — this step never launches anything) and asks via `AskUserQuestion` per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`, framed as whether this looks like the right thing to have built — product and UX judgment, offering proceed to GATE / request changes / other. It is never framed as a correctness review and never invites one: `sy:gate` alone owns correctness, and a checkpoint that starts collecting defects is duplicating the reviewer while spending your attention before the review has even run.

Proceed records the current `head_sha` as `pregate_checkpoint_cleared_sha` and dispatches GATE. Request changes increments `pregate_checkpoint_changes_requested` by one and dispatches a fresh BUILD continuation from BUILD's own `phase_checkpoint` — the identical continuation-from-checkpoint mechanism `needs-decision` already uses, and precisely why the worker contract needs no sixth return value and stays closed at five: the checkpoint is a parent-side pause between two dispatches, not something a worker signals, prompts for, or can even observe. BUILD's next `done` lands a new head, which leaves the recorded cleared SHA stale against it, so this section re-engages on the new commit without any special resume bookkeeping.

GATE itself never re-enters this section once dispatched. Its fix-cycle commits are review remediations bounded by `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/scope-discipline.md`, not the product/UX surface this checkpoint exists to catch, so re-asking on each of them would spend your attention on exactly the changes the question has nothing to say about.

## State router

Preflight (above) runs once, first, ahead of this classification — including on resume, since a checkpoint can route straight to BUILD or GATE without ever passing through START.

On resume the parent also loads `ship-state.yaml` here and drains it before dispatching whichever phase the classification lands on: any `memory_refutations` still listed are pending, not history, so the parent applies each via `memory_refute` and clears the list from state — the same drain rule as an in-flight worker return (§ Worker contract). It belongs in this pre-dispatch step because a resume routing straight to BUILD or GATE passes through no phase procedure that could own it, and the HANDOFF retro deliberately does not backstop it; an undrained list means the refuted anchor is still read back as if it held.

The same reasoning covers a declared but unresolved pre-gate checkpoint. Before any GATE dispatch — fresh after BUILD's `done`, or a resume classifying straight to GATE — the parent checks `pregate_checkpoint_channel` against `pregate_checkpoint_cleared_sha == head_sha` and, on a mismatch, routes into § Pre-gate checkpoint's same `AskUserQuestion` rather than dispatching GATE directly; a `null` or absent channel falls straight through. It belongs in this same pre-dispatch step for the identical reason the memory drain does: a resume routing straight to GATE passes through no phase procedure that could own the check, so anything left to a phase to perform simply would not run on that path.

Classify first, then load only the needed procedure:

```text
START     initialize/resume ownership        → ship-start worker · references/start-resume.md
BUILD     implement/integrate plan, draft PR → ship-build worker · references/implementation.md
GATE      CI, immutable review, promote      → ship-gate  worker · references/immutable-gate.md
HANDOFF   retro, usage, transcript, metrics  → parent · references/handoff-accounting.md (scan delegated)
MERGE     direct user merge authorization    → parent · references/merge-accounting.md
```

The parent classifies state, dispatches the matching phase to a worker, and acts on the return per the worker contract. Do not preload mutually exclusive procedures; a worker loads only its own.

## Completion bar

Normal completion is a reviewable PR with current acceptance evidence, green CI and independent review covering the same head, the automated reviewer explicitly requested with the request confirmed (when `ship.request_ci_reviewer` resolves true — see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`), and every review thread — bot and human — reconciled and answered rather than left for you, a doc-accuracy self-check over the shipped documentation diff, Task `in-review`, human retrospective, standalone JSON usage/metrics comments, and (full tier, when `transcript.attach` resolves true — see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`) scanned transcript attachment. Then stop at handoff.
