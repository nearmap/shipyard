# Contributing to Shipyard

Shipyard is a Claude Code plugin: a `plan → spec → ship` workflow over a pluggable issue tracker.
Contributions are small, verifiable, and keep the tracker seam clean. This guide is the short version.

## Dev loop

1. Edit the skills, agents, docs, or adapter files.
2. Validate: `pixi run validate` — checks frontmatter, the agent return contracts, the promises skills make to each other, and the script self-tests. It must pass.
3. Run the MCP server's own test suite: `pixi run pytest`. It covers `sy_tools/` — the tool surface, both tracker adapters, config resolution, and the secret-scrub path. `validate.py` reads parts of that same tree as text — it requires files under `sy_tools/` to exist, content-checks `sy_tools/server.py` and both adapters, and scans the tree for seam violations — but executes none of it, so neither suite substitutes for the other. Lint and type-check the same tree: `pixi run ruff check sy_tools/` and `pixi run ty check sy_tools/`. CI enforces all four; run them locally rather than finding out from a failed check.
4. Load the plugin locally to try it: `claude --plugin-dir /path/to/shipyard`.
5. Commands are namespaced by the plugin name (`sy`): `/sy:plan`, `/sy:spec`, `/sy:ship`, `/sy:spike`, `/sy:pr`, `/sy:ci`, `/sy:explain`, `/sy:help`, `/sy:init-repo`, `/sy:config`.

Keep prose (READMEs, roadmaps, docs) clear and unwrapped; keep machine-facing text (agent briefs, contracts, JSON logs) terse and structured. How hard to cut either, and the two tests to cut by, is `skills/shared/references/context-economy.md`; `/sy:tighten` is the pass that applies it to one drafted piece of either kind.

## Comments and docstrings

Two audiences, two places, and the split between them is the whole rule. A **docstring** is written for a caller: what this does and what its contract is, just enough and no more. A **comment** is written for the next person editing this code: the one thing they cannot recover by reading it.

- A docstring that restates the signature, re-lists parameters the type hints already name, or narrates the implementation is worse than no docstring — it is a second source of truth that no test checks and that drifts silently. Public functions, tool surfaces, and modules get one; a private helper whose name and types already say it does not.
- A genuinely non-obvious **why** does not belong in the docstring. Move it to an inline comment at the exact line it explains, where the reader meets the surprise, rather than in a summary they read before they need it.
- Every comment earns its keep individually. It is untested and untestable, nothing fails when the code beside it changes, and it therefore costs more to maintain than that code. Write one where a reader would otherwise "simplify" something and break it invisibly: a measured result, a rejected alternative and the reason it was rejected, an ordering that is load-bearing, a workaround for behaviour outside this repo.
- One line by default. A paragraph has to be carrying a paragraph's worth of *why*.
- Restate nothing the code already says.
- **No ticket or PR references.** They rot into a lookup the reader cannot perform and a claim they cannot check; the history is in git. The single exception is a literal `TODO`, where naming the ticket is the point.

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

Each verb has **exactly one** documented, implemented path: the MCP tool of the same name. There is deliberately no parallel CLI recipe for a tracker operation anywhere outside `sy_tools/` — a second path is a second thing to keep correct, and the recipes this replaced had drifted into an inverted link direction and a truncating read that both looked like they worked. If you need tracker-specific behaviour, it belongs in an adapter, reached through a contract verb.

## Where a test lives

Tests mirror the package they cover from one root: `sy_tools/tests/`, never co-located with their source. `sy_tools/tests/test_layout.py` enforces it. `sy_tools/tests/tracker/` mirrors `sy_tools/tracker/` and is the one place a test may name a concrete tracker — an adapter's tests have to, which is the exemption `sy_tools/tests/test_tracker_seam.py` documents and scopes to that directory. A test written beside its adapter would silently keep passing while sitting outside the mirror, which is why the layout is asserted rather than assumed.

## Adding a new tracker adapter

1. Create `skills/tracker/<name>/ADAPTER.md` and `sy_tools/tracker/<name>/adapter.py`, one per zone above.
2. Implement **every** contract verb from `CONTRACT.md` on the `TrackerAdapter` protocol in `sy_tools/tracker/__init__.py`, mapping each to the native system, and document what the native system does with it in `ADAPTER.md`. Document any deliberate asymmetry (e.g. GitHub's transcript attachment is a private gist, not a native file; its `done` transition is native project automation). `sy_tools/tests/tracker/test_canonical.py` fails the build on a missing verb.
3. Include a **status mapping table** (canonical → native) and a **type mapping table** (`epic`/`task`/`bug` → native), matching the existing adapters' layout (`jira/ADAPTER.md`, `github/ADAPTER.md`).
4. Keep all tracker-native names, helper scripts, and node-id juggling inside `skills/tracker/<name>/` — never in core.
5. Declare the adapter's required config in `skills/tracker/<name>/config-map.json` (`legacy_env`, `required`, `secret_env`) so the `validate_config` tool enforces it, and fail fast when it is missing. Select it at runtime with `"tracker": "<name>"` in `.shipyard/config.json`.
6. Verify every write by reading it back; treat empty results or errors as failure.

## Before every PR

- Run `pixi run validate`, `pixi run pytest`, `pixi run ruff check sy_tools/`, and `pixi run ty check sy_tools/`, and make sure all four pass.
- `docs/smoke_mcp.py` exercises every canonical verb live, against whichever tracker `.shipyard/config.json` configures, through the real MCP server (it creates real issues on a real board — read its header first and point it at a scratch project).
- Keep PR descriptions short: the diff shows *what* changed; the description explains *why*.
- If the change is consumer-visible (anything under `skills/`, `agents/`, `hooks/`, `sy_tools/`, `scripts/`, or `docs/`), bump `version` in `.claude-plugin/plugin.json`. `claude plugin update` gates entirely on that string, not on the git SHA — it will happily keep the marketplace clone fetched to the latest commit while reporting "already at the latest version" forever if the version never moves, and installed copies stay pinned to stale content at their old version-keyed cache path. After merging, tag the release with `claude plugin tag --push` so the tag and `plugin.json` agree.
