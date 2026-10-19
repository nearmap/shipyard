# HARVEST: freeze the reference, decompose it, own every surface

In order. Nothing below writes the tracker before step 6's approval.

## 1. Freeze

Run `git status --porcelain --ignored` in the MVP worktree. Before pushing, put the untracked or ignored files the reference depends on — generators, data builders — to the user in one `AskUserQuestion`: an approved file is committed; a declined one becomes a `do not port` row with its reason. Never force-add a secret or credential file (`.env`, keys, tokens): each one becomes a `do not port` row naming what production needs instead.

Push the MVP branch and take its head as the reference SHA. No tag: the branch is never rebased or force-pushed, so the SHA stays reachable for as long as the branch lives, and `/sy:shutdown` deletes the branch once every Task the manifest names is done. A later harvest posts a newer SHA the same way.

## 2. Inputs

- Changed paths: `git diff --name-status $(git merge-base origin/main <sha>) <sha>`, `<sha>` being the reference SHA.
- Shortcuts: the trailer log (`SKILL.md` § Rebuild first) over the same range.
- The ledger's live rules.
- The parent epic's open children (`find-issues {"parent": "<epic>"}`), which may already own rows.

## 3. Decompose

Cut the reference into Tasks by the seam and leaf rules in `${CLAUDE_PLUGIN_ROOT}/skills/plan/references/roadmap-shaping.md`: one coherent PR each, real dependency order, a blocker only where one exists. Count only the Tasks this harvest creates against the resolved `plan.max_active_tasks` cap (per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`). If more are needed, ask: create all / create the critical subset and keep the rest as named groups in the manifest / other.

## 4. Debate

Run `sy:debate` over the decomposition unconditionally (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/debate.md`), then steer it with `AskUserQuestion`.

## 5. Coverage manifest

- Every changed path, or a `/`-terminated directory row, maps to its owning Task(s) or to `do not port` with a reason.
- Every shortcut maps to the Task that closes it, or to `drop` with a reason.
- Wherever a planned Task's scope differs from the reference — a surface nobody owns, a shortcut no Task closes, a Task covering something the reference never built, a path production will take differently — ask the user whether it is intentional, via `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`). Record each answer on its row.
- No row is left without an owner or a recorded drop.

## 6. Approval

One `AskUserQuestion` naming every mutation: create N Tasks in `backlog` under the epic; `add-dependency` each blocker; post one `# MVP reference` comment per derived Task; post a `harvest` ledger entry carrying the reference SHA and `# MVP coverage manifest` on the MVP ticket; when the parent epic carries a `/sy:plan` roadmap, post one decision-log delta on it.

## 7. Write and verify by read-back

Post the `harvest` ledger entry first (`references/loop.md` § The ledger entry): it is what a harvest interrupted from here resumes from. Create the Tasks (`create-issue` with `parent`), record the blockers, and post each `# MVP reference` comment via `post-comment`:

- `human`: port by hand from the reference — never cherry-pick or check out reference files; no MVP names (the ticket key, the MVP branch, `mvp`/`demo`/`spike`, demo prefixes) in shipped code; and this Task's scope in plain words.
- `agent_detail`: the MVP branch and reference SHA, the owned paths, the owned shortcuts with their commit SHAs, or the literal `none`, and the `do not port` rows touching this Task.

Post the `# MVP coverage manifest` on the MVP ticket, its `agent_detail` carrying every row. Read each write back with `get-issue`; a Task, blocker, or comment missing from the read is a failed write, never assumed posted. Both comment types are append-only: a later correction is a new comment naming what it supersedes, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`.

## 8. Epic

When the parent epic carries a `/sy:plan` roadmap (a decision-log comment), post one decision-log delta without a `Plan checkpoint` footer, so earlier retros stay in `/sy:plan` re-entry's window, per `${CLAUDE_PLUGIN_ROOT}/skills/plan/references/checkpoint-handoff.md`: the new leaves, with the MVP ticket as their conceptual parent branch. Leave the epic body for `/sy:plan <epic>` to re-render.

## 9. Handoff

Give `/sy:spec <task>` for each new Task in dependency order. The MVP ticket stays `in-progress` while its hold is open; otherwise suggest `/sy:shutdown <ticket>`.
