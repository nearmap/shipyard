---
name: shutdown
description: >-
  Wind down one ticket's local footprint: list its worktrees, branches, scratch directory, and
  lingering background tasks, then — on one approval — stop and remove only what is recoverable.
  For an MVP ticket, also release its environment hold and close it as decomposed.
  Use when asked to clean up or close out a ticket. Main session only; never from a `/sy:ship`
  worker or other subagent.
argument-hint: "[ticket key]"
---

Clean up after one ticket without losing work. List first, act only on one approval, and leave anything unrecoverable standing.

$ARGUMENTS

## 1. Preflight

Only when a ticket is named or inferred from the current branch, run the tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`); a failure stops here with its single `## Action needed` block. With no ticket there is no tracker work and no preflight.

## 2. Inventory — read-only, scoped

- **Scope.** With a ticket: only branches matching `<ticket>/`, with or without a worktree; their worktrees and the detached worktrees whose directory name starts with `<ticket>-`, under the resolved `worktree.root` (`get_config {"key": "worktree.root"}`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`), and only `scratch_dir {"identifier": "<ticket>"}` — never `{"repo": true}`. With none: the current worktree only.
- **Each worktree's state.** `git status --porcelain --ignored` for uncommitted and ignored files. For a detached worktree, also `git branch -r --contains HEAD`: empty means its HEAD is on no remote branch.
- **Each `<ticket>/` branch's recoverability.** Its tip is recoverable only if it is an ancestor of `origin/main` or of the newest pushed `mvp/<ticket>[-N]` tag (`git merge-base --is-ancestor <branch> <ref>`; tags from `git ls-remote --tags origin 'mvp/<ticket>' 'mvp/<ticket>-*'`), or equals the `headRefOid` of a merged PR (`gh pr list --head <branch> --state merged --json headRefOid`).
- **In-flight `/sy:ship`.** `ship-state.yaml` in the ticket's scratch dir records a PR (`pr:`) that is not yet merged (`gh pr view <pr> --json state`).
- **Lingering background tasks** this session started and no longer uses: background shells (`ci_poll.sh` pollers, dev servers, watchers), `Monitor`s, idle or finished background agents, and scheduled `CronCreate`/`ScheduleWakeup` loops. A task still doing work the user asked for is listed as kept.
- **MVP ticket.** `get-issue` it: the newest `# MVP ledger` entry's whole `agent_detail`, its open hold included, and whether a `# MVP coverage manifest` exists.

## 3. Never plan the loss of unrecoverable work

A `<ticket>/` branch not judged recoverable, a worktree on such a branch, a worktree with uncommitted changes, a worktree holding ignored files other than caches (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `node_modules`; the files named), a detached worktree whose `git branch -r --contains HEAD` is empty, and the worktrees and scratch directory of an in-flight `/sy:ship` are each listed as **kept**, with the reason. None is offered for deletion, and no option in step 4 reaches one.

## 4. One approval

One `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`) naming each mutation:

- the background tasks to stop;
- worktree removals, local branch deletions, and the scratch directory removal (remote branches are left to the forge's delete-on-merge);
- for an MVP ticket: post the hold-release `# MVP ledger` entry per `${CLAUDE_PLUGIN_ROOT}/skills/mvp/references/loop.md` § The ledger entry, carrying the newest entry's snapshot forward with `change: hold` and `hold: none`; then, only when a `# MVP coverage manifest` exists, the canonical decomposition closure, steps 3-5 of `${CLAUDE_PLUGIN_ROOT}/skills/tracker/SKILL.md` § Canonical decomposition, with the tag beside the replacement IDs in `agent_detail`. With no manifest, ask instead: keep the ticket open / close it as abandoned: `post-comment` the reason, then `set-status done`.

Options: all / choose / other.

## 5. Execute, in this order

1. Stop the approved background tasks (`TaskStop`, `CronDelete`, ending a `/loop`).
2. Tracker writes.
3. Local cleanup: `git worktree remove` without `--force`, local branch deletion with `git branch -d` only, scratch removal. No remote branch is deleted, and a pushed `mvp/` tag never is.

## 6. A denied action stays denied

Apply the denied-write boundary in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`: never retry a denied action another way. Finish everything else, then end with one `## Action needed` block listing each denied action as an exact `! <command>` line the user can run.

## 7. Verify by read-back

Re-read `git worktree list`, and — for a ticket — `get-issue`; confirm no stopped task is still running (`pgrep -f ci_poll.sh` for the pollers, `CronList` for scheduled loops). Report what is left and why.
