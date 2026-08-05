# Immutable CI and independent review

This phase is a convergence loop owned by a lightweight controller: the frontier reasoning is delegated to `sy:gate` (one verdict per review scope), so the controller itself stays cheap. It owns the recorded build worktree; each iteration delegates the verdict to `sy:gate`, CI triage to `/sy:ci`, automated-review threads to `/sy:pr`, and any non-trivial fix to `sy:slice`, then applies accepted fixes, re-pushes, and re-establishes the scope. It loops until CI, the review verdict, and the automated reviewer's threads converge on one commit, and returns per the worker contract.

## Resolve gate model

Resolve once, live through the resolver:

```
REVIEW_MODEL          = the model `agent_model {"name": "gate"}` reports
REVIEW_MODEL_FALLBACK = the value `get_config {"key": "models.tiers.frontier_fallback"}` reports
```

Before resolving, compare the `fingerprint` that `fingerprint_config {}` reports against the `config_fingerprint` recorded at START. A mismatch means configuration changed mid-run: **stop and report** rather than reviewing at quietly different settings, because a reviewer whose model changed between fix rounds invalidates the comparison this gate exists to make. Re-running `/sy:ship` after an intentional change is the supported path.

Pass `REVIEW_MODEL` as the Agent invocation's **model override**, not merely as prompt text, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`. Record it as `review_model_requested`. The usage transcript later provides `review_model_observed`; do not claim they match until observed. The resolver has already clamped the value up to `gate`'s floor, so a config attempting a weaker reviewer never reaches here.

If a `sy:gate` invocation returns no verdict because the requested model is unavailable — a spend cap, a rate limit, or a `<synthetic>` refusal in place of a review — do not retry the same model and do not read the empty return as a pass. Re-dispatch once at `REVIEW_MODEL_FALLBACK`, set `review_model_observed` to the model that actually ran, and note the substitution in the coverage comment. If the fallback also cannot run, return `blocked` (review model unavailable) with the pinned SHAs. A model-unavailability failure must never silently promote or bounce the verdict up to the dispatcher.

## Pin scope

After push:

```text
REVIEW_BASE_SHA=<immutable merge-base/base SHA>
REVIEWED_SHA=<current PR head SHA>
TARGET_SHA=<origin/<target branch> SHA at pin time>
```

Create a detached review worktree, under the resolved worktree root (`get_config {"key": "worktree.root"}`; defaults to the sibling directory beside the repo), pinned to `REVIEWED_SHA`. Invoke `sy:gate` there with purpose, acceptance criteria, exact SHAs, standards authority, risk lenses, verification obligations, and the compact design contract (plan invariants plus `accepted_deviations` from state). Add `sy:gate` to `agents_used`. Gate verifies HEAD before reviewing.

CI may run concurrently. Separate waiting from triage. Never poll `gh pr checks` or `gh run watch` once per reasoning turn, and never let a monitor self-resume at a turn-budget boundary — on a large matrix that bleeds tokens and the phase never returns. Wait with the single shared token-free background poller — launch `${CLAUDE_PLUGIN_ROOT}/scripts/ci_poll.sh poll <pr>` with `run_in_background`; it sleeps between checks and exits when nothing is pending, spending no reasoning turns while it waits, and no phase hand-writes its own poller; only once CI is terminal, delegate the diagnosis to a `/sy:ci` subagent (added to `agents_used`) that returns a compact result rather than tailing raw logs. If CI cannot reach a terminal state within a sane bound (`ci.poll_timeout`, default 1800s — raise it for repos/matrices known to run long so one poll call spans the wait), return `blocked` (CI pending) with an idempotent checkpoint and the pending run id rather than looping. Never apply fixes to the review checkout. If code changes, finish/cancel stale review, fix in build worktree, push, and create a new immutable review scope.

Persist:

```text
HEAD_SHA=<current PR head>
CI_GREEN_SHA=<successful CI SHA>
REVIEW_BASE_SHA=<reviewed base>
REV_REVIEWED_SHA=<reviewed head>
TARGET_SHA=<target branch SHA at review pin>
REVIEW_MODEL_REQUESTED=<Agent model override>
```

Post compact PR review coverage:

```text
Review coverage
REVIEW_BASE_SHA: <sha>
REVIEWED_SHA: <sha>
REVIEW_MODEL_REQUESTED: <model>
REVIEW_EFFORT: max
```

Acceptance evidence gets its own PR comment, never only the mutable description.

## Fix cycle

`sy:gate` reports; the GATE worker triages findings — applying accepted fixes in the build worktree and pushing, and recording rejections with reasoning. A finding whose resolution is genuinely ambiguous returns `needs-decision` (checkpoint: resolved vs pending findings and current pushed SHA); a finding exposing a plan-contract problem returns `bail-to-spec`. Every new commit invalidates CI/review coverage.

A finding outside the plan's declared scope is not automatically a follow-up: when it is small, adjacent, and low-risk, fold the fix into the build worktree as a recorded scope extension (added to `accepted_deviations`) rather than filing a follow-up, and batch such fold-ins into the current fix round so re-review stays one delta; defer only when the fix justifies its own ticket. See `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/scope-discipline.md`.

When `ship.request_ci_reviewer` resolves true (`/sy:pr` §3; see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`), request the automated reviewer (e.g. Copilot) — it does not comment on a draft, so marking the PR ready (`gh pr ready`, the promotion step below) is the precondition, not the trigger. The review must then be explicitly requested through `/sy:pr` §3's reviewer-request ladder, after which it comments within minutes. When it resolves false, skip the request; human review threads still get reconciled the same way. Reconcile whatever threads exist through a `/sy:pr` delegate (added to `agents_used`) that returns the threads compactly: evaluate each critically, fix the real ones in the build worktree, push back with reasoning on the rest, and reply to every thread. `/sy:pr` §3 enumerates the reviewer's threads by author bot-type rather than a hardcoded bot login (the login form differs across GitHub's REST and GraphQL surfaces, so a single-login filter yields a false "0 new comments"); trust its reconcile over an empty single-login query. Those fixes re-establish the scope like any other; the verbose comment bodies stay out of the controller.

**Narrow-fix reviewer cadence.** Not every fix round needs a fresh automated-reviewer request. When a round's entire diff is confined to resolving thread(s) the automated reviewer already flagged — no lines outside what those threads flagged, no new file, no behaviour beyond the fix itself — `sy:gate`'s own focused delta review (still frontier model, max effort, the reviewer of record for correctness) is sufficient for that round: skip the fresh request, but still reply to and resolve the originating thread(s) through `/sy:pr` as usual, provided the head that ships differs from the automated reviewer's last-reviewed head by nothing but such fixes — otherwise request again. The moment a round's diff touches anything beyond what the automated reviewer's threads have flagged, request it again — and request it once more before the loop converges (and before merge) whenever its last review differs from the current head by anything beyond such fixes. This changes request cadence only, never coverage: the completion bar's own requirements (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` — the automated reviewer explicitly requested with the request confirmed, and every review thread reconciled and answered) are unchanged.

**Stopping rule.** The loop converges only when a pass over the current head surfaces no undispositioned actionable finding: every finding is dispositioned — accepted and fixed, or rejected with recorded reasoning — and what remains is at most already-dispositioned pre-existing nits. The loop never converges, promotes, or returns `done` while an undispositioned actionable finding stands (a checkpointed `blocked` return parks the loop without dispositioning them).

**Trivial-diff cost path.** When a fix round's delta is trivial — docs-only, comment-only, or `__all__`-only — the loop may skip a redundant full re-review in favour of the focused delta review below and shorten the automated-reviewer wait before reconciling. Cost comes out of the loop, never the reviewer: `sy:gate` still runs at the resolved frontier review model and max effort on whatever scope it reviews, coverage of the final head is never waived, and no trivial-diff path may lower gate model, effort, or coverage — cost-scaling may only raise them.

**Drift re-check.** A long convergence loop re-checks the target branch rather than trusting the START snapshot: at each new review scope, and at least once per fix round, fetch and compare the current target/integration branch head against the recorded `TARGET_SHA`. Disjoint, uncoupled drift is noted; overlapping or plausibly coupled drift refreshes CI against the current merge result and opens a new immutable review scope, exactly as at merge revalidation.

A focused delta review is valid only when prior reviewed head is the immutable base and new head is immutable head. Rebase or base change requires full appropriate coverage.

When current HEAD equals CI-green SHA and reviewed SHA, gate findings are resolved, every existing review thread — automated reviewer (when requested) and human — is addressed, and acceptance evidence is posted, promote through `/sy:pr` and set the Task's status to `in-review` via the `tracker` skill. Later human or automated review that changes code re-establishes both gates on resume.

Return `done` with coverage SHAs and `agents_used`; the parent dispatches HANDOFF.
