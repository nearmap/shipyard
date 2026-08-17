# Talking to the user

Every skill that talks to you directly — `/sy:plan`, `/sy:spec`, `/sy:ship`, `/sy:spike`, `/sy:pr`, `/sy:explain` — uses exactly one of three modes per turn and never blends them: a decision folded into narration reads as one more sentence and gets missed, which is the failure this reference exists to prevent. Whichever mode a turn is in, it goes through `/sy:tighten` before it is sent. `/sy:ci`, `/sy:standards`, and `/sy:help` never talk to you directly and are out of scope here.

## Status

Narrative prose: what happened, what changed, what's next. Nothing about this turn requires you to act. A status update never ends with a question folded into the prose ("let me know if...", "does this look right?") — if a decision is actually pending, the turn is a Question or an Action needed, and follows one of the two shapes below.

## Question

A choice only you can make, answerable by picking among options. Ask through the `AskUserQuestion` tool, never as a question mark in prose — only the tool actually stops and waits for your answer. One question (or one batched call of up to four) per turn. Frame real candidate options rather than a false binary, worded so that picking "Other" and typing a different answer is always a reasonable path. When a pending decision depends on context not yet stated this turn — a plan's rationale, a set of findings — send that content as direct text first, in full, as part of this same Question turn rather than a separate Status turn blended with it; a call that names "the summary above" or "these findings" with nothing actually sent has not done this.

Use this mode for: a `needs-decision` escalation the parent cannot resolve from plan/standards/code itself, a ship profile below the plan's, choosing a spike's parent Epic, the plan sign-off gate (approve / request changes), confirming a rewrite of an open PR's mutable metadata, `/sy:explain`'s per-layer checkpoint (continue / question / challenge / skip ahead), and any other real fork that research or code cannot settle.

## Action needed

Something outside a multiple-choice pick that only you can do — authorize a merge, resolve external state, run a command. Close the turn with one isolated block, headed `## Action needed`, stating exactly what's needed and why the run is waiting on it. It is the last thing in the turn, never sandwiched inside status prose, at most one per turn.

## Optional suggestions

A non-blocking nicety — suggesting `/rename`, pointing at a follow-up `/sy:plan` brief, offering `/sy:explain` on a finding worth understanding more deeply, or offering to run a ready explainer doc in a new session instead of inline — is not an Action needed. It stays a single low-key aside, explicitly framed as optional, so it never competes with a real gate. If ignoring it changes nothing about whether the run proceeds, it does not earn a block.
