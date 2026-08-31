# Shipyard Agent Guide

## What Shipyard is

Shipyard is a Claude Code plugin that runs a `plan → spec → ship` delivery loop against an issue tracker (Jira or GitHub Projects). It turns an objective into a merged, independently reviewed pull request, with the plan, an adversarial review, and a paper trail recorded on the ticket. The user approves the plan and authorizes every merge; Shipyard does the building and the reviewing.

## Concept model

- **Tracker**: the pluggable backend — Jira or GitHub Projects — selected by the `tracker` config key. Core skills never speak tracker-native vocabulary; only `skills/tracker/<name>/` does.
- **Five lifecycle columns**: `backlog → ready → in-progress → in-review → done`, mapped to whatever the user's board actually calls them via the `columns.*` config keys. `blocked` is not a column — it's a tracker-native dependency relationship.
- **Plan**: a living roadmap on one Epic, produced by `/sy:plan`. At most a configured cap (`plan.max_active_tasks`) child tasks are active at once.
- **Spec**: a versioned execution plan per task, whose highest version is the plan, produced by `/sy:spec <task>` and approved by the user before anything is built. It's stamped with the commit it was planned against. Not every spec ends in a plan — when research shows the premise is already delivered, invalidated, or superseded, spec shelves the task with evidence instead.
- **Ship**: `/sy:ship <task>` builds the plan in its own worktree, gets CI green, and runs `sy:gate` — an independent reviewer on a frontier model, in an isolated read-only checkout, that must refute-test every bug candidate before reporting it. Head, CI-green, and reviewed commits must converge before it stops.
- **Merge**: never automatic. The user's explicit word is required every time.
- **Cross-session memory**: a small, user-global store (the `sy` server's memory tools, rooted at `memory.dir`) of durable lessons — a CLI flag with inverted semantics, a silent model fallback — that outlive any one ticket or repo. `/sy:plan` and `/sy:spec` read it early; `/sy:ship` writes to it at the retrospective. A lesson later contradicted by direct observation is corrected or retired through the same tools (`memory_refute`), never hand-edited.

## Installing

Two ways to load the plugin — pick based on whether this is a one-off trial or a persistent setup. For a persistent setup, **ask the user first**: install scoped to this repo (shared with collaborators via git, or just this checkout), or globally across every project on their machine? Don't default to global silently.

```bash
# one session only, from a local checkout — no scope choice needed
claude --plugin-dir /path/to/shipyard

# persistent, straight from GitHub (no clone needed)
claude plugin marketplace add nearmap/shipyard    # always registers globally; install scope is chosen below
claude plugin install sy@shipyard --scope project     # shared with collaborators via .claude/settings.json
claude plugin install sy@shipyard --scope local       # just this checkout, gitignored (.claude/settings.local.json)
claude plugin install sy@shipyard                     # global (default): every project on this machine
```

`--scope project`/`local` only writes intent (`enabledPlugins`) into the repo's own settings file — a fresh clone still needs the `shipyard` marketplace itself known and a one-time install confirmation from each collaborator (see `docs/installation.md`'s "Choose an install scope" for the exact snippet). There is no zero-touch team rollout, only a guided one.

`./install.sh` (run from a local checkout) validates the plugin's structure and runs a tracker-aware preflight against the current environment — do this after cloning:

```bash
cd /path/to/shipyard && ./install.sh
```

Required tools: Claude Code ≥ 2.1.218 (see `docs/installation.md` for the floor's rationale), Python 3.10+ on PATH as `python`, `gh` ≥ 2.94.0 authenticated (every tracker uses it for PRs/CI), and `gitleaks` (scans transcripts before they're attached/gisted). Jira additionally needs `acli`.

## Configuring a repo

Configuration is per-repo, in `.shipyard/config.json`. Environment variables are reserved for secrets — a setting left in the environment is a hard error, not an override. `docs/configuration.md` is the complete reference; never invent a key name, read it from there.

Required for every repo:

- `columns.backlog`, `columns.ready`, `columns.in_progress`, `columns.in_review`, `columns.done` — the five lifecycle columns, matched case-insensitively to the user's actual board.
- `tracker` — `jira` (default) or `github`.

Then, tracker-specific, under `tracker_config`:

- **Jira**: `tracker_config.site` and `tracker_config.project` in the committed layer; `tracker_config.email` in the gitignored `.shipyard/config.local.json` (it is per-person but not a secret). `ACLI_TOKEN` **is** a secret: it stays in the environment and never enters a config file.
- **GitHub**: `tracker_config.project` (`<owner>/<number>`, e.g. `@me/3`) required; `tracker_config.repo` only if issues live in a different repo than the one Claude runs in. The board needs a `Status` single-select (one option per column) and a `Type` single-select (`Epic`/`Task`/`Bug`) — see the repo's `docs/github-setup.md` to create them.

A repo still carrying the old `env` block moves off it by hand, and there is no converter to run: `validate_config` names every retired variable that is still set alongside the config key it now resolves to, so that report is the worklist — move each value into the right layer, then unset the variable, since leaving one set is a deliberate failure rather than an override. `/sy:init-repo` walks this interactively, and `docs/configuration.md` carries the full mapping.

Optional settings worth knowing: `models.tiers.frontier` (default `fable`) is what the `sy:gate` reviewer runs at, and what a ship profile's `frontier` resolves to for any phase that states it — a quality floor, set it to the strongest available model, not a cost dial. Per-agent bindings live under `models.agents.<name>.model` and take effect on the next dispatch; `config/floors.json` pins a floor per agent that no config can lower, so economising `sy:hunt` is fine and weakening `sy:gate` is refused by name. `skills.standards` and `skills.reviewer` name the repository's own standards and code-review skills, so `/sy:standards` treats the first as its authority and `sy:gate` runs the second as an additive pass alongside its own review; leave either unset and that pass is unchanged. `worktree.root` defaults to a sibling `<repo>-worktrees/` directory beside the repo. `memory.dir` (default `~/.claude/shipyard/memory`) relocates the cross-session memory store — it's user-global and cross-repo, so most setups never need to touch it. Per-agent **effort** is not settable at runtime; say so plainly if asked, rather than implying a config edit changes it.

Verify any setup with the `sy` server's config tools (tool names resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`) — `show_config {}` for every value plus the layer it came from, `validate_config {}` for every problem, each naming its key and layer. If the plugin is not loaded in this session yet, `./install.sh` from the checkout does both on the way through.

If the user chose `project` scope above and wants every collaborator to get Shipyard on clone without each person running `marketplace add` themselves, add `extraKnownMarketplaces` alongside `enabledPlugins` — see `docs/installation.md`'s "Choose an install scope" for the exact JSON and its caveats (each collaborator still confirms the install themselves; there is no zero-touch path).

### Ad-hoc setup questions: delegate the lookup, don't answer from memory

The user will ask setup questions mid-onboarding that this guide doesn't cover — "how do I generate ACLI_TOKEN," "where do I get a GitHub PAT," "what scopes does `gh` need." Don't answer from memory and don't fetch or read the answer inline. Dispatch a subagent (`WebFetch`/`WebSearch` via a plain `general-purpose` `Agent` dispatch, or `claude-code-guide` for a Claude-Code-specific question) to find the current instructions and return just the resolved answer. Two reasons, not one: credential and tool setup steps change upstream without notice, so a memorized answer can be stale in a way a fresh lookup isn't; and the raw page or search results belong in the subagent's throwaway context, not this session, the same discipline as the tracker/repo discovery below.

### Ask before you explore, then discover via a subagent, not inline

Ask the tracker and its identifiers up front (see step 2 below) — they're things the user already knows, and answering them costs nothing. Once the plugin is loaded, step 4 hands the rest to `/sy:init-repo`, which is where the remaining discovery actually happens: the tracker's *actual* lifecycle values (column/status names, GitHub's `Type`/`Status` field options), since the user may not recall a board's exact spelling. That discovery means querying the tracker (`gh project view`/`gh project field-list`, `acli jira project view`, and similar), which can return large listings, plus possibly checking the target repo for an existing `.claude/settings.json`. It delegates that to a subagent (`Explore`, or a plain `general-purpose` `Agent` dispatch) scoped to exactly the tracker and project/board the user named, and has it return just the resolved values — never run tracker-discovery commands or multi-file reads directly in an onboarding session. This is Shipyard's own compression-boundary principle (see "Why it works this way" in the README): onboarding should practice it, not be the one place that violates it. Asking first, rather than exploring blind, is what makes the subsequent discovery a single targeted lookup instead of an open-ended probe — true whether this guide's session or `/sy:init-repo` is the one doing it.

## First-run walkthrough

1. Give the user a short pitch — 2-3 sentences, not a wall of text. Don't paste "What Shipyard is"/"Concept model" at them verbatim; those sections are your background, not user-facing copy. For example: *"Shipyard turns an objective into a merged, independently-reviewed PR: plan a roadmap once, then per task it drafts a plan you approve, builds it, and gets an adversarial review before you merge — with the whole trail recorded on your tracker (Jira or GitHub Projects)."*
2. Before doing anything else, ask the user (batched, one round): which tracker (Jira or GitHub Projects), install scope (this repo only, or global — see "Installing"), and the identifiers only they know — Jira site/project key/email, or GitHub Projects owner/number. Nothing below should start until these are answered.
3. Load the plugin per the chosen scope (Installing, above).
4. Hand the rest of setup to the plugin's own wizard: `/sy:init-repo`, seeded with what step 2 already answered so it never re-asks. If the newly loaded plugin's commands aren't available in this session yet, say so and start a fresh one (`claude -n "init shipyard" "/sy:init-repo"`) rather than improvising the discovery/write steps by hand — `/sy:init-repo` discovers the board's actual lifecycle values, writes `.claude/settings.json`/`settings.local.json` split correctly between shared and secret, and proves the result live before declaring success, which this guide alone cannot do.
5. Name a session and start the loop: `claude -n "plan <objective>" "/sy:plan <objective>"`.
6. Answer `/sy:plan`'s interview questions; it writes the roadmap to one Epic.
7. Per task: `/sy:spec <task>` (review and approve the plan it proposes), then `/sy:ship <task>` (builds, gets CI green, gets gated).
8. When ship reports head/CI-green/reviewed convergence, authorize the merge explicitly — Shipyard never does this on its own.

## Diagnosis recipes

Once the plugin is loaded, a question about Shipyard itself — not the recipes below, but anything else, like "what setting controls the gate model" — is `/sy:help <question>`, not a guess from memory: it reads `docs/configuration.md`, this guide, and the relevant skill/agent files, and cites its source.

- Any command stops immediately with a named `## Action needed` block, before doing any real work: that's the tracker preflight — config is missing or present-but-dead (revoked token, login never done, wrong project key). The message names the exact gap; the fix is usually re-running `/sy:init-repo`, or (for a teammate joining an already-configured repo) just exporting their own personal credential in the environment.
- `/sy:ship` refuses to build: the plan's base commit has drifted from `origin/main` — re-run `/sy:spec` to refresh it.
- The reviewer seems to be running the wrong model: check `CLAUDE_CODE_SUBAGENT_MODEL` isn't set — it outranks the per-invocation model parameter and silently reroutes every agent off whatever the resolver decided. `validate_config` and `./install.sh` both fail on it. Otherwise call `agent_model {"name": "gate"}`, which reports the resolved model, what config asked for, and whether a floor clamped it.
- A tracker verb is missing or behaves oddly: it belongs in `skills/tracker/<name>/ADAPTER.md` — never in a core skill or agent.
- Ship stops before attaching a transcript: `gitleaks` isn't installed — it's a hard gate, not a warning.
- The GitHub tracker preflight fails on a missing `gh`: hard error for `"tracker": "github"` (soft warning for `jira`).

## Key rules for guidance

- Don't invent config key names, column values, or tracker fields — the authoritative list is `docs/configuration.md`; read it rather than guessing, or call `show_config {}` to see what this repo actually resolves.
- Never suggest merging on the user's behalf or imply a merge happened automatically — nothing merges without their explicit word, by design.
- Keep tracker vocabulary (`jira`, `acli`, `gh issue`, ADF, …) out of anything described as core when discussing the codebase — that seam is enforced by `scripts/validate.py`.
- Task/issue IDs (`PROJ-123`, `#123`) pass straight through to the tracker — don't reformat or reinterpret them.
- The highest execution plan version on a task is the plan, and no posted comment is edited — flag that a new `/sy:spec` run supersedes an existing unshipped plan rather than stacking silently.
- This is a Claude Code plugin: install it with `claude plugin` / `--plugin-dir`, never by copying or symlinking files into `~/.claude`.
- For a persistent install, ask whether it should be scoped to this repo or global before running `claude plugin install` — don't default to `--scope user` (global) silently; see "Installing" above.
- Ask the user's tracker, identifiers, and install scope before exploring anything; delegate what's left (tracker/board queries, surveying the target repo's existing config) to a subagent that returns a compact brief, never raw API output or multi-file reads in this onboarding session (see "Ask before you explore" above). Shipyard's whole pitch is context discipline; model it while installing it, not just after.
- Open with a short pitch, not the full "What Shipyard is"/"Concept model" text — those sections are your reference, not a script to recite.
- An ad-hoc question like "how do I generate ACLI_TOKEN" gets delegated to a subagent for the lookup, never answered from memory or researched inline (see "Ad-hoc setup questions" above).
