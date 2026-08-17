# Fixing in-branch vs. filing a follow-up

When review or build surfaces an issue outside the current plan's declared scope, the default is to fix it in the current branch, not to file a follow-up. The plan's declared scope is a default contract, not an inviolable wall: a small, adjacent, low-risk fix rides along with the change that surfaced it. The follow-up must justify itself against the in-branch fix, not the other way around.

## Fix it in-branch when

The fix is small (a bounded diff a reviewer can still hold alongside the main change), adjacent or low-coupling (near the current change, or a direct consequence of it), and low-risk (no new design decision, no unrelated behaviour change, no material new test surface). If you have the context now and would have to rebuild it later, that alone weighs toward fixing now. Record every such fold-in as a one-line scope extension in `accepted_deviations` — what changed, and why it was cheaper than a follow-up. A recorded scope extension is treated like an accepted deviation by review: it is not a scope-creep finding unless it breaks an invariant, obligation, or standard.

## File a follow-up when

It genuinely justifies itself: the fix is large or needs its own design/spec; it is genuinely independent, so filing it cold loses no context; it is risky or sits in an unfamiliar subsystem; folding it in would balloon the PR's reviewable surface past what one reviewer can hold in a sitting; or there is a real merge-ordering or ownership reason to keep it separate. When torn between one more small fold-in and a clean follow-up, prefer the fold-in — but once the PR would tell two stories instead of one, it is a follow-up.

## Keep re-review cheap

Under immutable review every new commit re-establishes the review scope, so batch fold-ins into the current fix round rather than trickling one per round — five small fixes in one commit cost one delta review; five commits cost five. Always name what was folded in or deferred: a silent deferral and an unrecorded fold-in are the same failure — scope drift the gate cannot see.
