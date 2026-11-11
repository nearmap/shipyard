---
name: report
description: >-
  Build one analysis report from a Task. Spec and gate only the data load, extract a seeded
  sample first and the full set in the background, then grow the report figure by figure in
  conversation, every render published for a look. One check pass and a PR at the end; no
  execution plan, no multi-round gate.
argument-hint: "<task>, or a free-text goal"
disable-model-invocation: true
effort: high
---

Build one report whose shape is discovered by looking, not planned ahead. The data load is the one thing specced and gated, because that is where a report's expensive mistakes live; every figure after it is a turn in a loop you steer. Where `/sy:spec` ends at a plan and `/sy:ship` builds one, this skill does both for a report and stops at a reviewable PR; where `/sy:spike` measures an idea and returns a verdict, this skill produces the document itself.

Before anything else, run the tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`); a failure stops here with its single `## Action needed` block.

$ARGUMENTS

## Rebuild first

On every invocation, and after any compaction or resume, before other work: `get-issue` the Task and read the newest `# Data contract vN`, `# Extraction`, and `# Report` comments; then read `report-state.yaml` in the Task's scratch directory (`scratch_dir {"identifier": "<task>"}`, resolved per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`). The state file is the live run: contract version, parquet paths and hashes, document list, head SHA, artifact and site URLs, and which turn is in flight. The tracker comments are the durable record it must agree with; on disagreement trust the tracker and say so. A reference read earlier in this session is only a summary after compaction; re-read the current state's reference before acting on it.

## Invariants

- **Only the data contract is planned and gated.** The row key, the columns, the sample rule, the asserts and the cost are drafted from the figures you expect, traced with `sy:trace`, reviewed once by `sy:spec-gate`, and approved by one `AskUserQuestion`. Figures, sections and prose are never planned ahead: they are asked for, built, rendered and looked at, one turn at a time (`references/figure-loop.md`).
- **Extracted data lives outside every worktree**, under `scratch_dir {"identifier": "<task>"}/data/`, with its path, sha256, row count and wall time posted to the Task. A re-spec, a branch wipe or a worktree removal never touches it, and nothing already read is read again.
- **Sample first.** The sample rule is a line in the contract, approved with the columns: a seeded uniform random draw on the row key by default, stratified by a column the ticket names when figures need every stratum populated. The sample is extracted before the full set so figure work starts in minutes; the full extraction runs in the background with a progress and an ETA on every status turn.
- **A change to the data contract is a normal turn, never a refusal.** A column derivable from the parquet is a cell. A column from a new source on the same row key is one more parquet joined at load, posted as `# Data contract vN+1` as a diff. A new row key is a second extraction, usually from the existing parquet, with its own sample rule and asserts and its own document. Each full-data extraction gets one approval question naming its cost.
- **A variant is a parameter while every cell and sentence applies to both renders, and a fork once its analysis diverges.** A parameter can be promoted to a fork at any turn, never the reverse.
- **Variants are anticipated, not awaited.** After every render, the checklist in `references/anticipate.md` runs over each new figure and headline number; cell-cost hits are built as variants under their primary figure before you see the draft, and costlier hits are named with their cost as the first options of the closing question. The draft artifact carries keep/omit buttons per variant (`references/variant-picker.md`); their selections are read back from the artifact's database before the next slice and at CHECK, and the final report contains exactly what was kept.
- **Every turn ends with the document rendered and published.** The default is render to HTML and publish through the `Artifact` tool; an external site is an option at the end or a `publish` turn mid-loop, and its destination is asked, never inferred.
- **Images stay out of this context.** Every rendered figure is inspected by `sy:img-inspector` per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`; no image `Read` appears in this session or any delegate.
- **One check pass, no rounds.** When you say done, the checks in `references/check-publish.md` run once; what they find is fixed as an ordinary figure-loop turn and you look again. There is no immutable-SHA review loop, no automated reviewer request, no fix cycle. Code review of the document's code is `/sy:pr`'s and the merge is yours.
- Every edit to the document runs in a `sy:slice` delegate in a caller-created, recorded worktree under the resolved `worktree.root`; verbose output (render logs, linters, tests) stays in the Task's scratch directory and only failures and summary lines are read back.
- Talking to you follows exactly one of three modes per turn — status update, `AskUserQuestion`, or an isolated `## Action needed` block — never blended; see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`. Every status turn carries the status line in `references/figure-loop.md` § Status line.
- Tracker writes obey write integrity (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`): a later contract corrects an earlier one by naming what it supersedes, never by editing it; the Task body is never rewritten.
- Tracker mechanics belong to the `tracker` skill (`/sy:tracker`); this skill names canonical verbs only. Human-facing comments go through `/sy:tighten` before posting.
- Read durable memory at the start (`memory_list`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`); refute a lesson this run directly contradicts at the point of discovery.

## State router

Classify before loading detailed procedure:

```text
INTAKE     no Task, or the Task lacks a row key, expected figures or data anchors → references/data-contract.md § 1
CONTRACT   intake complete, no approved `# Data contract` on the Task            → references/data-contract.md § 2
EXTRACT    contract approved, sample or full parquet not yet recorded             → references/data-contract.md § 3
LOOP       sample parquet recorded                                                → references/figure-loop.md
CHECK      the user said done                                                     → references/check-publish.md
RELEASE    PR open and `# Report` posted                                          → suggest /sy:shutdown <task>
```

Load only the reference for the current state. A change to the data contract from LOOP re-enters CONTRACT for that change alone and returns to LOOP; it never restarts intake.

## Agents

- `sy:trace` for the load path and any join whose coverage is unmeasured, at most the resolved `limits.max_depth_agents` cap in flight;
- the checklist in `references/anticipate.md`, in this session, over the figure list at CONTRACT and over each render at LOOP;
- `sy:spec-gate` once on the drafted contract, and again only on a material change to it (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/spec-gate.md` § When it runs);
- `sy:slice` for every document edit, in its own worktree; it returns a figure's path and never inspects it;
- `sy:img-inspector` for every rendered figure;
- `sy:sweep` only when a data source's layout is large enough to need breadth reconnaissance before the contract can be drafted.

No `sy:debate`: the core decision of a report is which figures, and you make it by looking. No `sy:gate`, `sy:ship-*`, or `sy:hunt`.

Every subagent dispatch resolves its model and effort from config and passes them as the `Agent` invocation's actual `model` and `effort` parameters, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`; a nested dispatch inherits nothing and must resolve again.
