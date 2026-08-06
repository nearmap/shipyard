![Shipyard: a roadmap plotted once, above a dry dock that repeats plan → build → launch per task](docs/img/background_1.png)

# Shipyard — `/sy:plan → /sy:spec → /sy:ship`

Shipyard is a Claude Code plugin that takes an objective from "we should build this" to a merged, independently reviewed pull request — with the whole trail recorded on your issue tracker, **Jira or GitHub Projects**. You plan a roadmap once, then repeat a short loop per task: spec it, ship it, merge it. Claude does the building and the reviewing; you approve the plan and authorize the merge.

**Already running Claude Code?** Paste this and let it drive the setup:

```text
Help me install and configure Shipyard in this repo. Read https://raw.githubusercontent.com/nearmap/shipyard/main/agent-guide.md first, then walk me through it step by step.
```

## What you get

Point Shipyard at a task and it produces a PR that is ready to merge: the change is built to an approved plan, CI is green, and an independent reviewer has signed off on the exact commits you are about to merge. Alongside the PR, the ticket carries the full paper trail — the plan it was built against, a retrospective, token and outcome logs, and the session transcript — so the "why" survives long after the diff is gone. Only you merge, and nothing merges without your explicit word.

![The delivery loop](docs/img/delivery-loop.png)

## Why it works this way

Two convictions shape everything, and both exist to earn your trust in the output.

**The review is adversarial, and it reviews exactly what you'll merge.** A separate `sy:gate` agent — running on a frontier-tier model, in its own read-only checkout pinned to the pushed commits — reviews the plan's obligations and design invariants, not just the diff, and every bug it suspects must survive an attempt to refute it before it is reported. If a fix is pushed, the review scope resets: the PR head, the CI-green commit, and the reviewed commit must be the *same commit* before anything hands off or merges. You are never reviewing one thing and merging another.

![The immutable review gate](docs/img/immutable-gate.png)

**Context holds decisions, not noise.** Reading fifty files to answer one question is fine — but it happens inside a disposable agent, and what comes back is a short brief of pointers backed by checkable evidence, not the raw transcript. The orchestrator stays clear-headed across a long build because it holds compact briefs rather than everything it read. In practice that means sharper decisions late in a task, not just a cheaper one.

![The compression boundary](docs/img/compression-boundary.png)

## The loop

| What you have | What you type |
|---|---|
| a repo that hasn't been configured for Shipyard yet, or a teammate joining one that has | `/sy:init-repo` |
| checking, changing, or reloading what Shipyard is configured to do here | `/sy:config` |
| a large objective or an existing roadmap | `/sy:plan` |
| one PR-sized task that needs an executable plan | `/sy:spec <task>` |
| an approved plan ready to build | `/sy:ship <task>` |
| a hunch that needs data before it becomes a project | `/sy:spike …` |
| a branch that needs its PR created or tidied | `/sy:pr` |
| a failing or pending CI run to diagnose | `/sy:ci` |
| a decision, bug, or system you want to genuinely understand before acting on it | `/sy:explain …` |
| a question about Shipyard itself — a command, a config knob, which model an agent uses | `/sy:help …` |

`<task>` is an issue ID — a Jira key like `PROJ-123` or a GitHub issue like `#123`. Shipyard never parses IDs; it passes them straight through to the configured tracker.

**Tip:** each command runs a long session, so name it at launch — `claude -n "spec PROJ-123 add billing" "/sy:spec PROJ-123"` — and it is easy to find again later. See [`docs/usage.md`](docs/usage.md).

### Plan

`/sy:plan` interviews you one question at a time, maps the code with read-only agents, and writes a living roadmap onto one Epic. Executable work becomes direct child tasks, each sized to one coherent PR; at most a configured cap (`plan.max_active_tasks`) are active at once, and everything further out stays as text until it is close enough to spec. Before the ladder is built, the roadmap's shape goes through a bounded proposer/adversary debate — every time, not only when the shape looks contested — and you steer the disagreement rather than the plan quietly picking one. Re-enter with `/sy:plan <epic>` to read what shipped and reshape the roadmap.

![The roadmap model](docs/img/jira-roadmap.png)

### Spec

`/sy:spec <task>` reads the ticket and the code, resolves the repo's engineering standards, and writes a complete plan: the approach and the strongest rejected alternative (pressure-tested by the same proposer/adversary debate, which runs on every plan before sign-off), ordered changes with file anchors, tests and acceptance criteria, and a verification obligation — a claim plus the named evidence that will prove it — for every risk lens the work activates. Before you see it, a separate `sy:spec-gate` reviewer reads the drafted plan for architecture, simplicity and correctness, and for the two things plans quietly omit: which docs the change makes stale, and which figures or screenshots need a visual check. What you are then asked to approve is a short prose summary — the judgment calls, not the file inventory; the full mechanical plan lands on the ticket as the single ACTIVE plan, stamped with the commit it was planned against, once you have said yes. Not every spec ends in a plan: when research shows the premise is already delivered, invalidated, or superseded, spec shelves the task with evidence instead of building on a premise that no longer holds.

![Verification obligations](docs/img/verification-obligations.png)

### Ship

`/sy:ship <task>` builds the approved plan to a reviewable PR. It branches from fresh `origin/main` into its own worktree, implements the plan in order, discharges each verification obligation with its named evidence, gets CI green, and runs the immutable gate above. When head, CI-green, and reviewed commits converge, it posts the evidence, moves the task to `in-review`, and stops. You merge; then what shipped feeds the next planning round.

![The ship dispatcher](docs/img/ship-states.png)

## Under the hood

The workflow skills stay small and delegate expensive reads and builds to a fleet of specialist agents, each in its own context with its own model, each returning a compact brief. `/sy:ship` in particular runs each phase — start, build, gate — as a disposable worker, so the orchestrator owns only the durable state and the conversation with you, while the heavy lifting happens in fresh contexts that never accumulate.

![The agent fleet](docs/img/agent-fleet.png)

The issue tracker is the one pluggable part. Core skills and agents speak a single [tracker contract](skills/tracker/CONTRACT.md) — canonical verbs, five lifecycle statuses (named per repo), and canonical types — and a thin adapter maps that to Jira or to GitHub Projects. The `tracker` config key selects one, and a validator keeps tracker-specific vocabulary out of every core file. The GitHub tracker needs **no organization**: it drives issue Type and Status as Projects v2 fields, which work the same on a personal board.

Some lessons outlive a single ticket — a CLI flag with inverted semantics, a model that silently falls back to a default. Those go into a small, user-global memory store (the `sy` server's memory tools over one file per lesson plus a greppable index) rather than a ticket comment or a repo's `CLAUDE.md`. `/sy:plan` and `/sy:spec` read it during early research and `/sy:ship` at start; new lessons get written, at most a few at a time, during ship's retrospective. A lesson later contradicted by direct observation is corrected or retired through the same tools (`memory_refute`), never hand-edited and never carried forward. It is cross-repo by design, so a trap learned once does not have to be relearned in the next repo next month.

## Install and configure

Shipyard is a plugin, so it is loaded, not symlinked:

```bash
claude --plugin-dir /path/to/shipyard               # this session only

# persistent: this repo ships a marketplace manifest, so add it, then install the plugin
claude plugin marketplace add nearmap/shipyard   # a GitHub owner/repo (cloned for you) or a local path
claude plugin install sy@shipyard --scope project   # shared with this repo via .claude/settings.json (recommended)
```

`--scope project` (recommended) declares intent in this repo's own `.claude/settings.json`, shared with collaborators via git. `--scope local` is the same idea but written to this repo's gitignored `.claude/settings.local.json` instead — just for you. Drop the flag and Claude Code installs to **user** scope instead: every project on your machine, not just this one. See [`docs/installation.md`](docs/installation.md#choose-an-install-scope) for the full scope table and the team-rollout story (a fresh clone still needs the marketplace itself known, and each collaborator confirms their own install).

`./install.sh` validates the plugin and prints these instructions with a tracker-aware preflight. The details live in the docs:

- [`docs/installation.md`](docs/installation.md) — loading the plugin and the CLI tools it needs.
- [`docs/configuration.md`](docs/configuration.md) — every setting, the three-layer merge chain, per-agent model floors, and how to retire the old `env` block.
- [`docs/github-setup.md`](docs/github-setup.md) — one-time GitHub Projects board setup.
- [`docs/usage.md`](docs/usage.md) — the day-to-day loop and the session-naming pattern.

## The rules it holds itself to

- exactly one execution plan is ACTIVE per task; a newer plan supersedes the old one explicitly;
- plans record the commit they were written against; building on a materially drifted base is refused;
- every agent that writes code gets its own isolated worktree, kept beside the repo (or wherever `worktree.root` points) so parallel work never collides;
- `sy:gate` reviews pinned base/head commits in an isolated, read-only checkout, and every bug candidate must survive an adversarial refutation before it is reported;
- the PR head, the CI-green commit, and the reviewed commit must be identical before handoff or merge;
- nothing merges without direct user authorization.

## Layout

```text
shipyard/
  .claude-plugin/plugin.json      # name: sy, plus the version `claude plugin update` gates on
  hooks/hooks.json                # review guard + usage accounting (plugin-level)
  sy_tools/                       # the sy MCP server: the tool surface, config resolution, guards, tracker adapters
  scripts/                        # what a bash/hook path still needs: validate.py, ci_poll.sh
  agents/                         # sweep seam trace slice hunt gate spec-gate img-inspector explain-author debate debater ship-{start,build,gate}
  skills/
    plan/ spec/ ship/ spike/ pr/ ci/ standards/ explain/ help/
    tracker/
      SKILL.md CONTRACT.md        # the seam: selection + canonical vocabulary
      jira/    ADAPTER.md + references/
      github/  ADAPTER.md
  docs/
    installation.md configuration.md usage.md github-setup.md smoke_mcp.py img/
```

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Run `python scripts/validate.py` before every PR; the seam and contract-completeness checks are not optional.

![The four guarantees that hold end to end: adversarial review, context compression, verification obligations, and you merge](docs/img/background_2.png)
