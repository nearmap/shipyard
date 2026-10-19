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

- **Scope.** The ticket's `<ticket>/` branches and worktrees (with or without a worktree), the detached worktrees whose directory name starts with `<ticket>-` under the resolved `worktree.root` (`get_config {"key": "worktree.root"}`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`), and any sibling ticket worked in this session. With no ticket: the current worktree only. Never touch pre-existing untracked files, user files in `/tmp`, already-modified lockfiles, other sessions' worktrees, branches with an open PR, or a pushed tag.
- **Worktrees.** `git worktree list`, and `git status --porcelain --ignored` in each. For a detached worktree also `git branch -r --contains HEAD`: empty means its HEAD is on no remote branch.
- **Branches.** For each `<ticket>/` branch, `gh pr list --head <branch> --state all --json state,headRefOid`; its tip is recoverable only if it equals a merged PR's `headRefOid`, or is an ancestor of `origin/main` or of the newest pushed `mvp/<ticket>[-N]` tag (`git merge-base --is-ancestor <branch> <ref>`; tags from `git ls-remote --tags origin 'mvp/<ticket>' 'mvp/<ticket>-*'`).
- **In-flight `/sy:ship`.** `ship-state.yaml` in the ticket's scratch dir records a `pr:` not yet merged (`gh pr view <pr> --json state`).
- **MVP ticket.** `get-issue` it: the newest `# MVP ledger` entry's whole `agent_detail`, its open hold included, and whether a `# MVP coverage manifest` exists.

## 3. Kept, never deleted

Listed as **kept**, with the reason and the files named: a branch not judged recoverable and its worktree; a worktree with uncommitted changes; a worktree holding ignored files other than caches (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `node_modules`); a detached worktree on no remote branch; the worktrees and scratch directory of an in-flight `/sy:ship`, unless the user named them. Ask about these only if the user wants one gone, and ask through `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`).

## 4. Execute, in this order

1. Stop lingering background tasks this session started and no longer uses: `TaskStop`, `CronDelete`, ending a `/loop`, killing the dev servers and `ci_poll.sh` pollers it started. A task still doing work the user asked for is kept.
2. Worktrees: `git worktree remove` without `--force`, then `git worktree prune`.
3. Branches. A local branch whose tip equals a merged PR's `headRefOid`: `git branch -D` (`-d` fails on squash merges). Any other recoverable branch: `git branch -d`, else keep it. A remote branch is deleted only when it still exists (`git ls-remote --heads origin <branch>`), its PR is merged, and the remote head equals that `headRefOid`; nothing else remote, and a pushed `mvp/` tag never.
4. Primary checkout: only if clean, switch to `main` and `git pull --ff-only`.
5. Scratch: remove the files this session wrote (its scratchpad, the scratch files it wrote in the repo). Keep the ticket's shipyard `scratch_dir` — it is the run record — unless the user asks.
6. Tracker. A merged non-MVP ticket: `set-status done` if it is not already. An MVP ticket: post the hold-release `# MVP ledger` entry per `${CLAUDE_PLUGIN_ROOT}/skills/mvp/references/loop.md` § The ledger entry, carrying the newest entry's snapshot forward with `change: hold` and `hold: none`; then, only when a `# MVP coverage manifest` exists, the canonical decomposition closure, steps 3-5 of `${CLAUDE_PLUGIN_ROOT}/skills/tracker/SKILL.md` § Canonical decomposition, with the tag beside the replacement IDs in `agent_detail`. With no manifest, ask through `AskUserQuestion`: keep the ticket open, or close it as abandoned (`post-comment` the reason, then `set-status done`).
7. Offer a memory write if the session taught a lesson; optionally name the next ready ticket.

## 5. A denied action stays denied

Apply the denied-write boundary in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`: never retry a denied action another way. Finish everything else, then end with one `## Action needed` block listing each denied action as an exact `! <command>` line the user can run. A tool-side safety refusal of a variable-built path may be re-run once with the literal path; that is the same action.

## 6. Summary

Re-read `git worktree list` and — for a ticket — `get-issue`; confirm no stopped task is still running (`pgrep -f ci_poll.sh`, `CronList`). End with what is left and why, and "type `/exit`".
