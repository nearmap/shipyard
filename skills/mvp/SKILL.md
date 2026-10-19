---
name: mvp
description: >-
  Build a working MVP fast on a branch that never merges, outside spec, GATE, and merge
  standards, with every shortcut recorded as it is taken; then harvest it into PR-sized
  Tasks that each port by hand from a frozen tag. Unlike /sy:spike, which measures an idea
  and returns a verdict, an MVP builds the thing itself.
argument-hint: "[ticket key, or a free-text objective]"
disable-model-invocation: true
effort: high
---

Build the thing, show it working, then turn what it taught into real work. The MVP branch is a reference, never a delivery: production code reaches `main` only through the Tasks the harvest derives.

Before anything else, run the tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`); a failure stops here with its single `## Action needed` block.

$ARGUMENTS

## Rebuild first

On every invocation, and after any compaction or resume, before other work: `get-issue` the ticket and take the newest `# MVP ledger` comment's `agent_detail`. It is the full live state — every entry is a snapshot, so the newest alone is enough. If no ledger entry is on the page and `comments_truncated` is set, stop loudly rather than rebuild from less. Also note whether a `# MVP coverage manifest` is on the page, and list the pushed tags with `git ls-remote --tags origin 'mvp/<ticket>' 'mvp/<ticket>-*'`. Read the shortcuts taken so far in the MVP worktree:

```
git log --format='%h %(trailers:key=Shortcut,valueonly)' origin/main..HEAD
```

A reference read earlier in this session is only a summary after compaction; re-read the current state's reference before acting on it.

## Invariants

- No `/sy:spec`, no GATE, no merge standards on the MVP branch.
- The branch never merges into `main`. It may merge `main` in; it is never rebased or force-pushed.
- Localhost first. Deploy only on the user's word.
- A deploy is verified by the brief's version check reporting the pushed SHA, never by readiness alone.
- A ledger entry is posted on each deploy, each new user rule, and each hold change (`references/loop.md`).
- Every shortcut gets a `Shortcut: <what was faked> -> <what production needs>` trailer on the commit that takes it.
- Ask only through `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`).
- Tracker mechanics belong to the `tracker` skill (`/sy:tracker`); this skill names canonical verbs only.

## Agents on their own

The MVP session builds directly; these run beside it, never in place of the user's word on a deploy:

- `sy:sweep` for broad reconnaissance of a large surface;
- `sy:trace` for one load-bearing path, at most the resolved `limits.max_depth_agents` cap in flight — resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`;
- `sy:slice` for a bounded build step in its own worktree; its brief carries the trailer rule above;
- `sy:img-inspector` for any screenshot or rendered visual (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md`);
- `/sy:ci` for a pushed branch's CI.

Every subagent dispatch resolves its model from config and passes it as the `Agent` invocation's actual model override, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`; a nested dispatch inherits nothing and must resolve again.

## State router

Classify before loading detailed procedure:

```text
KICKOFF   no ticket, or brief incomplete   → references/kickoff.md
HARVEST   tag pushed, no manifest          → references/harvest.md from step 2, on the newest tag
HELD      manifest posted, hold open       → build nothing; report the hold → /sy:shutdown <ticket>
LOOP      brief complete, building         → references/loop.md
HARVEST   the user says the MVP is done    → references/harvest.md
RELEASE   harvest done, hold released      → /sy:shutdown <ticket>
```

Load only the reference for the current state. Do not preload mutually exclusive procedures.
