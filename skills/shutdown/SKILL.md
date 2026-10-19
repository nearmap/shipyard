---
name: shutdown
description: >-
  Wind down one ticket's footprint on the user's request: stop lingering background tasks, remove
  its recoverable worktrees and branches, close the ticket. Anything unrecoverable or ambiguous is
  kept and asked about. For an MVP ticket, also release its environment hold and close it as decomposed.
  Use when asked to clean up or close out a ticket. Main session only; never from a `/sy:ship`
  worker or other subagent.
argument-hint: "[ticket key]"
---

Clean up after one ticket without losing work. The user's request is the approval: act directly on everything recoverable, and ask only about what is ambiguous or unrecoverable, which is listed as kept by default.

$ARGUMENTS

## 1. Preflight

Only when a ticket is named or inferred from the current branch, run the tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`); a failure stops here with its single `## Action needed` block. With no ticket there is no tracker work and no preflight.

## 2. Inspect — read-only, scoped

- **Scope.** The ticket's `<ticket>/` branches and worktrees (with or without a worktree), the detached worktrees whose directory name starts with `<ticket>-` under the resolved `worktree.root` (`get_config {"key": "worktree.root"}`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`). With no ticket: the current worktree only. Never touch pre-existing untracked files, user files in `/tmp`, already-modified lockfiles, other sessions' worktrees, or branches with an open PR.
- **Worktrees.** `git worktree list`, and `git status --porcelain --ignored` in each. For a detached worktree also `git branch -r --contains HEAD`: empty means its HEAD is on no remote branch.
- **Branches.** For each `<ticket>/` branch, `gh pr list --head <branch> --state all --json state,headRefOid`; its tip is recoverable only if it equals a merged PR's `headRefOid`, or is an ancestor of `origin/main` or of the MVP ledger's newest harvest SHA (`git merge-base --is-ancestor <branch> <ref>`).
- **In-flight `/sy:ship`.** `ship-state.yaml` exists in the ticket's scratch dir and its `pr:` is unset or not yet merged (`gh pr view <pr> --json state`).
- **MVP ticket.** `get-issue` it: the newest `# MVP ledger` entry's whole `agent_detail`, its open hold and harvest SHA included, and whether a `# MVP coverage manifest` exists. A Task carrying a `# MVP reference` comment brings its MVP ticket into scope the same way, for the MVP branch alone.
- **MVP branch.** When a manifest exists, `get-issue` every Task named by any manifest: the MVP branch is retired only when all of them are done, no manifest row is owned by a named group, a manifest is newer than the newest `harvest` ledger entry, and its remote head equals the newest harvest SHA.

## 3. Kept, never deleted

Listed as **kept**, with the reason and the files named: a branch not judged recoverable and its worktree; a worktree with uncommitted changes; a worktree holding ignored files other than caches (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `node_modules`, `.pixi`, `.venv`, `target`); a detached worktree on no remote branch; the worktrees and scratch directory of an in-flight `/sy:ship`, unless the user named that worktree or directory. Ask about these only if the user wants one gone, and ask through `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`).

## 4. Execute, in this order

1. Stop lingering background tasks this session started and no longer uses: `TaskStop`, `CronDelete`, ending a `/loop`, killing the dev servers and `ci_poll.sh` pollers it started. A task still doing work the user asked for is kept.
2. Primary checkout, as one Bash call (a lone `cd` outside the session's directories does not persist): `cd <primary> && test -z "$(git status --porcelain)" && git switch main && git pull --ff-only`.
3. Worktrees, except the one this session runs in: `git worktree remove` without `--force`, then `git worktree prune`. The session's own worktree is listed in the summary for the user to remove after `/exit`.
4. Branches. A local branch whose tip equals a merged PR's `headRefOid`: `git branch -D` (`-d` fails on squash merges). Any other recoverable branch: `git branch -d`, else keep it. A remote branch is deleted only when it still exists (`git ls-remote --heads origin <branch>`), its PR is merged, and the remote head equals that `headRefOid`; the one other remote branch deleted is a retired MVP branch; nothing else remote.
5. Scratch: remove this session's scratchpad. Keep the ticket's shipyard `scratch_dir` — it is the run record — unless the user asks.
6. Tracker. A merged non-MVP ticket: `set-status done` if it is not already. An MVP ticket not already done: post the hold-release `# MVP ledger` entry per `${CLAUDE_PLUGIN_ROOT}/skills/mvp/references/loop.md` § The ledger entry, only when the newest entry's hold is open, carrying the newest entry's snapshot forward with `change: hold` and `hold: none`; then, only when a `# MVP coverage manifest` exists, the canonical decomposition closure, steps 3-5 of `${CLAUDE_PLUGIN_ROOT}/skills/tracker/SKILL.md` § Canonical decomposition, with the reference SHA beside the replacement IDs in `agent_detail`. With no manifest, ask through `AskUserQuestion`: keep the ticket open, or close it as abandoned (`post-comment` the reason, then `set-status done`).
7. Offer a memory write if the session taught a lesson; optionally name the next ready ticket.

## 5. A denied action stays denied

Apply the denied-write boundary in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`: never retry a denied action another way. Finish everything else, then end with one `## Action needed` block listing each denied action as an exact `! <command>` line the user can run.

## 6. Summary

Re-read `git worktree list` and — for a ticket — `get-issue`; confirm no stopped task is still running (`pgrep -f '[c]i_poll.sh poll <PR>'`, `CronList`) and `git branch --list '<ticket>/*'`. End with what is left and why, and "type `/exit`".
