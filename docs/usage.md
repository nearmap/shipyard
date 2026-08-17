# Using Shipyard

Once the plugin is [loaded](installation.md) and the repo is [configured](configuration.md) — or run `/sy:init-repo` below to do that interactively — day-to-day work is a short loop: plan a roadmap once (and revisit it when reality shifts), then per task, spec it, ship it, and merge it. Each command runs a long, mostly autonomous session, so the two habits that pay off most are **naming your sessions** and **letting each phase own its own session**.

## Name your sessions

Every Shipyard command kicks off a substantial session you may want to find again later. Give it a name at launch with `-n`/`--name`, which sets the session's display name (used in the session picker and the terminal title):

```bash
claude -n "spec PROJ-123 add billing" "/sy:spec PROJ-123"
```

The trailing argument is the first prompt, so the session opens straight into the command. A named session is easy to locate later with `claude --resume`. `/sy:spec` also suggests an in-session `/rename` once the work has a clear slug, so you can name it after the shape of the task becomes clear.

## 0. First time in this repo?

```text
/sy:init-repo
```

Every other command checks the tracker is genuinely usable — configured *and* live, not just present — as its first step, and stops with a named `## Action needed` block if it is not. `/sy:init-repo` is the fast path to fixing that: it reads whatever config already resolves, names any leftover `SY_*` variable still waiting to be retired by hand, asks only for what is actually missing, and writes each value to the right layer. On a repo someone already configured, joining it is usually just exporting your own credential; on a brand-new repo, it is the full interview. See [`configuration.md`](configuration.md) for what every value means and where it's allowed to live.

## 1. Plan

```text
/sy:plan add usage-based billing to the API
```

`/sy:plan` interviews you one question at a time (only when the answer changes the shape of the work), maps the code with read-only agents, and writes a living roadmap onto a single Epic. Executable work becomes direct child tasks, each sized to one coherent PR; at most a configured cap (`plan.max_active_tasks`) are active at once, and everything further out stays as text in the Epic until it is close enough to spec. Before the ladder is built, the roadmap's shape goes through a bounded proposer/adversary debate (`sy:debate`) — every time, not only when the shape looks contested — and you steer the disagreement rather than the plan quietly picking one.

Re-enter later with `/sy:plan <epic>` to read what shipped since the last checkpoint and reshape the roadmap. You run `/sy:plan` when you start an objective and whenever the roadmap needs revisiting — not once per task.

## 2. Spec

```text
/sy:spec <task>
```

`<task>` is an issue ID — a Jira key like `PROJ-123` or a GitHub issue like `#123`. `/sy:spec` reads the ticket and the code, resolves the repo's engineering standards, pulls representative data when shape matters, and asks you only when research cannot settle a decision. It writes a complete plan — the approach, the strongest rejected alternative (pressure-tested by the same debate, which runs on every plan before sign-off), ordered changes with file anchors, tests, acceptance criteria, a verification obligation for every activated risk lens, and the docs, visual, and pre-gate-checkpoint obligations the change makes necessary — then has a separate `sy:spec-gate` reviewer read that drafted plan for architecture, simplicity, correctness, and those last three fields before you ever see it.

You approve the plan before anything is built, and what you are asked to approve is a short prose summary of the judgment calls rather than the full file inventory. On approval the mechanical plan lands on the ticket as the newest execution plan version, stamped with the commit it was planned against; nothing already posted is edited, and an earlier version stays readable as history. That plan comment is the only thing written — the ticket body is left untouched, so a reporter's repro steps or a PM's acceptance notes are never a target of the write, and the prose summary you just approved is not posted back as a second comment restating it. The task moves to `ready`, and the plan ends with a `/sy:ship` kickoff and a ship profile that names each phase's model explicitly (`START <model> / BUILD <model> / GATE <model> / effort <tier> / process <full|light>`), so `/sy:ship` passes each phase's model through as an actual override instead of inferring one.

Not every spec ends in a plan. When research shows the premise is already delivered, invalidated, or superseded, spec shelves the task instead: it posts the decisive evidence as a comment and closes the task rather than producing a plan for work that should not ship.

## 3. Ship

```text
/sy:ship <task>
```

`/sy:ship` builds the approved plan and takes it to a reviewable PR. It branches from fresh `origin/main` into its own worktree, created under the sibling `<repo>-worktrees/` directory by default or under `worktree.root` when set (first checking that `main` has not drifted from the commit the plan assumed), implements the plan in order, gets CI green, and has the independent `sy:gate` agent review the exact pushed commits in an isolated, read-only checkout. When the plan declared a pre-gate checkpoint, ship pauses only before the first GATE dispatch following a BUILD `done` — right after opening the draft PR, and again after any "request changes" round produces a new BUILD `done` — to ask you whether this looks like the right thing: a direction check on the channel the plan named, never a correctness review. A run whose plan declared none is unaffected. Fixes go back into the build worktree, and every new commit starts a new review scope. When the PR head, the CI-green commit, and the reviewed commit are the same commit, ship posts the evidence to the ticket, moves it to `in-review`, and stops.

`/sy:ship` never merges on its own.

## 4. Merge

Merging needs your explicit word. When you authorize it, ship re-verifies that the head still equals the CI-green and reviewed commits, checks whether the target branch moved since review, and merges atomically. It then sets the ticket `done` and removes only the worktrees it created under the worktree root. What shipped feeds the next `/sy:plan` round.

## Supporting commands

- `/sy:spike <problem>` — before committing to a project, run a reproducible throwaway experiment that measures the gain and the regression in both directions and ends with a verdict — pressure-tested by a `sy:debate` pass every time, not only when the result looks contested — plus, if it clears the bar, a ready-made `/sy:plan` brief.
- `/sy:pr [draft]` — create, promote, or tidy the current branch's PR, keeping the description short and preserving acceptance evidence in comments. `/sy:ship` drives this for you; run it directly for one-off PR work.
- `/sy:ci [fix]` — triage the current branch's CI: find the run covering the current head, diagnose failures, and (with `fix`) push and re-watch until green. Never merges.
- `/sy:explain <topic>` — understand a gnarly decision, bug, or system before acting on it. An isolated agent investigates, verifies every mechanism claim, and authors a layered explainer doc into the topic's scratch directory (`scratch_dir(<topic-slug>)`, reported back as a resolved path you can open); you then walk it one layer at a time, with checkpoints and live repros on challenge, ending at a decision rather than a lecture. Useful mid-session — e.g. right before authorizing a merge — or as its own named session for a clean slate (`/sy:explain <doc path>` re-runs a doc someone already authored). Never touches the tracker.
- `/sy:help <question>` — ask about Shipyard itself: which setting controls what, which model an agent uses, what a command does. Reads only the plugin's own docs (`configuration.md`, `agent-guide.md`, the skill/agent files) and cites the source; never touches the tracker or your code.
- `/sy:config` — inspect, validate, change, and reload this repo's configuration; shows every resolved value with the layer it came from.

## Naming every phase

Because spec and ship are separate long sessions, launch each with its own name so your session history reads as a timeline of the work:

```bash
claude -n "plan billing epic"        "/sy:plan add usage-based billing to the API"
claude -n "spec PROJ-123"            "/sy:spec PROJ-123"
claude -n "ship PROJ-123"            "/sy:ship PROJ-123"
```

## What lands on your tracker

Every shipped task leaves a paper trail on its ticket, each record its own comment: a human-readable `# Ship retrospective`, a standalone `# Claude Code usage` JSON log, a standalone `# Claude Code ship metrics` JSON log, and (on the full process tier) a secret-scanned session transcript — attached to the work item on Jira, or a private gist linked from the metrics comment on GitHub. `/sy:plan` and `/sy:spec` archive their sessions the same way. There is no manual `/export`.

## Cross-session memory

Separately from any ticket, Shipyard keeps a small, user-global memory store — one Markdown file per durable lesson (a CLI flag with inverted semantics, a silent model fallback, and the like) plus a greppable index. `/sy:plan` and `/sy:spec` read it during early research, and `/sy:ship` reads it at start; new lessons get written, at most a few per run, during ship's retrospective. A lesson later contradicted by direct observation is corrected or retired through the same tools (`memory_refute`), never hand-edited and never carried forward as if it still held. It lives at `memory.dir` (default `~/.claude/shipyard/memory`, see [`configuration.md`](configuration.md)) and is cross-repo by design, so a trap learned once does not have to be relearned in the next repo next month.
