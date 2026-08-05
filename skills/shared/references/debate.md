# Proposer/adversary debate

A roadmap, plan, or spike verdict can look settled to the session that wrote it and still rest on a weak core decision. `sy:debate` is the standing check against that: it runs a bounded proposer/adversary exchange over the decision and returns only the synthesized disagreement — never the raw rounds — for the user to steer (see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`, Question mode). Like `sy:sweep`, `sy:seam`, and `sy:img-inspector`, the raw work stays inside the delegate; only a compressed lead comes back.

## When to run it

Every `/sy:plan` roadmap, every `/sy:spec` plan, and every `/sy:spike` verdict runs at least one `sy:debate` pass over its core design decision before it reaches sign-off and becomes durable — unconditionally, not gated on whether the decision already presents as a genuine two-sided fork. The author's own judgment that the call is settled is exactly what this catches: under the old conditional bar, the pass ran only when a session already suspected it was wrong, which is when it was least needed. There is no size or reversibility floor below which the pass is skipped.

Run it after the normal research pass (sweep/seam/trace as applicable), so the debate argues over gathered evidence rather than speculation. A fork that surfaced mid-research and was already debated needs no second pass at sign-off — one recorded pass over the core decision is the bar, not one per decision. A call that turns only on something the user can weigh still ends in a `Question` — the pass still runs, and the debate sharpens that question rather than replacing it.

## Dispatch it

```
DEBATE_MODEL = the model `agent_model {"name": "debate"}` reports
```

Dispatch `sy:debate` once, foreground, with the explicit `model` override `DEBATE_MODEL` (an `Agent` call does not inherit the parent's model — see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`) plus, in the prompt itself:

- the decision under debate — the core premise or approach — as a single neutral sentence, not pre-weighted toward either side;
- the seed evidence already gathered — paths, findings, anchors — so it isn't rediscovered;
- the literal `DEBATER_MODEL` string — the model `agent_model {"name": "debater"}` reports — for `sy:debate` to forward to each of its own nested dispatches, which inherit nothing.

Raise `DEBATE_MODEL` (e.g. to the frontier tier, `get_config {"key": "models.tiers.frontier"}`) for a fork whose blast radius justifies the extra cost; the default floor is opus because this is a judgment task, not a lookup.

## Hand it back

`sy:debate` returns `AGREE` / `CONTESTED` / `READ` (see `${CLAUDE_PLUGIN_ROOT}/agents/debate.md`). Present it as a status update, then close with a single `AskUserQuestion` naming the real candidate options from `CONTESTED` — never a summary that quietly picks a winner; this is a `Question`-mode fork per `user-interaction.md`, not a status update with a question folded in. Record the outcome durably on whichever surface the caller already writes decisions to: `/sy:plan` folds it into the roadmap decision-log delta; `/sy:spec` folds the adversary's strongest objection into the plan's "strongest rejected alternative and why" and any resulting risk into "risks/edge cases"; `/sy:spike` folds it into the verdict comment, alongside the strongest failure cases. The debate transcript itself is never attached — only `sy:debate`'s synthesis and the user's steer are durable.
