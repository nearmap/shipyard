---
name: tighten
description: >-
  PROACTIVE — tighten one piece of already-written text before it is sent or posted, cutting padding
  without losing a fact. Route on destination: a console turn to the user, a plan's sign-off half, an
  issue body or PR description, a plan's /sy:ship half, a dispatch brief, or one of this plugin's own
  files. Use it on any milestone turn, any drafted plan half, and any brief handed to another agent.
argument-hint: "[the text to tighten, or a path to it] — and its destination"
---

Rewrite one piece of text denser without losing anything its reader acts on. The patterns below were derived by measuring real corpora rather than from style intuition, and the finding that shapes this whole skill is that sentence-level filler was **absent**: the bloat is structural. Cut structures, never adjectives — do not go hunting for weasel words.

## First: which destination?

Ask, or resolve from what you were handed. Several patterns below are destination-specific, and routing on audience alone sends a console caller into plan-half rules.

- **A console turn** to the user → `## Human-facing text` § Everywhere + § A console turn, then `## Mode`.
- **A plan's sign-off half** → `## Human-facing text` § Everywhere + § A plan sign-off half.
- **An issue body, or a PR description** → `## Human-facing text` § Everywhere; a PR description also drops anything the diff already shows.
- **A plan's `/sy:ship` half, a dispatch brief for another agent, or one of this plugin's own files** (a skill, a reference, an agent brief) → `## Agent-facing text`.

## Ground rules

Preserve every fact. Cut padding, never substance: the output says everything the input said, in less text — it does not say less. Match the destination's register; a sign-off half is prose a human reads once, a `/sy:ship` half is instructions a builder executes.

No budget, threshold or target number governs this pass. "Under N lines" is not the goal, a percentage is not a quota, and the cut list is the whole instrument — when nothing on it is present, change nothing.

The economy rule this pass enforces lives in one place: `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/context-economy.md`. Read it before rewriting anything; do not copy its tests into your output.

## Human-facing text

### Everywhere

**Restatement of what the reader already has.** Costs the reader a re-read to discover there is nothing new in it, and buries whatever *is* new that turn. Three measured shapes: a finding already delivered and told again in full; the "still blocking / still running" block reprinted on a turn where nothing about it changed; and one fact entered under two section headings of the same document.

- Cut: a second full telling of an earlier finding → "the suppressed review section carried all four findings again (see earlier) — worth reading on every PR."
- Exception: the final handoff turn of a long session may restate a still-blocking item once, because the reader is likely re-entering cold. Restating it on every intermediate turn is the defect. And when a status ledger has one changed cell, print that row and its consequence, not the grid.

**Provenance clauses.** "Verified rather than assumed", "measured, not argued", "the adversary's strongest objection", "standards resolution reached the same conclusion". Costs a clause per claim to say *how* you know, when the reader is judging *what* you know.

- Cut: the "Verified before changing, not after:" header above a list of facts. The facts stand on their own.
- Exception: keep it when the provenance *is* the news ("I verified this myself because the source already got three things wrong") — once per session, not once per claim. Keep **owner** attribution always: it tells the reader whose decision they are re-opening. Drop internal-pass attribution.

### A console turn

**Pipeline-internal narration.** The largest single driver. The reader is told how the agent's own machinery behaved: worker deaths, resume decisions, delegation budgets, model tiers, poller mechanics, state-file writes. None of it changes what they know or must do.

- Cut: three paragraphs on a restarted worker — which error killed it, what survived, why it was re-dispatched rather than resumed, what was seeded into the retry → "BUILD died on an API error before writing code; restarted. Nothing lost — the multi-GB toolchain env survives, and its prebuilt binary predates this task, confirming the stale-binary trap the plan flags."
- Exception: keep it when the machinery failure changes the reader's trust in a result or costs them real time — "GATE's return carried stale SHAs and misattributed its own commits to you" tells them not to trust a prior report.

**Trailing sidebar appendices.** A milestone turn keeps going past its answer with an enumerated block of secondary findings: "Two things worth your attention", "Three things I did not do", "Two things I'm holding myself to". The label forces a second item into existence.

- Cut: the whole block, whenever its items restate the agent's own procedure rather than deliver a finding or a decision.
- Exception: one item the reader would act on differently if they knew it ("anything wrapping that client cannot pickle at all") stays — as a single unlabelled low-key aside, never as a "Two things…" block.

**Delegation-brief pre-announcement.** Before any result exists, the turn describes in detail what each dispatched agent was told to investigate and why.

- Cut: a paragraph per delegate → "Three investigations running: the I/O cost model, the consumer map, and where the seam belongs."
- Exception: a brief encoding a decision the reader could veto before the work is spent ("I've told it explicitly not to assume the old seam was right"). State that clause; drop the rest.

Do not touch short turns. Turns under 200 chars (`Let me check X.`, `Dispatching BUILD.`) were already correct across the corpus; all of the removable volume sits in turns of 1,500 chars or more.

### A plan sign-off half

**Mitigation essay welded to every risk.** The risk is one clause, then two to four times that defending the response: what catches it, why that is enough, what a wider version would break. The reader is deciding whether to approve, not auditing the guard rail.

- Cut: down to the exposure and the guard's real strength → "59 class names are hand-transcribed from another repo and no in-repo test can catch a valid-but-wrong substitution. GATE re-reads the pinned config and a human checks the draft PR; both are eyeball checks, not gates."
- Exception: keep the mitigation when its adequacy *is* the judgment being signed off — one clause.

**Machine-half detail carried in prose.** Paths, build flags, exact values, versions, line anchors, SHAs, and the connective prose hauling them; one file carried 108 backticked spans. None of it changes an approve/reject, and all of it is already in the other half.

- Cut: "a new `<path>` derives a per-service prefix from `<env var>`'s first path segment (three worked examples) and exposes scoped accessors" → "a shared helper derives a per-service storage prefix from the deploy path, and a lint rule bans direct storage access outside it."
- Exception: name the one file or symbol when the decision hinges on it — one anchor per claim, and only where the claim would be unfalsifiable without it.

**Extra named sections beyond the five.** "Deliberate departure from the ticket", "Corrections to this work item", "Why the parent and not START", "Process note", a version preamble. Each re-narrates from a second angle what Approach already said.

- Cut: fold into Approach as two sentences — what was asked, what is shipping, why.
- Exception: a genuine departure from an explicit owner steer is exactly what sign-off is for. Keep it, but inside Approach, not as its own section.

**Runner-up rejected alternatives.** The heading says "Strongest rejected alternative", singular; most files stack a second and a third behind it.

- Cut: delete every alternative after the strongest.
- Exception: an alternative the reader is likely to propose themselves. One sentence, one reason.

**Non-decision risks.** Entries the text itself flags as pre-existing, latent, not-a-regression, or an unchanging property of the environment. The self-flagging phrase is the tell.

- Cut: delete.
- Exception: a pre-existing condition this change makes durable, harder to fix, or load-bearing is a real risk. The test is whether the plan changes the item's status, not whether the plan caused it.

Also delete on sight: the verbatim standing-policy boilerplate about out-of-scope being "a default contract, not a wall". It is standing policy, not this plan's content.

## Agent-facing text

Seven patterns, in descending measured volume. `Ordered changes` is 32-66% of a plan's ship half and the two largest patterns both live inside it, so a pass aimed only at the trailing fields cannot reach a third.

**1. The same fact in several sections.** The largest. Three axes: `docs requiring updates` re-listing paths an ordered change already names with the same line numbers; `tests and acceptance criteria` restating `verification obligations`; `design invariants` restating ordered changes in other words.

- Cut: state the fact once, in the ordered change. The obligation shrinks to `lens: …; claim: …; evidence: <that change>`, and the invariant and the tests entry go.
- Exception: a fact that must survive a *different* reader. **An invariant earns its line only when it states a property the ordered change does not state as a property** — that is what the gate reviewer checks against, and it is not always recoverable from imperative prose.

**2. Rationale a builder cannot act on.** Absorbs "narration of how the plan reached its own conclusions", which is never a shape of its own.

- Cut: "Snake-case, not kebab: kebab is exactly `CANONICAL_VERBS` (`scripts/validate.py:27`), and this adds no adapter method, so the adapter contract still holds and the exact-set check is untouched — `SHIP_WORKER_TRACKER_VERBS` needs no edit." → "Snake-case, not kebab (kebab = `CANONICAL_VERBS`, `scripts/validate.py:27`). No adapter method, no verb: `SHIP_WORKER_TRACKER_VERBS` unedited."
- Exception: rationale that **selects between two implementations the builder would otherwise pick wrong** — "Do **not** read `os.environ`: that is a false green, because a value set from Python arrives after libc init" chooses the predicate. The test is whether deleting the clause changes what gets typed. "Why we chose X" after an imperative fails it; "X not Y because Y silently passes" survives it.

**3. An obligation restating its own ordered change.**

- Cut: an obligation whose `evidence` respecifies a test the plan already ordered → "evidence: step 13's narrow->wide->narrow case", with the non-vacuity requirement moved into step 13, where the test is written.
- Rule, not an exception: **if the evidence is a test or step the plan already ordered, cite it by number.** Spell out only evidence the ordered changes do not create.

**4. The traced counterfactual.** After stating a rule, the plan walks the failure state by state instead of naming the consequence. Not the same as pattern 2: this is a simulation of the bad path, not a justification of the good one. The compressed form is a gotcha and is kept; the traced form costs 3-5x the assertion it protects.

- Cut: a five-clause walk of how a miscounted fake voids a retry case → "the fakes at `:71-77` and `:88-94` increment on every invocation — if the head read consumes a state, the retry case at `:96` passes without exercising `failed_once` (`:48-52`)."
- Exception: when the failure is **invisible** — a test that still passes, a green that is false — the trace is the only thing stopping the builder simplifying it away. Cap it at one clause, not one paragraph.

**5. Negatives argued at length.** Two shapes: a `none` field carrying 150-450 chars justifying the word "none", and an audit trail of paths checked and found clean.

- Cut: "`docs requiring updates`: **none**. Checked: [three doc paths, each with a sentence on why it does not apply]. Nothing existing becomes stale." → "`docs requiring updates`: **none**."
- Exception: a negative the builder is actively tempted to violate is an instruction, not an audit — "Leave `REQUIRED_TOOLS` and its length assertion **unchanged**" stays, as does a negative ending in a live disposition ("**accept it and do not regenerate in this PR**").

**6. Workflow choreography the downstream phase already owns.** The machine half instructing a later phase on process: who attests, what goes in the PR body, how to frame a checkpoint, halting protocol. The workflow already encodes all of it, and one plan told its flip window four times.

- Cut: "a 'compared, matches' claim does not discharge this obligation — the read-out is the evidence, because BUILD writes the PR body and cannot be the party that attests to its own transcription. BUILD's own step-1 cross-check is a first pass, not this." → "GATE reproduces the six lists in its verdict; a 'matches' claim does not discharge it."
- Exception: choreography that changes what the builder does — a halt condition genuinely belongs. Keep one telling of about 250 chars.

**7. Restating the standard instead of citing it.** `Standards authority` sections that name the authority and then reproduce its content: "Docstrings on public functions", "Tests mirror from `sy_tools/tests/`", "Ruff line length 120". All standing policy the run resolves anyway.

- Cut: keep only the **delta** — where the standard is unenforced, where two authorities conflict, and which wins: "Ruff's `ANN` rules are unselected in the root config, so this is hand-checked, not linter-caught"; "where the user's global 'always add docstrings' conflicts, `CONTRIBUTING:18-25` wins".
- Exception: an unusual constraint the authority does not carry — a seam scan, a set of string invariants a validator enforces — keeps its full statement.

## Mode

A correctness check, not a cut. A turn is exactly one of Status, Question or Action needed, and never a blend; the two violations this pass catches are a question folded into status prose — if a turn's prose asks anything, it is not a Status turn — and an Action-needed block that is not the last thing in the turn. The three modes are defined once, in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`; read them there.

## Protect

Nothing mechanical stands between this rewrite and a lost anchor, so this is a hard rule rather than a caution: **no file path, symbol, config key, flag, SHA, job name, obligation, invariant or acceptance criterion leaves the text unless the entry being applied names it as removable.** An obligation loses a sentence, never its lens, its claim or its named evidence.

Two things a naive densifier takes that are not padding:

- **The vacuity defence** — "the size assertions are what keep the test from passing vacuously", "an always-empty fake exits 0 either way and would assert nothing", "else it passes against the base unchanged". These stop a builder writing a test that already passes on the base.
- **Current source quoted at an anchor.** A plan whose line numbers must be re-located by content uses the quoted line as the re-location key. It is not duplication.

## Report

Name the patterns cut, and nothing else. When the text was already tight, say so and change nothing.
