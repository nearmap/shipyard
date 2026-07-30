# Contributing to Shipyard

Shipyard is a Claude Code plugin: a `plan → spec → ship` workflow over a pluggable issue tracker.
Contributions are small, verifiable, and keep the tracker seam clean. This guide is the short version.

## Dev loop

1. Edit the skills, agents, docs, or adapter files.
2. Validate: `python scripts/validate.py` — checks frontmatter, the agent return contracts, the promises skills make to each other, and the script self-tests. It must pass.
3. Load the plugin locally to try it: `claude --plugin-dir /path/to/shipyard`.
4. Commands are namespaced by the plugin name (`sy`): `/sy:plan`, `/sy:spec`, `/sy:ship`, `/sy:spike`, `/sy:pr`, `/sy:ci`, `/sy:explain`, `/sy:help`, `/sy:init-repo`, `/sy:config`.

Keep prose (READMEs, roadmaps, docs) clear and unwrapped; keep machine-facing text (agent briefs, contracts, JSON logs) terse and structured.

## The tracker seam rule

The tracker is pluggable. There is exactly one place that knows how to talk to a specific tracker: `skills/tracker/<name>/`.

**Core files — everything outside `skills/tracker/` — must not name a concrete tracker or its CLI.**
No `jira`, `acli`, `gh issue`, `gh-project`, ADF, or any other tracker-native term leaks into a skill, agent, or script that lives outside `skills/tracker/`.
Core speaks **only** the canonical vocabulary in `skills/tracker/CONTRACT.md`: the verbs (`create-issue`, `create-child`, `get-issue`, `update-issue`, `find-issues`, `set-status`, `assign`, `link-parent`, `add-dependency`, `add-label`, `post-comment`, `post-log`, `attach-artifact`, `link-pr`), the canonical statuses, and the canonical types.

The validator enforces this seam — a stray tracker-native name in a core file fails `scripts/validate.py`. If you need tracker-specific behaviour, it belongs in an adapter, reached through a contract verb.

## Adding a new tracker adapter

1. Create `skills/tracker/<name>/ADAPTER.md`.
2. Implement **every** contract verb from `CONTRACT.md`, mapping each to the native system. Document any deliberate asymmetry (e.g. GitHub's transcript attachment is a private gist, not a native file; its `done` transition is native project automation).
3. Include a **status mapping table** (canonical → native) and a **type mapping table** (`epic`/`task`/`bug` → native), matching the existing adapters' layout (`jira/ADAPTER.md`, `github/ADAPTER.md`).
4. Keep all tracker-native names, helper scripts, and node-id juggling inside `skills/tracker/<name>/` — never in core.
5. Declare the adapter's required config in `skills/tracker/<name>/config-map.json` (`legacy_env`, `required`, `secret_env`) so `sy_config.py validate` enforces it, and fail fast when it is missing. Select it at runtime with `"tracker": "<name>"` in `.shipyard/config.json`.
6. Verify every write by reading it back; treat empty results or errors as failure.

## Before every PR

- Run `python scripts/validate.py` and make sure it passes.
- For the GitHub adapter, `docs/smoke_github.sh` exercises every verb against a scratch repo/board (it creates real issues — read its header first).
- Keep PR descriptions short: the diff shows *what* changed; the description explains *why*.
- If the change is consumer-visible (anything under `skills/`, `agents/`, `hooks/`, `scripts/`, or `docs/`), bump `version` in `.claude-plugin/plugin.json`. `claude plugin update` gates entirely on that string, not on the git SHA — it will happily keep the marketplace clone fetched to the latest commit while reporting "already at the latest version" forever if the version never moves, and installed copies stay pinned to stale content at their old version-keyed cache path. After merging, tag the release with `claude plugin tag --push` so the tag and `plugin.json` agree.
