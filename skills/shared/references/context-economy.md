# Context economy

Every agent-facing artifact Shipyard writes is loaded into a finite budget and read by a model that gets worse at using it the fuller it gets. Anthropic's guidance names the resource directly — "LLMs have an 'attention budget' that they draw on when parsing large volumes of context" — and sets the target as finding "the smallest possible set of high-signal tokens that maximize the likelihood of some desired outcome" (https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents). This file is the single copy of that rule for Shipyard's own artifacts: skills, agent briefs, plans, roadmaps, PR descriptions, state briefs, handoff records. Cite it from wherever an artifact gets authored; never restate it there.

The cost is measured, not stylistic. Chroma's *Context Rot* found performance degrading with input length across 18 LLMs and 194,480 calls, on tasks trivial enough that length was the only variable (https://www.trychroma.com/research/context-rot). Position compounds volume: accuracy is highest at the beginning and end of a context and degrades significantly in the middle, even for explicitly long-context models (Liu et al., https://arxiv.org/abs/2307.03172). And instructions dilute each other: IFScale measures the best frontier models at 68% accuracy across 500 instructions, dropping later ones more often than earlier (https://arxiv.org/abs/2507.11538). A sentence added to a brief is not free: it competes with every other sentence there, and it pushes something else toward the middle.

## The two cut tests

Apply both to every paragraph before it ships:

1. **Does removing this sentence change what its reader does?** If nothing downstream changes, it is commentary, and it goes.
2. **Would a pointer do the work this text is doing?** If the content already lives somewhere the reader can reach, cite that instead of copying it.

## Write to the actor

A settled decision is stated, not re-argued. Once the choice is made, the artifact tells its reader what to do; the reasoning that produced it belongs to the human-facing half of the record, or nowhere. Rationale in machine-facing text is the most common form of dilution here.

## No restatement, across the parts or within one

An artifact with two labeled parts — a human half and an agent half, a sign-off summary and a mechanical plan — carries each fact in exactly one of them. Repetition across that boundary is not redundancy for safety; it is two copies that drift, and a reader of either half cannot tell which one is current. Cross-reference across the boundary instead of copying over it.

The rule does not stop at that boundary; it holds *within* a half as well. A fact stated in one section of an agent half and again in another is the same pair of drifting copies, minus the labeled seam that made the duplication easy to see. State it once, in the section whose reader acts on it, and point at that section from anywhere else that needs it.

## Evidence is not instruction

Forensic detail — the trace that established a fact, the counts, the hypotheses ruled out — is evidence for a claim, not an instruction to anybody. It earns a place inline only where a reader acts on it; otherwise it belongs in a companion record that nothing depends on.

In Shipyard that has a hard edge. Exactly one tracker comment is carried forward, and only half of it: the `/sy:ship` parent materialises the highest plan version's own `## For /sy:ship` half to a file once per session and hands later phases that file's path (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § State router), and no `/sy:ship` phase reads the ticket itself. Every *other* comment still resolves to nothing for every phase — so a fact a later phase needs is in the plan's ship half, or in that phase's own dispatch brief, or it is not available at all, and "it's in the investigation comment" is not a way to keep detail without paying for it.

## The instance already in the tree

The `## Return contract — target ≤N tokens` block in every `agents/*.md` is this principle made enforceable at the one point where an agent's output enters someone else's budget.
