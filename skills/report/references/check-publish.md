# CHECK and RELEASE: one pass, then the PR

## The check pass

Runs once when you say done. First, read the picker's selections back (`references/variant-picker.md` § Reading selections back) and have the slice remove every omitted variant cell, so the checks below run on the document that ships. It is not a review loop: anything it finds is fixed as an ordinary figure-loop turn and you look again, and the pass is re-run only on the cells that changed.

| check | how | evidence |
|---|---|---|
| every number in prose equals a computed cell | the document's prose numbers are listed and each matched to a cell output in the render | the match list in the scratch directory |
| every axis label, legend and panel title matches its data | `sy:img-inspector` per figure, seeded with the cell's grouping and column names | the inspector verdicts |
| the load asserts hold on the full parquet | the load cell's assert lines in the render log | the lines |
| no identifiers that must not leave the repository appear in the HTML | the repository's redaction words (`redaction.extra_words`, resolved per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`) and any pattern the standards contract names | the grep |
| the standards contract's checks for the document's code | the named formatter, linter and tests, run in the worktree with output in the scratch directory | failures only |

## Release

1. Render the final HTML without the draft profile, so no picker and no omitted variant ships. Commit the document(s), `_quarto-draft.yml` and `_draft/variant-picker.html`, their rendered Markdown where the repository keeps it, and nothing from `scratch_dir(<task>)/data/`.
2. Open the PR through `/sy:pr`; the description is the question, the row key(s), and the artifact links. The acceptance-evidence comment carries the check table above with its evidence.
3. Post one `# Report` comment via `post-comment`: `human` is the headline numbers with the row key each is read against, and the artifact links; `agent_detail` is the PR URL, the head SHA, the parquet paths and hashes, every site URL, and the omitted variant ids with the last draft commit that held them. Tighten the human half with `/sy:tighten` first.
4. Post one `post-log` with the run's token accounting, aggregated over this session and every delegate, as `/sy:ship` does.
5. `set-status` the Task `in-review`. Merging is not this skill's: you merge, or ask `/sy:pr`.

Name the mutations in the `AskUserQuestion` that closes the loop (done · one more look): the PR, the two comments, the status move, and the site upload below if chosen.

## Site

An external site is never required and never inferred. When the repository provides a skill that publishes reports, the closing question — or a `publish` turn mid-loop — offers it, and a yes is followed by a second `AskUserQuestion` for the destination: the path the project last used, from the newest `# Report` comment on a sibling Task when one exists, offered as the default; another path; one document only. The chosen destination is recorded in the `# Report` comment next to its URL. Without such a skill, the artifact is the delivery and the question is not asked.

## After merge

Suggest `/sy:shutdown <task>` once the PR is merged; the parquets in the scratch directory are left for a later session, which is the point of posting their paths.
