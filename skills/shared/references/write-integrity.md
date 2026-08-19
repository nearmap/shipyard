# Write integrity

Under auto-mode a skill executes mandated external writes — comments, metrics, attachments, status changes, merges — without a human watching each one. Two failure modes then have no operator to catch them: a record that was true when posted but is no longer, and a write that was denied but still finds a way through.

**Design invariant (standing; `sy:gate` protects it).** Both rules below hold for every mandated external write, interactive or auto-mode, in every writing skill (`/sy:ship`, `/sy:pr`, `/sy:spec`, `/sy:plan`). They are tracker-agnostic: they constrain how a write is corrected or how a denial is honoured, never which tracker or CLI performs it.

## Retroactive honesty

A record already posted to an external surface — a retrospective, a metrics or usage comment, a review reply, a coverage note, a plan or decomposition comment — that is later overruled, superseded, or found wrong is corrected on that same surface, not left standing. The correction is explicit and additive: post the record that carries the current truth, saying what it corrects and why. Silence is the failure: nothing is quietly abandoned because the run has moved on.

This never means rewriting history to look cleaner: the original and its correction both remain legible, so the record shows the reversal rather than a polished version hiding it.

On the tracker the correction is additive by construction rather than by discipline: a posted comment is not edited, so a superseded record is corrected by the record that supersedes it naming what it replaces, and the superseded one stays posted in full. For a plan or decomposition comment that correcting record is a new highest version — nothing is marked superseded in place, and no reader consults such a mark (`${CLAUDE_PLUGIN_ROOT}/skills/tracker/CONTRACT.md`). Emptying an overruled plan down to a two-line stub is the polished version this rule already forbids, not a correction of it.

## Denied-write boundary

When an external write is denied — by the permission system, a ruleset, a missing credential, or an explicit refusal — that denial is final for that write. It is never rerouted through a different tool, path, or credential to force the same effect through. A delegation denied under auto-mode may fall back only to another path that is *itself* an authorized, documented route to the same write (running a documented direct-Bash command inline when the identical, already-permitted subagent form is unavailable); it may never escalate to a route the denial was expressing a boundary against. If no authorized route remains, the write does not happen: surface the denial loudly — return `blocked` or close with an `## Action needed` block naming exactly what was denied and what would unblock it. A denied write that is quietly abandoned is as much a violation as one forced through.
