# GitHub issue migration cookbook

This is a separate migration workflow. Do not load it during normal `/plan → /spec → /ship` execution.

1. Reuse a tracking item already promoted to Epic; do not duplicate it as a child Task.
2. Create one Jira Task per executable GitHub issue under the Epic.
3. Maintain an explicit `gh# -> Jira key` ledger and skip mapped rows; creation is not inherently idempotent.
4. Create first, then assign separately when create-time assignment is unreliable.
5. Map closed GitHub issues to Jira Closed only when the migration semantics genuinely mean terminal/delivered; do not misuse decomposed closure.
6. Write bodies and comments as Markdown and let the `create-issue`/`post-comment` tools convert to ADF — there is no separate conversion step to run and no staged `.adf.json` to manage. Prefix imported comments with GitHub author/date because Jira records the API token owner as author.
7. Use the `link-parent` tool for re-parenting and cross-level parent updates.
8. Preserve provenance: prepend `Migrated from GitHub #<n>`, add Jira backlink on GitHub, and close rather than delete source issues.

Preserve the mapping ledger and make reruns idempotent.
