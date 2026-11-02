---
name: whiteboard
description: >-
  Work a fuzzy problem out with the user before anything is planned, measured or built. Look at the
  theory and a small sample of real data, cut a broad idea round down with a debate, then argue it
  out turn by turn over a shared Excalidraw board the user can drag and edit, until one decision
  both agree with. Hands an unproven gain to /sy:spike and a settled decision to /sy:plan or
  /sy:spec. Writes no production code and nothing to the tracker.
argument-hint: "[the problem in your own words | a board slug to resume]"
disable-model-invocation: true
effort: max
---

Two colleagues at a whiteboard, working out how to approach a problem before anyone builds it. The output is one decision both agree with, the reasoning behind it, and the evidence it rests on.

The user often does not know the exact aim until they see the problem, and may not know the theory or design practice that bears on it. That is why the session exists: look at the problem first, bring that knowledge, and argue until the two of you agree.

$ARGUMENTS

## Where it sits

- Before `/sy:spike`, `/sy:plan` and `/sy:spec`. A spike measures a hypothesis; a whiteboard finds the hypothesis worth measuring, or shows none is needed.
- No tracker read or write, at any point, and no preflight. The commands it hands off to own the record.
- No production code, branches or PRs. Small probe scripts only, in the board's own folder.

## The board

One folder per problem, keyed by the board's slug under this repository's scratch directory: the `folder` that `board_write {"board_id": "<slug>", "spec": ...}` and `board_read {"board_id": "<slug>"}` return (tool names resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`). It holds:

- `notes.md`: the full record (aim, glossary, theory, hypotheses, decision tree, claims, options, probes, decision, dated log). A new session resumes from it.
- `board.excalidraw`: the shared board. Write it only through `board_write` and read it only through `board_read`; never edit the scene JSON by hand.
- `probes/`: probe scripts and their outputs.

`references/board.md` holds the `notes.md` template, the lanes and note kinds, and the line telling the user how to open the board.

The board is the user's surface as much as Claude's. **Start every turn with `board_read`.** Every `user_changed` field, `user` note or link, and deletion is a move to answer in that turn, before anything else: a note dragged to Ruled out needs its reason recorded, a revived note gets argued on its merits, an edited text is a correction to adopt or dispute. `board_write` refuses to overwrite any user edit the spec has not adopted, so an unanswered move cannot be lost silently. A non-null `stale_save` means the user's editor saved a copy opened before the last write: say so, show where it was kept, and ask what they changed in it.

To resume: read `notes.md`, then `board_read`, then give a three-line recap (aim, leading option, strongest open objection) and carry on.

## Routing

- small cohesive evidence ⇒ read directly;
- broad archaeology ⇒ `sy:sweep`;
- one load-bearing code or data path ⇒ `sy:trace`, at most the resolved `limits.max_depth_agents` cap in flight.

Seed every agent with the anchors already in hand and the ground already covered. Returned briefs are leads: verify the decisive spans before a claim reaches the board.

## Talking to the user

Every turn follows `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`. A discussion turn is a Question turn: the argument goes first as direct text, then one `AskUserQuestion` call carries the frontier (below). Never a question mark in prose.

**Facts are Claude's job, decisions are the user's.** Never ask something code, data, docs or a probe can answer. Look it up or send an agent; only the questions downstream of a running lookup wait.

## 1. Open (one turn)

- Restate the problem in the user's words: who is asking and what they will do with the answer.
- Name the kind of work: root cause, method choice, system design, feasibility, measurement design, or a mix. It decides what counts as an answer: a root-cause question needs causes found, not a design.
- One aim per board; a second aim gets its own. Pick the slug and open the board with a first `board_write`: the lanes and the status note. Its `folder` is where `notes.md` and `probes/` go.
- Suggest, as a single optional aside, `/rename whiteboard <slug>`.

## 2. Theory first, then an early look (bounded)

Write the **theory and prior** section of `notes.md` before reading any data, so the data can contradict it: what the code, the maths or the field already says (name the source), the assumptions each candidate rests on, the plausible approaches including ones nobody raised, and where theory says each one breaks.

Turn it into hypotheses `H1`, `H2`, ... Each names the measurement that tests it and **the result that would disprove it**. A hypothesis no result could disprove is rewritten.

Then look at the problem:

- the repository's own rules and docs first (its `CLAUDE.md` says where they are), and durable memory on the topic per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`;
- the code path behind today's behaviour, if one exists;
- real data: a sample of 20 to 50 real cases, the basic distributions, and a full-population count for anything that could be rare. No synthetic data here;
- what the people asking actually said, in their words.

Record which hypotheses the real data already strains; do not refit them to it. No conclusions, and nothing that costs more than a few minutes of compute: that becomes a planned probe. Keep a **coverage map** in `notes.md` (each area read, or excluded with its reason), so a blind spot is written down rather than read as covered.

**Fast path.** When the look shows only one sensible option, say so with the reason and ask whether to go straight to a decision.

## 3. Write the board, then the idea round

Write `notes.md` and the first board. The glossary is written now, from the terms the code and docs already use, plus one line per data source (id, what it is, which version); use only those terms afterwards.

Then open with breadth and cut it down before the user spends time on it:

1. Put 6 to 10 candidate ideas on the board, each one line naming the hypothesis it rests on. Include one the user has not raised and one that challenges the leading framing.
2. Pressure-test the list with `sy:debate`, unconditionally, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/debate.md`. The decision under debate is "which of these ideas can answer <aim>"; seed it with the board's theory, hypotheses, data and code anchors, never Claude's ranking.
3. An idea leaves only with a concrete reason: an assumption the data or theory breaks, a constraint it violates, a cost the decision cannot carry, or an aim it cannot answer even if it works. Move it to Ruled out with that reason, its evidence tier and what would reopen it.
4. Show the user the three strongest survivors, ranked, each with its strongest point for and against, plus one line on what was cut. The rest stay on the board.

## 4. The discussion (most of the session)

Short turns, opinions, questions back. Each turn:

1. One line of where we are: leading option, strongest open objection.
2. Answer the user's point, and every board move `board_read` returned. Agree, disagree, or name what would settle it.
3. Add one thing: a counterpoint, a failure case, a possibility nobody raised, or the theory or practice that bears on it.
4. One line on what you did not check that bears on it.
5. Close with the frontier.

At most five live options in a turn, ranked. Side issues go to the parked list, not the turn.

**The frontier.** Open decisions form a tree in `notes.md`, each naming the decisions it depends on. The frontier is every open decision whose prerequisites are settled. Close each turn with one `AskUserQuestion` call holding the top of the frontier, at most four questions, each with the recommended answer first, labelled `(Recommended)`, and a one-line why. A question that depends on another still open waits. A "no" with a reason is a challenge to answer on its merits.

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

Write the board when something changes status (an option ruled out or revived, the aim moves, a probe returns, a decision settles), not every turn, and log each change in `notes.md` with its date.

## 5. Probes

The smallest real-data check that separates surviving options or tests the assumption the leading one rests on: minutes to an hour. Each names its hypothesis.

- Write the prediction on the board before running: which option wins, and why.
- Compare against the simplest baseline (today's behaviour, do nothing, the one-parameter version). An alternative that does not beat it by more than the uncertainty has not won.
- Measure both directions: what improves and what gets worse, including the slices, chosen beforehand, where the preferred option should lose.
- Give intervals from the right resampling unit (whatever the cases are correlated over), not from rows.
- Real data from the real source. Synthetic data may stress a mechanism or check parameter recovery, labelled as such, never in place of a real result.
- Agree with the user before anything costs real money or time. A figure is checked through `sy:img-inspector`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`.

When a result misses its prediction, say so first. Then ask, in order, before patching anything: is the problem formulated wrongly, is the data wrong (selection, joins, units, labels), is a model assumption wrong, or is the evaluation design wrong? Log which, and correct the theory section if theory was wrong.

## 6. Converge

The session converges when the frontier is empty and one option survives, or the remaining difference would not change the decision. "The data cannot separate these" is a valid end for a root-cause question when it names the intervention that would.

1. Pressure-test the leading decision with `sy:debate`, unconditionally, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/debate.md`, and answer its strongest objection on the board.
2. Write the Decision: what, why, rejected alternatives with reasons, residual risks, and what result would make us revisit it. Compare the survivors side by side on quality against the baseline (with uncertainty), complexity, maintainability, compute cost and impact on the people using the result.
3. Ask the user to agree or disagree in their own words. Hand nothing off until both of you state the same understanding. A disagreement goes to the probe that decides it, or is recorded as whose call it is, with Claude's objection kept beside the decision.

## Hand-off

Only when the user asks. The Decision section is the brief.

- **Feasibility still in doubt** (a gain nobody has measured at representative scale): `/sy:spike <brief>`.
- **A multi-task objective**: `/sy:plan <brief>`.
- **One PR-sized change**: `/sy:spec <brief>`, or `/sy:spec <task>` with the brief when a task already exists.

Each brief names the opportunity, the evidence, the success bar, the invariants, the failure modes and the open questions. End with the board's folder path and the kickoff line.

Every subagent dispatch resolves its model and effort from config and passes them as the `Agent` invocation's actual `model` and `effort` parameters, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`; a nested dispatch inherits nothing and must resolve again.
