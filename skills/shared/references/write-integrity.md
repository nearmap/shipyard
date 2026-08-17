# Write integrity

Under auto-mode a skill executes mandated external writes — comments, metrics, attachments, status changes, merges — without a human watching each one. Two failure modes then stop being caught by the operator and have to be caught by the instructions instead: a record that was true when posted but is no longer, and a write that was denied but still finds a way through. This reference states both as standing invariants so every writing skill (`/sy:ship`, `/sy:pr`, `/sy:spec`, `/sy:plan`) shares one rule and `sy:gate` can protect them.

**Design invariant (standing; `sy:gate` protects it).** Both rules below hold for every mandated external write, interactive or auto-mode. They are tracker-agnostic: they constrain how a write is corrected or how a denial is honoured, never which tracker or CLI performs it.

## Retroactive honesty

A record already posted to an external surface — a retrospective, a metrics or usage comment, a review reply, a coverage note, a plan or decomposition comment — that is later overruled, superseded, or found wrong is corrected on that same surface, not left standing. The correction is explicit: post a follow-up or edit the record so a reader sees the current truth, and say what changed and why. Silence is the failure. Leaving a stale claim in place because the run has moved on lets the external record drift from what actually happened, which is exactly the drift auto-mode makes invisible. A superseded record is marked superseded; a wrong number is corrected with the right one; a reversed decision is annotated, never quietly abandoned.

This never means rewriting history to look cleaner. The honest move is additive — the original and its correction both remain legible — so the record shows the reversal, not a polished version that hides it.

On the tracker the correction is additive by construction rather than by discipline: a posted comment is not edited, so a superseded record is corrected by the record that supersedes it naming what it replaces, and the superseded one stays posted in full. Emptying an overruled plan down to a two-line stub is the polished version this rule already forbids, not a correction of it.

## Denied-write boundary

When an external write is denied — by the permission system, a ruleset, a missing credential, or an explicit refusal — that denial is final for that write. It is never rerouted through a different tool, path, or credential to force the same effect through. A delegation denied under auto-mode may fall back only to another path that is *itself* an authorized, documented route to the same write (for example, running a documented direct-Bash command inline when the subagent form of the identical, already-permitted operation is unavailable); it may never escalate to a route the denial was expressing a boundary against. If no authorized route remains, the write does not happen: surface the denial loudly — return `blocked` or close with an `## Action needed` block naming exactly what was denied and what would unblock it — rather than papering over it or silently skipping the write. A denied write that is quietly abandoned is as much a violation as one forced through: both hide the boundary from the operator.
