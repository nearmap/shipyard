# Retrospective, token accounting, transcript, and handoff

This phase runs mostly as a worker for the records and accounting. The readable transcript is rendered from the on-disk session tree by a delegate, so no manual `/export` is ever run.

Create the durable records for the plan's process tier, each as its own tracker comment, never combined: `full` = all four below; `light` = records 1–3 only, with `transcript_attachment: null` in the metrics JSON. The tier never changes CI/review coverage.

## Doc-accuracy self-check (before the retro)

Distinct from BUILD's leaked-token content-QA grep (`${CLAUDE_PLUGIN_ROOT}/skills/ship/references/implementation.md`), which only proves nothing leaked: re-verify the load-bearing factual claims in the shipped documentation diff — version numbers, changelog citations, dated facts, claims about how a tool, system, or team actually behaves — against their primary source, not against the diff's own internal consistency. This is a repeatable catch category rather than a one-off: `sy:gate` has already caught exactly this shape once, a cited changelog version naming the wrong release. Record the check in `.scratch/` as claim → source consulted → outcome; a claim that cannot be verified is corrected or explicitly flagged, never shipped silently. Fold the outcome into the retrospective's prose.

## 1. Human retrospective comment

Post `# Ship retrospective` as clear prose:

- shipped vs plan;
- divergences and mid-ship decisions — accepted deviations and any parent-resolved `needs-decision` — and why;
- what the plan missed;
- lessons for next `/sy:plan`;
- a concrete proposed edit to the repo's standards doc — whatever `/sy:standards resolve` names as authority — when this run surfaced a new team-process decision, or "none" otherwise; a proposal lands through the bounded-fix → focused-delta-gate → merge sub-flow in `${CLAUDE_PLUGIN_ROOT}/skills/ship/references/merge-accounting.md` like any other finding, never special-cased as "just docs";
- follow-ups;
- PR URL and gate coverage.

Do **not** embed token or metrics JSON in this comment.

While writing the retro, distill any durable, cross-cutting, tool/skill-level lesson (not repo trivia) into cross-session memory — `python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_memory.py" add ...` per the write bar in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`. The retro records what happened here; the memory write is what `/sy:plan`, `/sy:spec`, and `/sy:ship` START read back in unrelated future sessions.

## 2. Standalone token-usage JSON comment

Token accounting must include the parent ship session and every nested subagent transcript (`sy:slice`, `sy:gate`, nested `sy:hunt`, `sy:sweep`, fallbacks, etc.). Claude stores subagent transcripts separately, so do not derive totals from parent export text alone.

Generate the report from the full transcript tree. This example assumes `sy:slice` was used; repeat/remove `--require-agent` flags to match local `agents_used`:

```bash
python ${CLAUDE_PLUGIN_ROOT}/scripts/session_usage.py summarize \
  --session-id "$SHIP_SESSION_ID" \
  --phase ship \
  --task "$TASK_KEY" \
  --require-agent gate \
  --require-agent slice \
  --output .scratch/claude-usage-$TASK_KEY.json
```

Inspect the JSON before posting:

- `scope` must be `main_plus_subagents`;
- `transcripts.subagents` must be consistent with the agents actually used;
- run with one `--require-agent` per directly used agent in `agents_used`; the command must fail if any is absent;
- `by_agent` must include nested agents when present;
- totals must not be manually reconstructed or inferred.

Post one small tracker comment containing only:

````text
# Claude Code usage

```json
<contents of .scratch/claude-usage-$TASK_KEY.json>
```
````

Post via the `tracker` skill (it renders Markdown to the tracker's native format). This usage log is standalone; never append it to the retrospective, execution plan, or decomposition comment.

## 3. Standalone ship-metrics JSON comment

Post a second small comment containing only a JSON object under `# Claude Code ship metrics`:

```json
{
  "schema": "shipyard.ship_metrics.v1",
  "task": "TASK-123",
  "pr_url": "...",
  "plan_divergence_count": 0,
  "ci_fix_rounds": 0,
  "review_findings_accepted": 0,
  "review_findings_rejected": 0,
  "human_review_defects": 0,
  "gate_false_pass": null,
  "post_merge_defect": null,
  "rollback": null,
  "lead_time_seconds": null,
  "transcript_attachment": "ship-session-....txt"
}
```

Use `null` for unknown values. Never infer metrics.

`gate_false_pass` is unknowable at ship time: always post it as `null`, then set it post-hoc — correcting the same comment per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md` — when a human or CI later finds a defect the gate passed. It is the shadow-run signal for whether the gate can be trusted without its human backstop; the backstop is retained until that record says otherwise.

## 4. Transcript attachment (full tier only)

A HANDOFF delegate (subagent, added to `agents_used`) renders and uploads the transcript straight from the on-disk session tree, so nothing session-bound and no by-hand `/export` is involved. It renders the whole tree — main plus every nested subagent — into one readable file:

```bash
python ${CLAUDE_PLUGIN_ROOT}/scripts/session_usage.py export \
  --session-id "$SHIP_SESSION_ID" \
  --task "$TASK_KEY" \
  --output .scratch/$TASK_KEY-ship-transcript.txt
```

The renderer truncates bulky tool output and strips raw-JSONL noise, so the result is an audit-readable transcript, not a machine dump; token accounting still comes from `session_usage.py summarize`. The delegate then runs the deterministic scan (known-secret scrub, then `gitleaks`), contextual review, and redaction, uploads exactly one attachment, and verifies it, per `${CLAUDE_PLUGIN_ROOT}/skills/ship/references/merge-accounting.md` and the `tracker` skill's attachment flow. Run it as late as possible (after an authorized merge) so the captured tail is maximal; because it reads on-disk transcripts it can also run on a resumed session. The rendered transcript is never read back into the ship context.

Subagent delegation is the primary path. When it is denied under auto-mode — the delegation itself is refused rather than the attachment — the identical operation may run inline as an explicit permitted fallback: execute the same `session_usage.py export` above, then the deterministic scan and redaction, then upload the single attachment through the `tracker` skill's attachment flow, all directly in the caller. This is the authorized-alternate-route case of the denied-write boundary in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`: it is the same documented render-and-attach, not a reroute to force a write the operator blocked. The invariant is unchanged — the rendered transcript file is written, scanned, and uploaded by path only, and its text is never read back into the caller context. The fallback's scan is deterministic-only — it drops the contextual-review step of the primary path, since reading the transcript to review it would defeat the isolation invariant it exists to preserve — so treat a clean result here as evidence, not proof, per the `tracker` skill's attachment flow. If neither path can complete, surface it loudly (do not silently skip the attachment) per the same boundary.

## Handoff

Task stays `in-review` until merge. Before reporting, run this phase's end-of-run hygiene assertion: no poller from this run is still alive (`pgrep -f "ci_poll.sh poll <this run's PR>"` returns nothing), and this run's recorded worktrees all exist while nothing this run created is unrecorded (recorded build/review worktrees — under the worktree root `${SY_WORKTREE_ROOT:-<repo>-worktrees}` — remain until an authorized merge cleans them; the primary checkout and any sibling run's worktrees are out of scope; a mismatch in this run's set is drift to fix loudly, not to report around). Report PR URL, tracker status, acceptance state, coverage SHAs/requested+observed gate, start, and build models, usage/metrics comment status, transcript attachment status, and owned-worktree/hygiene status as a status update, then close the turn with an isolated `## Action needed` block (per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`) stating the PR is ready and merge awaits your explicit authorization — never let that wait get lost among the status facts above it. Front-load the follow-on mutations in that same block so consent is informed and no later write is a surprise: name that on your go-ahead the run will merge the verified head, reply to any review thread that newly surfaces before merge (drafted and posted for you, never left for you to write), apply the proposed standards-doc edit above if the retro named one, attach the scanned transcript, and set the task done. Under auto-mode this is the one consent point covering every one of those mutations, so it must enumerate them rather than authorize a bare "merge"; the two contingent ones are named here precisely so they are never a surprise write if their trigger occurs.
