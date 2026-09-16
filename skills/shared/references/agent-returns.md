# Agent returns: one hand-back, spent on the contract return

An agent gets exactly one hand-back, and four rules follow from that. This file is the only place they are stated, for the reason `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md` is cited rather than re-pasted: a rule that must be copied is a rule that will be missed. Cite this path from a brief; never restate what is below.

## The rules

- **spend it on the contract return and nothing else** — `SubagentHandback` delivers exactly one report per agent. A second call is refused (`Nothing was sent: your report was already delivered`) and reaches nobody, so a hand-back spent on a mid-round status note, a progress update, or a "this is not the final return" is the contract return thrown away.
- **no mid-round channel** — most delegates hold no `SendMessage`, whatever the refusal text suggests. Mid-round state goes to a file; the agent returns once.
- **persist before the return** — an agent whose report the caller cannot cheaply regenerate writes it to the repo-keyed scratch root before returning and names the path in its return block, so the return is a pointer to a durable artifact rather than the artifact's only copy.
- **a return ends the agent's turn** — no further work after emitting one.

## Persisting a report

The write target is the repository-keyed scratch root — `scratch_dir {"repo": true}`, whose exposed name resolves per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md` and which is the same directory from the main checkout or any worktree of it — and the write goes through the `Write` tool, never a shell redirect: the mutation guard reads every `>` in a Bash command as a redirect target, including one inside the report's own prose. Name the file for what it covers and stamp it, `<kind>-<scope>-<UTC basic timestamp>.md`, so a second run over the same scope does not clobber the first. A brief that states its reason for deviating from that shape is not violating it: `${CLAUDE_PLUGIN_ROOT}/agents/repo-review.md` writes one unstamped file per review scope because its report is posted once per scope, and `${CLAUDE_PLUGIN_ROOT}/agents/explain-author.md` writes `<topic>_explainer.md` into the topic-keyed directory (`scratch_dir {"identifier": "<topic-slug>"}`) that is the doc's own address. An agent holding no `Write` grant has a report its caller can cheaply regenerate, and persists nothing.
