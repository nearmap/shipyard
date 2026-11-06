# LOOP: the report grows one turn at a time

One document per row key (the format the standards contract names; a Quarto `.qmd` by default), one load cell at its top reading the parquet(s) from `scratch_dir(<task>)/data/`, and every analysis cell inline below it so any number can be questioned from the document. No bespoke results files, no helper modules the document calls for its numbers.

## A turn

1. Classify what you asked for (table below).
2. Dispatch `sy:slice` in the recorded worktree with the cell to add or change, the document, the parquet path and the standards primitives; it commits locally and returns the figure paths. One slice per turn; batch several small asks into it.
3. Render to HTML in the worktree; route the render log to the scratch directory and read back only failures.
4. Fan every new or changed figure out to `sy:img-inspector` with the cell's column names and the intended story (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`). A verdict of `regenerate` is folded into the same turn.
5. Publish the HTML through the `Artifact` tool, republishing to the same URL per document and variant; record the version in `report-state.yaml`.
6. Close with the status line and one `AskUserQuestion`: add · change · variant · publish · done.

The first turn drafts every figure the intake listed on the sample parquet; once the full parquet lands, the next turn re-renders on it and the status turn names any headline number that moved.

## Turn kinds

| you ask for | classification | what happens |
|---|---|---|
| a figure, a section, an edit, a colour map | figure or edit | a cell; no contract change |
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
<task> · head <sha> · artifact v<N> (<documents>) · site: <not uploaded | url> · data: <sample | full> · extraction: <done | p% ETA> · contract v<N>
```

## Checkpoints

`report-state.yaml` is updated before each `AskUserQuestion` and after each slice commit: contract version, parquet paths and hashes, documents and their variants, head SHA, artifact URLs and versions, the turn in flight. A resumed session reads it (`SKILL.md` § Rebuild first) and continues from the last closed turn; it re-does no committed slice and re-posts no comment.
