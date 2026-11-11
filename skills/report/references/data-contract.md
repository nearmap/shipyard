# INTAKE, CONTRACT, EXTRACT: the one part of a report that is planned

## 1. Intake: what the Task must carry

Four things the tool cannot infer, as dot points in the Task body:

- **the question** the report answers;
- **the row key** — what one row is (one observation, one pair, one region);
- **the figures expected**, roughly, each naming its x, y, and any facet or slice. This is what puts a column into the contract: a figure asked for later that needs a column not here is a change to the data contract (`references/figure-loop.md`), never a failure of intake;
- **the data anchors** — where each source lives, as a path, a data dictionary, or a dataset name;
- optionally, **done when** — the deliverable beyond the artifact, such as a path in the repository or a site.

**Task given.** `get-issue` it and check the body against that list. Ask for the missing items in one batched `AskUserQuestion` call (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`); post the answers as one `# Report intake` comment via `post-comment`. Never rewrite a body the user wrote; the intake comment completes it.

**Free-text goal.** Choose the parent Epic as `/sy:spike` does (`find-issues` for Epics, then `AskUserQuestion`), interview for the same items, show the drafted body, and create the Task only on explicit approval: `create-issue` with `parent`, the body tightened with `/sy:tighten` (destination an issue body).

Read durable memory before the contract is drafted (`memory_list`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`); a lesson about a data source enters the contract as a known anchor.

## 2. Contract: from the figure list to the columns

Resolve each expected figure to the columns it needs, then run the checklist in `references/anticipate.md` over the figure list: a hit that needs a column not yet named (the paired quantity, the other covariate, the unfiltered population) adds it here, where it costs one extraction instead of a later change. The contract is the union of those columns on the row key, plus the slice columns the user habitually asks for. Draft it as one table, then the lines below it:

```text
Data contract v<N>
row key: <one row is ...>
| column | from | needed by | join |
sample: <uniform random on the row key | stratified by <column>>, n = <per stratum or total>, seed <int>
asserts: <0 rows unmatched on the key; join coverage ≥ <p>% ...>
cost: <files, rows, measured or estimated wall time at <workers>; sample time>
```

- Every join whose coverage is unmeasured gets a `sy:trace` dispatch on the load path before the contract is shown — one path per agent, at most the resolved `limits.max_depth_agents` cap in flight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`). A source keyed finer than the row key (one row per photo under a per-observation key) needs a stated reduction rule, and that rule is in the table.
- Resolve the repository's standards for the report's language and plotting through the `repo-standards` delegate, exactly as `/sy:spec` §3 does; its compact contract names the document format (the default is a Quarto `.qmd`) and the style primitives every figure reuses.
- Put the drafted contract through `sy:spec-gate`, resolving its model and effort with `agent_model {"name": "spec-gate"}` and passing them as the `Agent` invocation's parameters (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`). Seed it with the contract, the figure list, the standards contract, and the base commit and repository to read against. The reviewer applies the checklist in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/spec-gate.md` to a contract rather than a plan: axes 1–3 read as "is this the right key and the fewest columns, and will the load produce them"; axis 5 lists every expected figure as a visual-debug obligation; axes 4 and 6 are answered `none` unless the ticket names a doc or a site. Disposition every finding before the question below; the round cap and the denial handling in that reference apply unchanged.

Then one `AskUserQuestion`: approve · change columns · change the sample · change the row key · add a figure first. Name the mutations the approval authorizes: post the contract as `# Data contract v<N>` (`human` is the table and the lines above; `agent_detail` is the trace pointers and the spec-gate verdict), `set-status` the Task `in-progress`, `assign` it to yourself, create the branch `<task>/<slug>` and its worktree under the resolved `worktree.root`, and seed `report-state.yaml` in `scratch_dir {"identifier": "<task>"}`. On `vN+1` the comment opens with `Supersedes: v<N>` and carries the diff; the earlier comment is never edited.

## 3. Extract: sample now, full set in the background

1. Run the sample extraction in the foreground. It writes `scratch_dir(<task>)/data/contract-v<N>.sample.parquet`. Read back only the row count, the assert results and the wall time.
2. Start the full extraction as a background command with a progress file in the same directory; never poll it in-context (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md` § Waiting on a dispatch). Every status turn from here reports its progress and ETA.
3. When each parquet lands, post one `# Extraction` comment via `post-comment`: `human` is which parquet, rows, wall time; `agent_detail` is the path, sha256, seed and the assert values. This comment is what lets a later session reuse the data instead of re-reading it.
4. An assert that fails on the full set stops the loop with a status turn naming the value and one `AskUserQuestion`: relax the assert · change the contract · stop.

A change to the data contract from the figure loop re-enters this section for the new parquet alone: it is joined at load to the existing one and never replaces it, except for a new row key, whose own sample and full extraction run the same way into their own files.
