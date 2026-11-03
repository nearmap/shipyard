---
name: whiteboard
description: >-
  Work a fuzzy problem out with the user before anything is planned, measured or built. The user picks
  the surfaces: a decision page rendered from notes.md, a shared Excalidraw design board, or both.
  Every mode interviews the user hard on scope, requirements, constraints, resources and what done
  means, explores the possibilities, looks at the theory and a small sample of real data, cuts a broad
  idea round down with a debate, and argues it out turn by turn until one decision both agree with,
  then designs it on the board when the user is ready. Hands an unproven gain to /sy:spike, a
  settled decision to /sy:plan or /sy:spec, and a working version wanted first to /sy:mvp. Writes no
  production code and nothing to the tracker.
argument-hint: "[the problem in your own words | a board slug to resume]"
disable-model-invocation: true
effort: max
---

Two colleagues at a whiteboard, working out how to approach a problem before anyone builds it. The output is one decision both agree with, the reasoning behind it, and the evidence it rests on.

$ARGUMENTS

## Where it sits

- Before `/sy:spike`, `/sy:plan`, `/sy:spec` and `/sy:mvp`. A spike measures a hypothesis; a whiteboard finds the hypothesis worth measuring, or shows none is needed.
- The user picks the surfaces at Open: the decision page, the design board, or both. Every mode interviews, explores and designs; the surfaces only change where the work is shown.
- No tracker read or write, at any point, and no preflight. The commands it hands off to own the record.
- No production code, branches or PRs. Small probe scripts only, in the board's own folder.

## The record

One folder per problem, keyed by the board's slug under this repository's scratch directory: the `folder` that `page_render {"board_id": "<slug>"}`, `board_write` and `board_read` return (tool names resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`). It holds:

- `notes.md`: the full record, in every mode (aim, interview, decision tree, options, theory, what we know, probes, design, decision, dated log). A new session resumes from it. Its template is in `references/page.md`.
- `page.html`: the decision page, `notes.md` rendered for reading. With the page in use, call `page_render` after every change to `notes.md`; the open page refreshes itself. Never edit `page.html` by hand.
- `board.excalidraw`: the design board, once it is opened. Write it only through `board_write` and read it only through `board_read`; never edit the scene JSON by hand. Its shape is in `references/board.md`.
- `probes/`: probe scripts and their outputs.

The board is the user's surface as much as Claude's. **Start every turn with `board_read`** once the design board exists. Every `user_changed` field, `user` note or link, and deletion is a move to answer in that turn, before anything else: a note dragged to another cell or section is a layout choice to adopt, an edited text or label is a correction to adopt or dispute, a deleted note is a question about the design.

After every `board_write`, tell the user in one line to close and reopen the board tab: no editor reloads the file.

**A stale save.** A non-null `stale_save` means the user's editor saved a copy opened before the last write, and the board now holds the last write again. Say where the old copy was kept, then answer each item in `stale_save.changes`:

- a moved or edited note of Claude's, a renamed section or a relabelled link: re-apply it through `board_write`, a cell move as the note's new `section` and `at` in the spec;
- a move out of every section, an edit to a user's note, or an added scratch note: ask the user to redo it after reopening;
- an added note in a section, or an added section: add it again as Claude's own and say the owner changes, or ask the user to re-add it;
- a deleted note: ask whether it leaves the design; if it does, the user deletes it again after reopening;
- a deleted link or section: the user deletes it again after reopening, because `board_write` never deletes.

To resume: call `page_render` for the slug, read `notes.md`, then `board_read` when the board exists, then give a three-line recap (aim, leading option, strongest open objection) and carry on. A `page_render` that returns `created: true` means the slug named no board: confirm the slug with the user before carrying on.

**Writing for the reader.** The sections of `notes.md` that render on the page, and board note text, follow the `text.reader` setting, resolved with `get_config` and applied as written per `${CLAUDE_PLUGIN_ROOT}/skills/tighten/SKILL.md` § The reader (Precedence, The floor, Applied as written).

## Routing

- small cohesive evidence ⇒ read directly;
- broad archaeology ⇒ `sy:sweep`;
- one load-bearing code or data path ⇒ `sy:trace`, at most the resolved `limits.max_depth_agents` cap in flight.

Seed every agent with the anchors already in hand and the ground already covered. Returned briefs are leads: verify the decisive spans before a claim reaches the record.

## Talking to the user

Every turn follows `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`. The argument goes first as direct text, then one `AskUserQuestion` call carries the turn's choice point (below). Never a question mark in prose.

**Facts are Claude's job, decisions are the user's.** Never ask something code, data, docs or a probe can answer. Look it up or send an agent; only the questions downstream of a running lookup wait.

## Choice points

Every stage ends each turn on ONE `AskUserQuestion` call with the questions, labels and exits below. A stage exits only on the user's choice. The user can switch mode at any turn by saying so; confirm the switch in the next question.

| Stage | Question (header) | Options, recommended first | Exit |
|---|---|---|---|
| Open | "How do you want to work?" (Mode) | Page first, board later (Recommended): agree problem and options on the page; board offered when ready. / Page and board now: both from the start; sketch while we talk. / Board only: draw after a short interview; record in notes.md, no page. / Page only: no board; the design as text and tables on the page. | the mode for the session |
| Open | "Where should we start?" (Start) | Explore the possibilities (Recommended): 6 to 10 ideas, cut and add until the frame is right. / Straight to the options: skip framing; theory and the strongest few options. / Decide now: the options are known; go to the open decisions. | the first stage after the interview |
| Open, only when the page is in use and a hosted-page publishing tool exists | "Where should the page open?" (Page) | Local file (Recommended) / A private hosted page | where the page is shown |
| Interview | "Is this summary right?" (Summary) | Yes / Fix one point / Keep grilling me | Yes: next stage. Fix one point: reopens it. Keep grilling me: another interview turn |
| Explore | "Which ideas go?" (Cut), multiSelect, at most four ideas per question; and "Is the frame right?" (Frame) | Yes / Not yet / Add my idea (Other) | Yes: idea round. Not yet: another framing turn |
| Idea round | "Which options go forward?" (Options), multiSelect | at most four surviving options per question, strongest first, split across further questions in the same call; bring back a cut one (Other) | the ticked options stay alive |
| Discussion | the frontier, at most four decisions per call | at most four answers per decision, recommended first; any other answer (Other) | each answer settles its decision |
| Probe | "Run this probe?" (Probe) | Run it / Decide on what we have / Change the probe (Other) | run, skip, or redesign the probe |
| Readiness | "Open the design board?" (Board); and "Which sections?" (Sections), multiSelect, at most four sections per question, likeliest first, split across the call's remaining questions | Open now / Not yet / Page only from here | Open now: design stage. Not yet: back to the decisions, unmet items listed. Page only: the design goes in notes.md |
| Design turn | "What next on the board?" (Next) | the next piece to draw (Recommended) / Rework a section / Back to the decisions / I'm drawing: read my changes next turn | the next design turn, or back to the decisions |
| Scratch note | "Where does this note belong?" (Scratch) | the likeliest section (Recommended) / the next two sections / Leave it in scratch; any other section (Other) | adopt it into that section, or leave it |
| Converge | "Do you agree with this decision?" (Decision) | Agree / Agree, record my dissent / Disagree | Agree: hand-off. Dissent: recorded, then hand-off. Disagree: run the probe that decides it |
| Hand-off | "What next?" (Next step) | the route Hand-off picks (Recommended) / the two next-closest routes / Stop here; when none fits, Stop here (Recommended) / the three closest routes; the remaining route (Other) | the hand-off, or the session ends |

## 1. Open (one turn)

- Restate the problem in the user's words: who is asking and what they will do with the answer.
- Name the kind of work: root cause, method choice, system design, feasibility, measurement design, or a mix. It decides what counts as an answer: a root-cause question needs causes found, not a design.
- One aim per board; a second aim gets its own. Pick the slug.
- Call `page_render` once: in every mode it creates the board's folder and a minimal `notes.md`. Its `folder` is where `notes.md`, the board and `probes/` live.
- Suggest, as a single optional aside, `/rename whiteboard <slug>`.
- Close with the Open questions from the Choice points table, then record the answers in `notes.md`.

The third question appears only when the page is in use and this session has a tool that publishes a private hosted page. A hosted page puts the record outside the repository, so say so in the question. When the user picks it, publish the page after each `page_render`, through that tool only; the hosted page carries no `probes/` figures, so say once that they show only on the local page. A denied publish is final: say so once and carry on with the local file.

When the page is in use, tell the user once how to open it, per `references/page.md`.

## 2. Interview (every mode)

Before any idea or theory, grill the user over several turns until the problem is understood well enough to argue about.

- Cover, in whatever order the answers lead: scope (in and out), requirements, hard constraints, the resources available (people, time, compute, data, budget), who consumes the result and what they will do with it, and what done, success and delivery look like.
- Challenge each answer: why, what if it is not so, what breaks, and what the user gives up by it. Drill into a vague or conflicting answer before moving on, and skip whatever an answer already settled.
- Every question goes through `AskUserQuestion`: at most four per call, each with the recommended answer first, labelled `(Recommended)`, and at most three real alternatives after it.
- After each answer, record it in the Aim and interview section of `notes.md`.
- When nothing is left open, send a short summary as direct text (scope, requirements, constraints, resources, consumer, done criterion), then close on the Interview choice point. Record the user's confirming words in `notes.md`.

On Board only and Decide now the interview is short, but it still confirms the done criterion and the hard constraints.

## 3. Explore (when Start is Explore the possibilities)

- A bounded early look: the repository's docs per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/docs-mcp.md`, durable memory on the topic, the code path behind today's behaviour, and what the people asking said, as the user gives it.
- Then write 6 to 10 one-line ideas in `notes.md`, each naming the hypothesis it rests on.
- Each turn: answer the user's cuts and additions, add at most one thing, and close on the Explore choice point. A cut idea gets its reason recorded and stays out unless the user revives it.

## 4. Theory first, then an early look (bounded)

Write the **theory and prior** section of `notes.md` before reading any data, so the data can contradict it: what the code, the maths or the field already says (name the source), the assumptions each candidate rests on, the plausible approaches including ones nobody raised, and where theory says each one breaks.

Turn it into hypotheses `H1`, `H2`, ... Each names the measurement that tests it and **the result that would disprove it**. A hypothesis no result could disprove is rewritten. Every assumption goes in the assumptions table: its evidence, who checks it, and what happens if it is wrong.

Then look at the problem:

- the repository's own rules and docs first, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/docs-mcp.md`, and durable memory on the topic per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`;
- the code path behind today's behaviour, if one exists;
- real data: a sample of 20 to 50 real cases, the basic distributions, and a full-population count for anything that could be rare. No synthetic data here;
- what the people asking actually said, in their words.

Record which hypotheses the real data already strains; do not refit them to it. No conclusions, and nothing that costs more than a few minutes of compute: that becomes a planned probe. Keep a **coverage map** in `notes.md` (each area read, or excluded with its reason), so a blind spot is written down rather than read as covered.

When the look shows only one sensible option, say so with the reason and offer Decide now.

## 5. The idea round

The glossary is written now, from the terms the code and docs already use, plus one line per data source (id, what it is, which version); use only those terms afterwards.

Then open with breadth and cut it down before the user spends time on it:

1. Write 6 to 10 candidate ideas in `notes.md`, each one line naming the hypothesis it rests on. Include one the user has not raised and one that challenges the leading framing.
2. Pressure-test the list with `sy:debate`, unconditionally, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/debate.md`. The decision under debate is "which of these ideas can answer <aim>"; seed it with the theory, hypotheses, data and code anchors in `notes.md`, never Claude's ranking.
3. An idea leaves only with a concrete reason: an assumption the data or theory breaks, a constraint it violates, a cost the decision cannot carry, or an aim it cannot answer even if it works. Record that reason, its evidence tier and what would reopen it.
4. Show the user the three strongest survivors, ranked, each with its strongest point for and against, plus one line on what was cut, and close on the Idea round choice point.

When the Explore stage ran, its ideas are the list: skip step 1. Step 2's debate still runs, unconditionally, but only over the ideas the user kept, and the user's own cuts are never re-litigated. Decide now skips the idea round and goes straight to the frontier.

## 6. The discussion (most of the session)

Short turns, opinions, questions back. Each turn:

1. One line of where we are: leading option, strongest open objection.
2. Answer the user's point, and every board move `board_read` returned. Agree, disagree, or name what would settle it.
3. Add one thing: a counterpoint, a failure case, a possibility nobody raised, or the theory or practice that bears on it.
4. One line on what you did not check that bears on it.
5. Close with the frontier.

At most five live options in a turn, ranked. Side issues go to the parked list, not the turn.

**The frontier.** Open decisions form a tree in `notes.md`, each naming the decisions it depends on and its reversal cost. The frontier is every open decision whose prerequisites are settled. Close each turn on the Discussion choice point: one `AskUserQuestion` call holding the top of the frontier, at most four decisions, each with the recommended answer first, labelled `(Recommended)`, a one-line why, and at most four answers per decision. A question that depends on another still open waits. A "no" with a reason is a challenge to answer on its merits.

**Reversal cost.** Each decision is cheap or costly to undo, per `references/system-fit.md`. A costly one gets both lenses and a probe or a `sy:debate` before it settles; a cheap one is settled fast and revisited when it bites.

**Both lenses for every serious option.** Numerical science: the estimand, the data-generating story against the method's assumptions, bias, variance, uncertainty, behaviour on simulated data with a known answer, sensitivity, determinism (`references/method-fit.md`, `references/numerical.md`). System design: where it sits, the thinnest interface, the contract that becomes fixed, failure, reruns, cost at full scale, new state, rollout (`references/system-fit.md`). An option argued through one lens is not ready to win.

**Pushing back.**

- Every option is attacked, the user's favourite and Claude's own, with `references/challenges.md`. Steelman an option before ruling it out.
- Label every claim: checked (with its source), documented, inferred, proposed, or unknown. A causal claim also names its rung on the evidence ladder in `references/method-fit.md`.
- An objection needs a concrete failure case: this input gives this wrong result. A vague worry is not an objection, from either side.
- Change position only on a new argument or new evidence, and say which. Agreeing to end an argument is the failure this skill exists to prevent.
- Say when the user is wrong, with the evidence; when unsure, propose the probe that would decide it.
- Never name a parameter value the work has not established. Propose the knob, not the setting.

**Ruling out.** An option leaves with a one-line reason, its evidence tier, and what would reopen it. If it needs a paragraph to dismiss, it is not ruled out yet; it needs a probe.

**When the aim moves**, record the new aim version and its reason in `notes.md`, and say which options the change revives or kills.

Update `notes.md` (and call `page_render`) when something changes status (an option ruled out or revived, the aim moves, a probe returns, a decision settles), and log each change with its date.

## 7. Probes

The smallest real-data check that separates surviving options or tests the assumption the leading one rests on: minutes to an hour. Each names its hypothesis. Propose each one on the Probe choice point.

- Write the prediction in `notes.md` before running: which option wins, and why.
- Compare against the simplest baseline (today's behaviour, do nothing, the one-parameter version). An alternative that does not beat it by more than the uncertainty has not won.
- Measure both directions: what improves and what gets worse, including the slices, chosen beforehand, where the preferred option should lose.
- Give intervals from the right resampling unit (whatever the cases are correlated over), not from rows.
- Real data from the real source. Synthetic data may stress a mechanism or check parameter recovery, labelled as such, never in place of a real result.
- Agree with the user before anything costs real money or time.
- Report each result in the Probes table, plus one figure only when the result cannot be read without it. Check every figure through `sy:img-inspector`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`, before it goes into `notes.md`; never `Read` an image.

When a result misses its prediction, say so first. Then ask, in order, before patching anything: is the problem formulated wrongly, is the data wrong (selection, joins, units, labels), is a model assumption wrong, or is the evaluation design wrong? Log which, and correct the theory section if theory was wrong.

## 8. Readiness and design

In Page first mode, offer the design board on the Readiness choice point once three things hold: the interview is confirmed, the costly-to-reverse decisions are named, and every open decision is answered or is a question the board will answer. It is advice, never a gate: the user can open the board on any turn. Board only and Page and board open it right after the interview.

- Sections follow the kind of work: Data model, Data flow, System design, Algorithm. With several live approaches, one section per approach and view ("Data flow: A").
- Before the first data-flow drawing, size it roughly (volume, rate, latency, per `references/system-fit.md`) and put the numbers on the arrow labels.
- Each design turn: `board_read`, answer every move, add one thing, and close on the Design turn choice point. A new open decision goes back to the decision tree, and the drawing that depends on it waits.
- The scratch area, everything outside every section, is the user's: a write never moves it. Ask where each new scratch note belongs on the Scratch note choice point, and adopt it only once the user agrees.
- Page only: the design goes in the Design section of `notes.md` as text and tables.

## 9. Converge

The session converges when the frontier is empty and one option survives, or the remaining difference would not change the decision. "The data cannot separate these" is a valid end for a root-cause question when it names the intervention that would.

1. Pressure-test the leading decision with `sy:debate`, unconditionally, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/debate.md`, and answer its strongest objection in `notes.md`.
2. Run a pre-mortem with the user: it is six months on and this failed; why? Each answer becomes a residual risk or a probe.
3. Write the Decision: what, why, how it meets the done criterion from the interview (or what it leaves short), rejected alternatives with reasons, residual risks, and what result would make us revisit it. Compare the survivors side by side on quality against the baseline (with uncertainty), complexity, maintainability, compute cost and impact on the people using the result.
4. Close on the Converge choice point. Hand nothing off until both of you state the same understanding. A disagreement goes to the probe that decides it, or is recorded as whose call it is, with Claude's objection kept beside the decision.

## Hand-off

Only when the user asks. The Decision section is the brief.

- **Feasibility still in doubt** (a gain nobody has measured at representative scale): `/sy:spike <brief>`.
- **A multi-task objective**: `/sy:plan <brief>`.
- **A throwaway working version wanted first, with no spec or gate**: `/sy:mvp <brief>`.
- **One PR-sized change**: `/sy:spec <brief>`, or `/sy:spec <task>` with the brief when a task already exists.
- **No route fits** (a do-not-build or root-cause ending): Stop here, and record `Hand-off: none`.

Each brief names the opportunity, the evidence, the success bar (the interview's done criterion, as the user confirmed it), the invariants, the failure modes, the open questions, and the delivery shape: the smallest end-to-end version, its milestones, and acceptance checks written from the done criterion. End with the board's folder path and the kickoff line.

Every subagent dispatch resolves its model and effort from config and passes them as the `Agent` invocation's actual `model` and `effort` parameters, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`; a nested dispatch inherits nothing and must resolve again.
