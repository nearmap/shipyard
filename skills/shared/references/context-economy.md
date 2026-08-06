# Context economy

Every agent-facing artifact Shipyard writes is loaded into a finite budget and read by a model that gets worse at using it the fuller it gets. Anthropic's guidance names the resource directly — "LLMs have an 'attention budget' that they draw on when parsing large volumes of context" — and sets the target as finding "the smallest possible set of high-signal tokens that maximize the likelihood of some desired outcome" (https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents). This file is the single copy of that rule for Shipyard's own artifacts: skills, agent briefs, plans, roadmaps, PR descriptions, state briefs, handoff records. Cite it from wherever an artifact gets authored; never restate it there.

The cost is measured, not stylistic. Chroma's *Context Rot* evaluated 18 LLMs across 194,480 calls and found that "even under these minimal conditions, model performance degrades as input length increases" — on tasks trivial enough that length was the only variable (https://www.trychroma.com/research/context-rot). Position compounds volume: performance "is often highest when relevant information occurs at the beginning or end of the input context, and significantly degrades when models must access relevant information in the middle" (Liu et al., https://arxiv.org/abs/2307.03172). And instructions dilute each other — IFScale reports that "even the best frontier models only achieve 68% accuracy at the max density of 500 instructions", with later instructions dropped more often than earlier ones (https://arxiv.org/abs/2507.11538). A sentence added to a brief is not free: it competes with every other sentence there, and it pushes something else toward the middle.

## The two cut tests

Apply both to every paragraph before it ships:

1. **Does removing this sentence change what its reader does?** If nothing downstream changes, it is commentary, and it goes.
2. **Would a pointer do the work this text is doing?** If the content already lives somewhere the reader can reach, cite that instead of copying it.

## Write to the actor

A settled decision is stated, not re-argued. Once the choice is made, the artifact tells its reader what to do; the reasoning that produced it belongs to the human-facing half of the record, or nowhere. Rationale in machine-facing text is the most common form of dilution here precisely because it reads as thoroughness — but an implementer cannot act on *why*, and every line of it displaces a line they could have acted on.

## No cross-part restatement

An artifact with two labeled parts — a human half and an agent half, a sign-off summary and a mechanical plan — carries each fact in exactly one of them. Repetition across that boundary is not redundancy for safety; it is two copies that drift, and a reader of either half cannot tell which one is current. Cross-reference across the boundary instead of copying over it.

## Evidence is not instruction

Forensic detail — the trace that established a fact, the counts, the hypotheses ruled out — is evidence for a claim, not an instruction to anybody. It earns a place inline only where a reader acts on it; otherwise it belongs in a companion record that nothing depends on.

In Shipyard that has a hard edge. A pointer to another tracker comment resolves to nothing for every phase after START, which is the only phase that reads the ticket. A fact a later phase needs is carried in that phase's own brief or it is not available at all — so "it's in the investigation comment" is not a way to keep detail without paying for it. Either the detail earns its place inline, or it goes in a companion comment the plan does not depend on.

## The instance already in the tree

Shipyard applies this to itself: the `## Return contract — target ≤N tokens` block in every `agents/*.md` is this principle made enforceable at the one point where an agent's output enters someone else's budget.
