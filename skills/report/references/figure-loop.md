# LOOP: the report grows one turn at a time

One document per row key (the format the standards contract names; a Quarto `.qmd` by default), one load cell at its top reading the parquet(s) from `scratch_dir(<task>)/data/`, and every analysis cell inline below it so any number can be questioned from the document. No bespoke results files, no helper modules the document calls for its numbers.

## A turn

1. Classify what you asked for (table below).
2. Dispatch `sy:slice` in the recorded worktree with the cell to add or change, the document, the parquet path and the standards primitives; it commits locally and returns the figure paths. One slice per turn; batch several small asks into it.
3. Render to HTML in the worktree; route the render log to the scratch directory and read back only failures.
4. Run the checklist in `references/anticipate.md` over every new or changed figure and headline number; build the cell-cost hits now as variants under their primary figure, in the same slice, labelled and grouped as `references/variant-picker.md` § In the document says.
5. Fan every new or changed figure, variants included, out to `sy:img-inspector` with the cell's column names and the intended story (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`). A verdict of `regenerate` is folded into the same turn.
6. Render the draft with the picker profile and publish the HTML through the `Artifact` tool with `capabilities: {db: {}, user: {}}`, republishing to the same URL per document and parameter value; record the version in `report-state.yaml`.
7. Close with the status line and one `AskUserQuestion`: the anticipate hits that cost a parameter or a change to the data contract first, each with its cost, then add · change · variant · publish · done.

Before the next slice is dispatched, read the picker's selections back (`references/variant-picker.md` § Reading selections back) and record them in `report-state.yaml`; a variant omitted there is not re-rendered as kept.

The first turn drafts every figure the intake listed on the sample parquet; once the full parquet lands, the next turn re-renders on it and the status turn names any headline number that moved.

## Turn kinds

| you ask for | classification | what happens |
|---|---|---|
| a figure, a section, an edit, a colour map | figure or edit | a cell; no contract change; its anticipate variants are built in the same slice |
| keep or omit a variant | selection | done with the picker's buttons in the draft artifact, never as a turn; read back before the next slice |
| the same report under a row filter, every cell and sentence applying to both | variant, parameter | a parameter on the one document, rendered once per value; a later figure lands in every render |
| a variant that wants its own follow-up figures or its own conclusions | variant, fork | a second document on the same parquet and load cell, started as a copy with the parameter fixed; from here the two are edited separately and a turn names which document it is for. Promote a parameter to a fork at any turn; never the reverse |
| a column derivable from the parquet | change, cell | a cell; no extraction |
| a column from a new source on the same row key | change to the data contract | `references/data-contract.md` § 2 for that column alone, posted as `# Data contract vN+1` with `Supersedes`; one more parquet on the same key, joined at load. Sample first, full in the background. One `AskUserQuestion` naming the cost: sample first · full only · skip the figure |
| a figure whose row is a different thing | change to the data contract, new row key | never refused. Contract vN+1 names the new key with its own sample rule and asserts, usually built from the existing parquet; the figure lives in a new document; the same cost question; the `# Report` comment will state both keys next to their artifacts |
| a site upload now | publish | `references/check-publish.md` § Site, mid-loop; the loop continues |
| done | check | `references/check-publish.md` |

## Status line

Every status turn opens with one line, so where things are is never a question you have to ask:

```text
<task> · head <sha> · artifact v<N> (<documents>) · variants: <kept>/<built> · site: <not uploaded | url> · data: <sample | full> · extraction: <done | p% ETA> · contract v<N>
```

## Checkpoints

`report-state.yaml` is updated before each `AskUserQuestion` and after each slice commit: contract version, parquet paths and hashes, documents and their parameters, the variants built per primary figure and the selections last read, head SHA, artifact URLs and versions, the turn in flight. A resumed session reads it (`SKILL.md` § Rebuild first) and continues from the last closed turn; it re-does no committed slice and re-posts no comment.
