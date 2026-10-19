---
name: shutdown
description: >-
  Wind down one ticket's local footprint: list its worktrees, branches, scratch directory, and
  lingering background tasks, then — on one approval — stop and remove only what is recoverable.
  For an MVP ticket, also release its environment hold and close it as decomposed.
argument-hint: "[ticket key]"
disable-model-invocation: true
---

Clean up after one ticket without losing work. List first, act only on one approval, and leave anything unrecoverable standing.

$ARGUMENTS

## 1. Preflight

Only when a ticket is named or inferred from the current branch, run the tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`); a failure stops here with its single `## Action needed` block. With no ticket there is no tracker work and no preflight.

## 2. Inventory — read-only, scoped

- **Scope.** With a ticket: only branches matching `<ticket>/` and their worktrees under the resolved `worktree.root` (`get_config {"key": "worktree.root"}`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`), and only `scratch_dir {"identifier": "<ticket>"}` — never `{"repo": true}`. With none: the current worktree only.
- **Each worktree's state.** `git status --porcelain` for uncommitted work; `git log @{u}..` for unpushed commits. A branch with no upstream counts as unpushed.
- **Lingering background tasks** this session started and no longer uses: background shells (`ci_poll.sh` pollers, dev servers, watchers), `Monitor`s, idle or finished background agents, and scheduled `CronCreate`/`ScheduleWakeup` loops. A task still doing work the user asked for is listed as kept.
- **MVP ticket.** `get-issue` it: the newest `# MVP ledger` entry's open hold, whether a `# MVP coverage manifest` exists, and whether the `mvp/<ticket>` tag is pushed (`git ls-remote --tags origin mvp/<ticket>`).

## 3. Never plan the loss of unrecoverable work

A worktree with uncommitted or unpushed changes, or an MVP branch with no pushed tag, is listed as **kept**, with the reason. It is never offered for deletion, and no option in step 4 reaches it.

## 4. One approval

One `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`) naming each mutation:

- the background tasks to stop;
- worktree removals, local branch deletions, remote branch deletions, and the scratch directory removal;
- for an MVP ticket: post the hold-release `# MVP ledger` entry (`change: hold`, `hold: none`); then, only when a `# MVP coverage manifest` exists, the canonical decomposition closure in `${CLAUDE_PLUGIN_ROOT}/skills/tracker/SKILL.md` — a `# Decomposition` note naming the derived Tasks and the tag, `add-label decomposed`, `set-status done`. With no manifest, ask instead: keep the ticket open / close it as abandoned with a reason.

Options: all / choose / other.

## 5. Execute, in this order

1. Stop the approved background tasks (`TaskStop`, `CronDelete`, ending a `/loop`).
2. Tracker writes.
3. Local cleanup: `git worktree remove`, local branch deletion, scratch removal.
4. Remote branch deletions. A pushed `mvp/` tag is never deleted.

## 6. A denied action stays denied

Apply the denied-write boundary in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`: never retry a denied action another way. Finish everything else, then end with one `## Action needed` block listing each denied action as an exact `! <command>` line the user can run.

## 7. Verify by read-back

Re-read `git worktree list`, `git ls-remote --heads origin`, and — for a ticket — `get-issue`; confirm no stopped task is still running (`pgrep -f ci_poll.sh` for the pollers, `CronList` for scheduled loops). Report what is left and why.
