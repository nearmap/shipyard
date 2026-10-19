# KICKOFF: the brief, the ticket, the branch

Read durable cross-session memory first — `memory_list` (or `memory_search` on the tools and surfaces the objective touches) per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`. A lesson that bears on the build enters the brief as a known anchor; one this session directly contradicts is refuted on the spot with `memory_refute` (same reference), never carried forward.

## The brief

A complete brief has every one of these sections:

- goal and deadline;
- deploy target, deploy command, and version check — the command that reports which SHA is live. "Localhost only" is a valid answer and needs no command;
- deploy side effects — who or what else sees the target change;
- shortcuts accepted;
- shortcuts refused — these become ledger rules;
- unknowns to settle first;
- what "done" means;
- a "Read this first" line opting out of `/sy:spec`, GATE, and merge standards, and stating that this branch never merges.

**Ticket given.** `get-issue` it and check the brief against that list. Ask for the missing sections in batched `AskUserQuestion` calls of at most four (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`), then post the answers as one `# MVP brief` comment via `post-comment`. Never rewrite a body the user wrote; the brief comment completes it.

**Free-text objective.** Choose the parent epic as `/sy:spike` does (`find-issues` for epics, then `AskUserQuestion`), interview for the same sections, show the drafted brief, and create the Task only on explicit approval: `create-issue` with `parent`, the brief as its body.

## Start the build

Name the mutations at the approval point — the brief comment or the new Task, the status moves, the assignment, the branch, the first ledger entry — so the go-ahead covers them by name. Then:

1. `set-status` the parent epic `in-progress` if it is not already;
2. `set-status` the ticket `in-progress` and `assign` it to yourself;
3. create the branch `<ticket>/<slug>` in a worktree under the resolved `worktree.root` (`get_config {"key": "worktree.root"}`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`);
4. post the first `# MVP ledger` entry (`references/loop.md`): `change: rule`, the invariants in `SKILL.md` as the default contract, plus every refused shortcut from the brief as a rule verbatim.

Then route to LOOP.
