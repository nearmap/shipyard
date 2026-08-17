---
name: tighten
description: >-
  PROACTIVE — tighten one piece of already-written text before it is sent or posted, cutting padding
  without losing a fact. Route on destination: a console turn to the user, a plan's sign-off half, an
  issue body or PR description, a plan's /sy:ship half, a dispatch brief, or one of this plugin's own
  files. Use it on any milestone turn, any drafted plan half, and any brief handed to another agent.
argument-hint: "[the text to tighten, or a path to it] — and its destination"
---

Rewrite one piece of text denser without losing anything its reader acts on. The patterns below are derived from measured corpora, not from style intuition: three full workflow sessions (258 KB of console turns), seven plan sign-off halves, and seven plan `/sy:ship` halves. Sentence-level filler was **absent** in all three — a 36-marker search over the console corpus returned four hits, all load-bearing. The bloat is structural, so this skill cuts structures, never adjectives.

## First: which destination?

Ask, or resolve from what you were handed. Several patterns below are destination-specific, and routing on audience alone sends a console caller into plan-half rules.

- **A console turn** to the user → `## Human-facing text` § Everywhere + § A console turn, then `## Mode`.
- **A plan's sign-off half** → `## Human-facing text` § Everywhere + § A plan sign-off half.
- **An issue body, or a PR description** → `## Human-facing text` § Everywhere; a PR description also drops anything the diff already shows.
- **A plan's `/sy:ship` half** → `## Agent-facing text`.
- **A dispatch brief for another agent** → `## Agent-facing text`.
- **One of this plugin's own files** (a skill, a reference, an agent brief) → `## Agent-facing text`.

## Ground rules

Preserve every fact. Cut padding, never substance: the output says everything the input said, in less text — it does not say less. Match the destination's register; a sign-off half is prose a human reads once, a `/sy:ship` half is instructions a builder executes.

No budget, threshold or target number governs this pass. "Under N lines" is not the goal and a percentage is not a quota; the cut list is the whole instrument. Measured removable share ran ~22-50% per file, so expect roughly a third — and when nothing on the list is present, change nothing.

The economy rule this pass enforces lives in one place: `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/context-economy.md`. Read it before rewriting anything; do not copy its tests into your output.

## Human-facing text

### Everywhere

**Restatement of what the reader already has.** Costs the reader a re-read to discover there is nothing new in it, and buries whatever *is* new that turn. Covers three measured shapes: a finding already delivered and told again in full; the "still blocking / still running" block reprinted on a turn where nothing about it changed; and one fact entered under two section headings of the same document.

- Before: "**Copilot's suppressed section was the whole review.** Across #2859's four rounds, every one of the four findings came from the collapsed *"suppressed due to low confidence"* block; the visible review said "no new comments" each time. Two were defects I'd have shipped — the trailing-slash root comparison that would have re-tried the just-404'd URL, and the unquoted `*` glob. It raises no thread, so nothing prompts you to look." *(the second telling, four turns after a first one of the same length)*
- After: "Copilot's suppressed section carried all four findings again (see earlier) — worth reading on every PR."
- Exception: the final handoff turn of a long session may restate a still-blocking item once, because the reader is likely re-entering cold. Restating it on every intermediate turn is the defect. And when a status ledger has one changed cell, print that row and its consequence, not the grid.

**Provenance clauses.** "Verified rather than assumed", "measured, not argued", "the adversary's strongest objection", "standards resolution reached the same conclusion". Costs a clause per claim to say how you know, when the reader is judging *what* you know. Ran to ~40 instances in one session and 34 across seven plan halves.

- Before: "Verified before changing, not after: / - `PerilScoresFunctor` already carries `file_client` across the same spawn pool in production, so this isn't new ground / - …"
- After: drop the header; the facts stand on their own.
- Exception: keep it when the provenance *is* the news ("I verified this myself because the source already got three things wrong") — once per session, not once per claim. Keep **owner** attribution always: it tells the reader whose decision they are re-opening. Drop internal-pass attribution.

### A console turn

**Pipeline-internal narration.** The largest single driver — 6.6-18.7% of three sessions. The reader is told how the agent's own machinery behaved: worker deaths, resume decisions, delegation budgets, model tiers, poller mechanics, state-file writes. None of it changes what they know or must do.

- Before: "**BUILD restarted.** The first worker hit a server-side API error moments after orientation, before writing any code. / Nothing was lost that matters, and one thing was gained: it had already created the `cpu-dev-rust` pixi environment in the main checkout, which is the multi-GB step. The worktree is clean at base, no commits, no PR, no lockfile churn — so the restart is idempotent by construction rather than by cleanup. / I re-dispatched fresh rather than resuming the dead agent, because `SendMessage`-resumed workers silently run at the default model on this box and BUILD has an opus floor. / Two things I seeded into the restart: [3 bullets] / I'll report when it returns."
- After: "BUILD died on an API error before writing code; restarted. Nothing lost — the multi-GB `cpu-dev-rust` env survives, and its prebuilt `resource_catalogue` is 11 days older than AM-1221, confirming the stale-binary trap the plan flags."
- Exception: keep it when the machinery failure changes the reader's trust in a result or costs them real time — "GATE's return carried stale SHAs and misattributed its own commits to you" tells them not to trust a prior report.

**Trailing sidebar appendices.** A milestone turn keeps going past its answer with an enumerated block of secondary findings: "Two things worth your attention", "Three things I did not do", "Two things I'm holding myself to". 29/17/35 turns carried one. The label forces a second item into existence.

- Before: "**Two things I'm holding myself to:** / - **`--admin` bypasses branch protection, so it can merge over a red or pending check.** I'll verify `head_sha == ci_green_sha == reviewed_sha` myself before invoking it rather than letting the flag paper over a stale gate. Your condition was "once CI is green," and `--admin` is the mechanism, not a waiver. / - **The handoff still runs first** — retrospective, usage and metrics JSON comments, transcript attachment, keys to `in-review`. …"
- After: delete. Neither item is a finding or a decision; both restate the agent's own procedure.
- Exception: one item the reader would act on differently if they knew it ("anything wrapping a `DictClient` cannot pickle at all") stays — as a single unlabelled low-key aside, never as a "Two things…" block.

**Delegation-brief pre-announcement.** Before any result exists, the turn describes in detail what each dispatched agent was told to investigate and why. ~6/3/10 instances at 900-1,700 chars each.

- Before: "Three investigations running against the reframed question: / - **Cost model** — the physical facts (file granularity per lookup index, row-group size, hilbert sortedness, and crucially whether sidecar metadata is fetched once per key per *process* or per `get()`), then explicit read counts for tile-major vs timeline-major-naive vs timeline-major-hilbert-ordered-with-cache. Includes a synthetic measurement of whether N narrow scans of one file cost N footer reads or zero when metadata is supplied. That number is what killed the design in January… [two more bullets of the same length]"
- After: "Three investigations running: I/O cost model, Gorillaz consumer map, and where the seam belongs."
- Exception: a brief encoding a decision the reader could veto before the work is spent ("I've told it explicitly not to assume source_v2's seam was right"). State that clause; drop the rest.

Do not touch short turns. In the console corpus 74/48/25 turns under 200 chars were already correct (`Let me check X.`, `Dispatching BUILD.`); all of the removable volume sits in turns of 1,500 chars or more.

### A plan sign-off half

**Mitigation essay welded to every risk.** ~13% of the corpus. The risk is one clause, then two to four times that defending the response: what catches it, why that is enough, what a wider version would break. The reader is deciding whether to approve, not auditing the guard rail.

- Before: "Three things stand in. GATE reads the pinned voyager config directly — it's on disk at `~/Development/datascience/voyager` and the SHA is pinned, so this is a real check rather than a trust exercise — and reproduces what it read… / Be precise about what survives all three. A name that isn't a `FeatureClass` member can't… One extra in-repo assertion was considered and left out…"
- After: "59 class names are hand-transcribed from another repo and no in-repo test can catch a valid-but-wrong substitution. GATE re-reads the pinned config and a human checks the draft PR; both are eyeball checks, not gates."
- Exception: keep the mitigation when its adequacy *is* the judgment being signed off — one clause.

**Machine-half detail carried in prose.** ~12%. Paths, build flags, exact values, versions, line anchors, SHAs, and the connective prose hauling them. One file carried 108 backticked spans. None of it changes an approve/reject, and all of it is already in the other half.

- Before: "A new `ui_libraries/uilib/src/lib/storage.ts` derives a per-service prefix once from `import.meta.env.BASE_URL`'s first path segment (`/argus/` → `argus`; `/pipeline-docs/v1/` → `pipeline-docs`; `"dev"` when `BASE_URL` is `/`) and exposes scoped `localStorage`/`sessionStorage` accessors."
- After: "A shared helper derives a per-service storage prefix from the deploy path, and an eslint rule bans direct storage access outside it."
- Exception: name the one file or symbol when the decision hinges on it — one anchor per claim, and only where the claim would be unfalsifiable without it.

**Extra named sections beyond the five.** ~11%, in 5 of 7 files: "Deliberate departure from the ticket", "Corrections to this work item", "Why the parent and not START", "Process note", a version preamble. Each re-narrates from a second angle what Approach already said.

- Before: a 1,411-char "Deliberate departure from the owner's steer" section following an Approach section that already described the shipping behaviour.
- After: two sentences inside Approach — what was asked, what is shipping, why.
- Exception: a genuine departure from an explicit owner steer is exactly what sign-off is for. Keep it, but inside Approach, not as its own section.

**Runner-up rejected alternatives.** The heading says "Strongest rejected alternative", singular; 6 of 7 files stacked a second and third, one filing a full one under Out of scope.

- Before: a second and third alternative at ~900 chars each, after the strongest one is already stated.
- After: delete both.
- Exception: an alternative the reader is likely to propose themselves. One sentence, one reason.

**Non-decision risks.** Entries the text itself flags as pre-existing, latent, not-a-regression, or an unchanging property of the environment. The self-flagging phrase is the tell.

- Before: "Risk 5: the deploy already serves both apps from one origin, which is pre-existing and not a regression this change introduces."
- After: delete.
- Exception: a pre-existing condition this change makes durable, harder to fix, or load-bearing is a real risk. The test is whether the plan changes the item's status, not whether the plan caused it.

Also delete on sight: the verbatim standing-policy boilerplate about out-of-scope being "a default contract, not a wall". It is standing policy, not this plan's content.

## Agent-facing text

Seven patterns, in descending measured volume. `Ordered changes` is 32-66% of a plan's ship half, so a pass aimed only at the trailing fields cannot reach a third — the two largest patterns both live inside the ordered changes.

**1. The same fact in several sections.** ~12%, the largest. Three axes: `docs requiring updates` re-listing paths an ordered change already names with the same line numbers; `tests and acceptance criteria` restating `verification obligations`; `design invariants` restating ordered changes in other words.

- Before *(one fact in four homes)*: change 1 "**omitting every key that reports a pass result** — no `scrubbed_vars`, no `redactions`, no `scanner`, no `scanner_findings`. A `0` or an empty list in any of them is the same false clean signal."; the obligation re-lists all four names; invariant 2 "the opaque branch omits every pass-result key rather than reporting a zero or an empty list for it"; tests "the report omits every pass-result key".
- After: state it once in change 1. The obligation becomes `lens: honest reporting; claim: no pass is reported that did not run; evidence: opaque-report test (change 1's key list).` The invariant and the tests entry go.
- Exception: a fact that must survive a *different* reader. **An invariant earns its line only when it states a property the ordered change does not state as a property** — that is what the gate reviewer checks against, and it is not always recoverable from imperative prose.

**2. Rationale a builder cannot act on.** ~8%, 3-8 instances per file. Absorbs "narration of how the plan reached its own conclusions", which is never a shape of its own.

- Before: "Snake-case, not kebab: kebab is exactly `CANONICAL_VERBS` (`scripts/validate.py:27`), and this adds no adapter method, so `skills/tracker/CONTRACT.md:132`'s "the adapter only provides `post-comment`/`get-issue`" holds and `check_agent_mcp_allowlists`'s exact-set check (`scripts/validate.py:462`, `granted & CANONICAL_VERBS`) is untouched — `SHIP_WORKER_TRACKER_VERBS` (`:58`) needs no edit."
- After: "Snake-case, not kebab (kebab = `CANONICAL_VERBS`, `scripts/validate.py:27`). No adapter method, no verb: `SHIP_WORKER_TRACKER_VERBS` (`:58`) unedited."
- Exception: rationale that **selects between two implementations the builder would otherwise pick wrong** — "Do **not** read `os.environ`: that is a false green, because a value set from Python arrives after libc init" chooses the predicate. The test is whether deleting the clause changes what gets typed. "Why we chose X" after an imperative fails it; "X not Y because Y silently passes" survives it.

**3. An obligation restating its own ordered change.** ~3,500 chars across 5 of 7 files; absent in the two that show the correct shape.

- Before: "lens: persisted shared storage; claim: no viewport-driven write to the scoped sidebar key, and an explicit wide-branch `toggle()` still persists; evidence: vitest — mount narrow via stubbed `matchMedia`, fire `change` narrow->wide->narrow, assert the key untouched throughout. The test must **also** assert the narrow branch mounted (trigger present, `<aside>` absent) and that `preference` round-trips, else it passes against the base unchanged — today's `AppShell` has no viewport listener and writes only inside `toggle` (`AppShell.tsx:38-44`)."
- After: "lens: persisted shared storage; claim: no viewport-driven write to the scoped sidebar key; evidence: step 13's narrow->wide->narrow case." — and the non-vacuity requirement moves into step 13, where the test is written.
- Rule, not an exception: **if the evidence is a test or step the plan already ordered, cite it by number.** Spell out only evidence the ordered changes do not create.

**4. The traced counterfactual.** 7 of 7 files. After stating a rule, the plan walks the failure state by state instead of naming the consequence. Not the same as pattern 2: this is a simulation of the bad path, not a justification of the good one. The compressed form is a gotcha and is kept; the traced form costs 3-5x the assertion it protects.

- Before: "Fakes 1 and 2 (`:71-77`, `:88-94`) increment on every invocation, and getting this wrong silently voids the retry case at `:96`: the head read would advance `n` to 1, `gh pr checks` would then return green on its first call, `_classify` would never yield `fail`, and the `failed_once` path at `:48-52` would go unexercised while the case still exits 0 and still passes."
- After: "Fakes 1 and 2 (`:71-77`, `:88-94`) increment on every invocation — if the head read consumes a state, the retry case at `:96` passes without exercising `failed_once` (`:48-52`)."
- Exception: when the failure is **invisible** — a test that still passes, a green that is false — the trace is the only thing stopping the builder simplifying it away. Cap it at one clause, not one paragraph.

**5. Negatives argued at length.** Two shapes: a `none` field carrying 150-450 chars justifying the word "none", and an audit trail of paths checked and found clean.

- Before: "`docs requiring updates`: **none**. Checked: `…/class_ids.md` explains the id-is-the-contract concept and the `F`/`A`/`G` import aliases but does not enumerate the enum's method surface; `parts/data-model/attributes.md` documents attribute *schemas* reproduced from `saturnlib.interfaces.core.attribute_schema`, not rollup membership; `parts/data-model/features.md` likewise. No part documents `AttributeClass.deprecated`, the idiom this follows. Nothing existing becomes stale."
- After: "`docs requiring updates`: **none**."
- Exception: a negative the builder is actively tempted to violate is an instruction, not an audit — "Leave `REQUIRED_TOOLS` and `:366`'s `len(REQUIRED_TOOLS) == 17` **unchanged**" stays, as does a negative ending in a live disposition ("**accept it and do not regenerate in this PR**").

**6. Workflow choreography the downstream phase already owns.** The machine half instructing a later phase on process: who attests, what goes in the PR body, how to frame a checkpoint, halting protocol. The workflow already encodes all of it, and one plan told its flip window four times.

- Before: "A "compared, matches" claim does not discharge this obligation — the read-out is the evidence, because BUILD writes the PR body and cannot be the party that attests to its own transcription. BUILD's own cross-check in step 1 is a first pass, not this."
- After: "GATE reproduces the six lists in its verdict; a "matches" claim does not discharge it."
- Exception: choreography that changes what the builder does — a halt condition genuinely belongs. Keep one telling of about 250 chars.

**7. Restating the standard instead of citing it.** `Standards authority` sections that name the authority and then reproduce its content: "Docstrings on public functions", "Tests mirror from `sy_tools/tests/`", "Ruff line length 120". All standing policy the run resolves anyway — and one plan cited the economy reference two lines above restating one of its rules.

- Before: four bullets of house style copied out of the authority the same section names.
- After: keep only the **delta** — where the standard is unenforced, where two authorities conflict, and which wins: "Ruff's `ANN` rules are unselected in the root config, so this is hand-checked, not linter-caught"; "where the user's global 'always add docstrings' conflicts, `CONTRIBUTING:18-25` wins".
- Exception: an unusual constraint the authority does not carry — a seam scan, a set of string invariants a validator enforces — keeps its full statement.

## Mode

This part is a correctness check, not a cut: across three sessions the derivation found five questions folded into status prose and two trailing-ask violations. A turn is exactly one of Status, Question or Action needed, and never a blend. A question belongs in the question tool, never folded into status prose — if a turn's prose asks anything, it is not a Status turn. An Action-needed block is the last thing in the turn. The three modes are defined once, in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`; read them there.

## Protect

Nothing mechanical stands between this rewrite and a lost anchor, so this is a hard rule rather than a caution: **no file path, symbol, config key, flag, SHA, job name, obligation, invariant or acceptance criterion leaves the text unless the entry being applied names it as removable.** An obligation loses a sentence, never its lens, its claim or its named evidence.

Two things a naive densifier takes that are not padding:

- **The vacuity defence** — "the size assertions are what keep the test from passing vacuously", "an always-empty fake exits 0 either way and would assert nothing", "else it passes against the base unchanged". These stop a builder writing a test that already passes on the base.
- **Current source quoted at an anchor.** A plan whose line numbers must be re-located by content uses the quoted line as the re-location key. It is not duplication.

Two entries above do legitimately move an anchor, and this rule does not contradict them: pattern 5 removes the *audit trail* behind a `none` field, and pattern 7 removes a *restated standard*. Both delete text whose anchors exist elsewhere by construction. Everything else keeps every anchor it started with.

## Report

Name the patterns cut, and nothing else. When the text was already tight, say so and change nothing.
