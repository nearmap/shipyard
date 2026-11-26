# Contributing to Shipyard

Shipyard is a Claude Code plugin: a `plan → spec → ship` workflow over a pluggable issue tracker.
Contributions are small, verifiable, and keep the tracker seam clean. This guide is the short version.

## Dev loop

1. Edit the skills, agents, docs, or adapter files.
2. Validate: `pixi run validate` — checks frontmatter, the agent return contracts, the promises skills make to each other, and the script self-tests. It must pass.
3. Run the MCP server's own test suite: `pixi run pytest`. It covers `sy_tools/` — the tool surface, both tracker adapters, config resolution, and the secret-scrub path. `validate.py` reads parts of that same tree as text — it requires files under `sy_tools/` to exist and content-checks `sy_tools/server.py` and both adapters — but executes none of it, so neither suite substitutes for the other. Lint and type-check the same tree: `pixi run ruff check sy_tools/` and `pixi run ty check sy_tools/`. CI enforces all four; run them locally rather than finding out from a failed check.
4. Load the plugin locally to try it: `claude --plugin-dir /path/to/shipyard`.
5. Commands are namespaced by the plugin name (`sy`): `/sy:plan`, `/sy:spec`, `/sy:ship`, `/sy:spike`, `/sy:mvp`, `/sy:shutdown`, `/sy:pr`, `/sy:ci`, `/sy:explain`, `/sy:whiteboard`, `/sy:help`, `/sy:init-repo`, `/sy:config`.
6. In this repo, tracker, site, board and column values go in `.shipyard/config.local.json`, never `.shipyard/config.json`: that file is tracked in a public repo and keeps only `$schema` and `skills` (`docs/standards/SY-005-public-repo.md`). This overrides the `/sy:init-repo` and preflight remediation, which write to `config.json`.

## Standards

Shipyard's own rules are the parts in `docs/standards/`, one file per rule: how text is written and what it loads (applying `skills/shared/references/context-economy.md`), how skills and agents are written, comments and docstrings, where tests live, and the plugin version bump. Read them before changing any of those. Spec-gate and gate cite them through the repo-local `shipyard-standards` skill that `.shipyard/config.json` names; `/sy:tighten` applies the text rules to one drafted piece. After pulling a change to them, restart the session and run `reload_config`: skills load at session start, and the server caches config.

## Declined ideas

Before proposing a change, check `.out-of-scope/`: one file per idea Shipyard deliberately declined, with the reason and every request that raised it. A declined idea can come back when its reason no longer holds; say why and update or delete the file in the same PR. When you decline a proposal for a durable reason, record it there.

## The tracker seam rule

The tracker is pluggable. Tracker-native knowledge lives in **two zones, each independently enforced**, and they are not interchangeable:

| Zone | Holds | Enforced by |
|---|---|---|
| `skills/tracker/<name>/` | the adapter docs: what this tracker does with each canonical verb | `scripts/validate.py::check_seam` |
| `sy_tools/tracker/<name>/` | the adapter code the `sy` MCP server dispatches to | `sy_tools/tests/test_tracker_seam.py` |

Neither zone is the other's fallback and neither may absorb the other: the docs zone explains behaviour to a reader, the code zone performs it, and a tracker name that leaks out of either is a build failure from a different check. `sy_tools/tracker/__init__.py`'s `adapter()` is the single selection point on the code side, exactly as `skills/tracker/CONTRACT.md` is on the docs side.

**Core files — everything outside those two zones — must not name a concrete tracker or its CLI.**
No `jira`, `acli`, `gh issue`, `gh-project`, ADF, or any other tracker-native term leaks into a skill, agent, script, or `sy_tools/` module outside its zone.
Core speaks **only** the canonical vocabulary in `skills/tracker/CONTRACT.md`: the verbs (`preflight`, `create-issue`, `create-child`, `get-issue`, `update-issue`, `find-issues`, `set-status`, `assign`, `link-parent`, `add-dependency`, `add-label`, `type-convert`, `post-comment`, `post-log`, `attach-artifact`, `attachment-download`, `attachment-update`, `link-pr`), the canonical statuses, and the canonical types.

Each verb has **exactly one** documented, implemented path: the MCP tool of the same name. There is deliberately no parallel CLI recipe for a tracker operation anywhere outside `sy_tools/` — a second path is a second thing to keep correct, and the recipes this replaced had drifted into a truncating read that looked like it worked. If you need tracker-specific behaviour, it belongs in an adapter, reached through a contract verb.

## Adding a new tracker adapter

1. Create `skills/tracker/<name>/ADAPTER.md` and `sy_tools/tracker/<name>/adapter.py`, one per zone above.
2. Implement **every** contract verb from `CONTRACT.md` on the `TrackerAdapter` protocol in `sy_tools/tracker/__init__.py`, mapping each to the native system, and document what the native system does with it in `ADAPTER.md`. Document any deliberate asymmetry (e.g. GitHub's attachments are a private gist, not a native file; its `done` transition is native project automation). `sy_tools/tests/tracker/test_canonical.py` fails the build on a missing verb.
3. Include a **status mapping table** (canonical → native) and a **type mapping table** (`epic`/`task`/`bug` → native), matching the existing adapters' layout (`jira/ADAPTER.md`, `github/ADAPTER.md`).
4. Keep all tracker-native names, helper scripts, and node-id juggling inside `skills/tracker/<name>/` — never in core.
5. Declare the adapter's required config in `skills/tracker/<name>/config-map.json` (`legacy_env`, `required`, `secret_env`) so the `validate_config` tool enforces it, and fail fast when it is missing. Select it at runtime with `"tracker": "<name>"` in `.shipyard/config.local.json` (Dev loop step 6).
6. Verify every write by reading it back; treat empty results or errors as failure.

## Before every PR

- Run `pixi run validate`, `pixi run pytest`, `pixi run ruff check sy_tools/`, and `pixi run ty check sy_tools/`, and make sure all four pass.
- `docs/smoke_mcp.py` exercises every canonical verb live, against the tracker `.shipyard/config.local.json` configures (Dev loop step 6), through the real MCP server (it creates real issues on a real board — read its header first and point it at a scratch project).
- Keep PR descriptions short: the diff shows *what* changed; the description explains *why*.
- If the change is consumer-visible, bump the plugin version per `docs/standards/SY-012-version-bump.md`.
