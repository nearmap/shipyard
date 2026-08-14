# Explicit merge path

Load only after the user directly authorizes merge. The authorization is the informed go-ahead front-loaded in the handoff `## Action needed` block, which named the follow-on mutations: this path will merge the verified head, reply to any review thread that newly surfaces before merge, apply the retrospective's proposed standards-doc edit when it named one via the bounded-fix sub-flow below, attach the scanned transcript when `transcript.attach` resolves true, and set the task done. Execute exactly those and no more; a mutation the consent point did not name is not covered by this authorization, and the three contingent ones execute only when their trigger actually occurs.

## Revalidate

1. reread PR head and required checks;
2. reconcile every review thread that has surfaced since GATE's last pass — human as well as bot, enumerated by author type per `/sy:pr` §3 — through a `/sy:pr` delegate (added to `agents_used`) that drafts and posts the replies. A thread is never left for the owner to answer by hand; the reply mutation is pre-authorized by the handoff consent point and needs no fresh go-ahead. A thread asking for an actual code change is not a reply-only case: that change routes through the bounded-fix sub-flow below, or re-enters GATE when larger, exactly like any other post-authorization finding;
3. verify current head equals `CI_GREEN_SHA` and `REV_REVIEWED_SHA`;
4. fetch and compare the current target branch against recorded `TARGET_SHA`. If the target moved: disjoint, uncoupled drift → proceed and note it in the handoff; overlapping or plausibly coupled drift → refresh CI against the current merge result and open a new immutable review scope when reviewed files interact. Target drift never silently downgrades coverage;
5. verify recorded `REVIEW_BASE_SHA`, requested review model, standalone usage comment, standalone ship-metrics comment, and transcript attachment (full tier);
6. inspect the usage JSON's `by_agent` entry for `sy:gate` and record the transcript-observed gate model in local state/handoff. If the observed model conflicts with the requested model, stop and investigate rather than claiming the requested reviewer ran;
7. if the same ship session is active and substantial post-handoff agent work occurred, regenerate full-tree usage JSON and post it as a new standalone log — `post-log` with `title` `Claude Code usage` and the regenerated object as its `payload` — rather than editing it into another comment;
8. refresh/rescan the transcript attachment when appropriate (full tier). If merge runs in another session, preserve the original ship transcript and record merge execution separately.

Follow the `tracker` skill's attachment flow for the deterministic scan (known-secret scrub, then `gitleaks`), contextual review, redaction, upload, and verification.

## Bounded fix before merge

When revalidation or the authorizing user surfaces one small bounded fix (a typo, a doc nit, a trivial CI repair), it may land without restarting the full cycle through the bounded-fix → focused-delta-gate → merge sub-flow:

1. apply the fix in the recorded build worktree and push;
2. dispatch a focused delta `sy:gate` with `REVIEW_BASE_SHA` = the prior `REV_REVIEWED_SHA` and `REVIEWED_SHA` = the new head — valid only when the prior reviewed head is the immutable base and the new head is the immutable head; a rebase or base change voids the delta and re-enters GATE for full coverage;
3. wait for CI on the new head with the shared poller (`${CLAUDE_PLUGIN_ROOT}/scripts/ci_poll.sh poll <pr> --repo <the PR's base repository, never a fork's own origin> --head <the SHA just pushed>`, run in the background; `--head` is what stops a stale or empty check set reading green), then merge against the new verified head.

The delta gate runs at the same resolved frontier review model and max effort as any other review scope: this sub-flow bounds the scope reviewed, never the reviewer.

## Merge

Resolve the configured strategy — `get_config {"key": "ship.merge_strategy"}` (one of `squash`, `merge`, `rebase`; see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`) — and merge atomically against the verified head:

```bash
gh pr merge <pr> --match-head-commit "$VERIFIED_HEAD_SHA" --<resolved strategy>
```

For a squash merge, compose the subject and body from the PR's own description per `/sy:pr` §4 rather than letting GitHub concatenate every branch commit subject into the message; a merge/rebase strategy has no subject/body to compose and this step does not apply to it.

Then verify merged state, set the task `done` via the `tracker` skill, and remove only build/slice/review worktrees (under the resolved `worktree.root`) and branches recorded by this run.

Close with the end-of-run hygiene assertion: every worktree recorded by this run is gone from `git worktree list`, and no poller from this run is still alive (`pgrep -f "ci_poll.sh poll <this run's PR>"` returns nothing). A leftover is a loud failure to clean and re-assert, never something to close over.

Explicit merge authorization never waives stale CI or review coverage.
