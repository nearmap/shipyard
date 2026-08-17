# Immutable CI and independent review

This phase is a convergence loop owned by a lightweight controller: the frontier reasoning is delegated to `sy:gate` (one verdict per review scope), so the controller itself stays cheap. It owns the recorded build worktree; each iteration delegates the verdict to `sy:gate`, CI triage to `/sy:ci`, automated-review threads to `/sy:pr`, and any non-trivial fix to `sy:slice`, then applies accepted fixes, re-pushes, and re-establishes the scope. It converges per § Stopping rule and returns per the worker contract.

## Resolve gate model

Resolve once, live through the resolver (tool names resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`):

```
REVIEW_MODEL          = the model `agent_model {"name": "gate"}` reports
REVIEW_MODEL_FALLBACK = the value `get_config {"key": "models.tiers.frontier_fallback"}` reports
```

Before resolving, compare the `fingerprint` that `fingerprint_config {}` reports against the `config_fingerprint` recorded at START. A mismatch means configuration changed mid-run: **stop and report** rather than reviewing at quietly different settings. Re-running `/sy:ship` after an intentional change is the supported path.

Pass `REVIEW_MODEL` as the Agent invocation's **model override** and record/reconcile it as `review_model_requested`/`review_model_observed` per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`. The resolver has already clamped the value up to `gate`'s floor, so a config attempting a weaker reviewer never reaches here.

If a `sy:gate` invocation returns no verdict because the requested model is unavailable, re-dispatch once at `REVIEW_MODEL_FALLBACK` per model-dispatch.md's "Unavailability falls back once", set `review_model_observed` to the model that actually ran, and note the substitution in the coverage comment. If the fallback also cannot run, return `blocked` (review model unavailable) with the pinned SHAs. A model-unavailability failure must never silently promote or bounce the verdict up to the dispatcher.

## Pin scope

After push:

```text
REVIEW_BASE_SHA=<immutable merge-base/base SHA>
REVIEWED_SHA=<current PR head SHA>
TARGET_SHA=<origin/<target branch> SHA at pin time>
```

Create a detached review worktree, under the resolved worktree root (`get_config {"key": "worktree.root"}`; defaults to the sibling directory beside the repo), pinned to `REVIEWED_SHA`. Invoke `sy:gate` there with purpose, exact SHAs, and the compact design contract (plan invariants plus `accepted_deviations` from state), composing the acceptance criteria, standards authority, risk lenses and verification obligations out of the plan file the state brief names — and passing that file's absolute path along with the brief rather than this phase's paraphrase of it. That costs no tracker verb and `hooks/review_guard.py` does not block it: the plan reached disk before GATE was dispatched (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § State router), and the guard denies mutation inside the review worktree rather than reads outside it. Add `sy:gate` to `agents_used`. Gate verifies HEAD before reviewing.

CI may run concurrently. Separate waiting from triage. Never poll `gh pr checks` or `gh run watch` once per reasoning turn, and never let a monitor self-resume at a turn-budget boundary — on a large matrix that bleeds tokens and the phase never returns. Wait with the single shared token-free background poller — launch `${CLAUDE_PLUGIN_ROOT}/scripts/ci_poll.sh poll <pr> --repo <the PR's base repository, never a fork's own origin> --head <the SHA just pushed>` with `run_in_background`; `--head` is what stops a stale or empty check set reading green, and `--allow-no-checks` is declared only for a target repo already known to have no CI; no phase hand-writes its own poller. Only once CI is terminal, delegate the diagnosis to a `/sy:ci` subagent (added to `agents_used`) that returns a compact result rather than tailing raw logs. If CI cannot reach a terminal state within a sane bound (`ci.poll_timeout`, default 1800s — raise it for repos/matrices known to run long so one poll call spans the wait), return `blocked` (CI pending) with an idempotent checkpoint and the pending run id rather than looping. Never apply fixes to the review checkout. If code changes, finish/cancel stale review, fix in build worktree, push, and create a new immutable review scope.

Persist:

```text
HEAD_SHA=<current PR head>
CI_GREEN_SHA=<successful CI SHA>
REVIEW_BASE_SHA=<reviewed base>
REV_REVIEWED_SHA=<reviewed head>
TARGET_SHA=<target branch SHA at review pin>
REVIEW_MODEL_REQUESTED=<Agent model override>
```

Post compact PR review coverage, collapsed by default. Plain HTML, and the layout is load-bearing: each tag on its own line with a blank line around it and around the nested fence, never inline — that blank line after `</summary>` is what makes the enclosed block parse as Markdown instead of one raw-HTML run.

````
<details>

<summary>Review coverage</summary>

```text
REVIEW_BASE_SHA: <sha>
REVIEWED_SHA: <sha>
REVIEW_MODEL_REQUESTED: <model>
REVIEW_EFFORT: max
```

</details>
````

Acceptance evidence gets its own PR comment, never only the mutable description.

## Net-new agent-facing text

Each addition to Shipyard's own always-resident agent-facing prose justifies itself here —
reviewer-initiated, builder-answerable, no new artifact and no new return value.

- **Scope** — net-new agent-facing text in the diff: `skills/**/*.md`, `agents/*.md`, and MCP tool
  docstrings and `Field(description=...)` in `sy_tools/server.py`.
- **Carrier** — the PR body, one line per addition. Nowhere else: a justification in a commit message
  or a tracker comment does not discharge this.
- **Keep/cut test** — text stays when its reader must act on it *before* reaching the thing it
  describes and no runtime signal teaches it: an irreversible effect, a caller-declared trust boundary
  where misuse succeeds, routing between two surfaces, mutually-exclusive options. Otherwise it goes.
  Apply the two cut tests in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/context-economy.md` by
  citation; never restate them.
- **Refutation test** — a justification survives only if removing the text would change what its
  reader does. "It adds useful background" is refuted by default. Use the refute mode `sy:hunt`
  already has. Three shapes read as cuttable and are not: a defence that stops a later reader
  simplifying a check into a no-op, a duplication `scripts/validate.py` itself requires in two places,
  and a documented carve-out.
- **Outcome** — an addition whose justification is refuted is an actionable finding in the fix cycle
  below. The builder cuts it or re-justifies there; no extra round budget, and no new `sy:gate` return
  field.

## Fix cycle

`sy:gate` reports; the GATE worker triages findings — applying accepted fixes in the build worktree and pushing, and recording rejections with reasoning. A finding whose resolution is genuinely ambiguous returns `needs-decision` (checkpoint: resolved vs pending findings and current pushed SHA); a finding exposing a plan-contract problem returns `bail-to-spec` — including a finding that restates a root cause an earlier round in this same session already treated as fixed, which is the plan's own shape being wrong rather than one more fix to attempt. A resumed continuation worker sees only the checkpoint and cannot recognize that pattern; the round bound below is what catches the same loop from outside. Every new commit invalidates CI/review coverage.

**Round bound.** Every pass increments `gate_rounds_total` in `ship-state.yaml` — a missing field reads as `0`, and a trivial-diff-path pass counts like any other. The count is for the run: it is never reset merely because a fix round pushes a new head, and only a raise-budget disposition moves the floor under it, by stamping `gate_rounds_budget_base` (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Worker contract). Before starting another round, compare `gate_rounds_total - gate_rounds_budget_base` (a missing base reads as `0`) against the resolved `ship.escalation.max_gate_rounds` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`); a count that has reached the cap is a breach — the cap-th round since the last raise-budget stamp is the last one allowed — and returns `needs-decision` tagged `reason: max_gate_rounds` rather than continuing, with a checkpoint carrying the round count, the dispositioned and still-pending findings, and the current pushed SHA. Those two `ship-state.yaml` fields are the sole authority: a checkpoint's round count is descriptive only, so a continuation re-reads the live fields rather than trusting a number that may predate a budget raise. This is the one `needs-decision` the parent never resolves itself (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Worker contract).

A finding outside the plan's declared scope is not automatically a follow-up: when it is small, adjacent, and low-risk, fold the fix into the build worktree as a recorded scope extension (added to `accepted_deviations`) rather than filing a follow-up, and batch such fold-ins into the current fix round so re-review stays one delta; defer only when the fix justifies its own ticket. See `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/scope-discipline.md`.

A finding — or any direct observation in this phase — that contradicts a seeded memory anchor is authored as a `MEMORY_REFUTE` candidate in the return block and recorded to `memory_refutations` in state, never carried forward as if the anchor still held and never left to HANDOFF; the parent holds the write and applies it the moment this phase returns (see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`).

When `ship.request_ci_reviewer` resolves true (`/sy:pr` §3; see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`), request the automated reviewer (e.g. Copilot) — it does not comment on a draft, so marking the PR ready (`gh pr ready`, the promotion step below) is the precondition, not the trigger. The review must then be explicitly requested through `/sy:pr` §3's reviewer-request ladder. When it resolves false, skip the request; human review threads still get reconciled the same way. Reconcile whatever threads exist through a `/sy:pr` delegate (added to `agents_used`) that returns the threads compactly: evaluate each critically, fix the real ones in the build worktree, push back with reasoning on the rest, and reply to every thread. `/sy:pr` §3 enumerates the reviewer's threads by author bot-type rather than a hardcoded bot login (the login form differs across GitHub's REST and GraphQL surfaces, so a single-login filter yields a false "0 new comments"); trust its reconcile over an empty single-login query. Those fixes re-establish the scope like any other; the verbose comment bodies stay out of the controller.

**Narrow-fix reviewer cadence.** When a round's entire diff is confined to resolving thread(s) the automated reviewer already flagged — no lines outside what those threads flagged, no new file, no behaviour beyond the fix itself — `sy:gate`'s own focused delta review (still frontier model, max effort, the reviewer of record for correctness) is sufficient for that round: skip the fresh request, but still reply to and resolve the originating thread(s) through `/sy:pr` as usual. Request again the moment the current head differs from the automated reviewer's last-reviewed head by anything beyond such fixes — and check that same condition once more before the loop converges and before merge. This changes request cadence only, never coverage: the completion bar's own requirements (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md`) are unchanged.

**Stopping rule.** The loop converges only when a pass over the current head surfaces no undispositioned actionable finding: every finding is dispositioned — accepted and fixed, or rejected with recorded reasoning — and what remains is at most already-dispositioned pre-existing nits. The loop never converges, promotes, or returns `done` while an undispositioned actionable finding stands (a checkpointed `blocked` return, or a `max_gate_rounds` breach, parks the loop without dispositioning them).

**Trivial-diff cost path.** When a fix round's delta is trivial — docs-only, comment-only, or `__all__`-only — the loop may skip a redundant full re-review in favour of the focused delta review below and shorten the automated-reviewer wait before reconciling. Cost comes out of the loop, never the reviewer: `sy:gate` still runs at the resolved frontier review model and max effort on whatever scope it reviews, coverage of the final head is never waived, and no trivial-diff path may lower those three — cost-scaling may only raise them.

**Drift re-check.** A long convergence loop re-checks the target branch rather than trusting the START snapshot: at each new review scope, and at least once per fix round, fetch and compare the current target/integration branch head against the recorded `TARGET_SHA`. Disjoint, uncoupled drift is noted; overlapping or plausibly coupled drift refreshes CI against the current merge result and opens a new immutable review scope, exactly as at merge revalidation.

A focused delta review is valid only when prior reviewed head is the immutable base and new head is immutable head. Rebase or base change requires full appropriate coverage.

When current HEAD equals CI-green SHA and reviewed SHA, gate findings are resolved, every existing review thread — automated reviewer (when requested) and human — is addressed, and acceptance evidence is posted, promote through `/sy:pr` and set the Task's status to `in-review` via the `tracker` skill. The tracker skill's own `validate_config`/`preflight` preamble is already discharged for this run by the parent's pre-dispatch tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Invariants), so call `set-status` directly rather than re-running that preamble — that one verb is the whole of this worker's granted tracker access. Later human or automated review that changes code re-establishes both gates on resume.

Return `done` with coverage SHAs and `agents_used`; the parent dispatches HANDOFF.
