# Contributing to Shipyard

Shipyard is a Claude Code plugin: a `plan → spec → ship` workflow over a pluggable issue tracker.
Contributions are small, verifiable, and keep the tracker seam clean. This guide is the short version.

## Dev loop

1. Edit the skills, agents, docs, or adapter files.
2. Validate: `pixi run validate` — checks frontmatter, the agent return contracts, the promises skills make to each other, and the script self-tests. It must pass.
3. Run the MCP server's own test suite: `pixi run pytest`. It covers `sy_tools/` — the tool surface, both tracker adapters, config resolution, and the secret-scrub path. `validate.py` reads parts of that same tree as text — it requires files under `sy_tools/` to exist and content-checks `sy_tools/server.py` and both adapters — but executes none of it, so neither suite substitutes for the other. Lint and type-check the same tree: `pixi run ruff check sy_tools/` and `pixi run ty check sy_tools/`. CI enforces all four; run them locally rather than finding out from a failed check.
4. Try the change in a dev build (below).
5. Commands are namespaced by the plugin name (`sy`): `/sy:plan`, `/sy:spec`, `/sy:ship`, `/sy:spike`, `/sy:mvp`, `/sy:shutdown`, `/sy:pr`, `/sy:ci`, `/sy:explain`, `/sy:whiteboard`, `/sy:help`, `/sy:init-repo`, `/sy:config`.

Keep prose (READMEs, roadmaps, docs) clear and unwrapped; keep machine-facing text (agent briefs, contracts, JSON logs) terse and structured. How hard to cut either, and the two tests to cut by, is `skills/shared/references/context-economy.md`; `/sy:tighten` is the pass that applies it to one drafted piece of either kind.

## Dev builds

**Your checkout, for one session.** `claude --plugin-dir /path/to/shipyard` loads the checkout for that session as `sy@inline`. If `sy@shipyard` is installed, the session loads the checkout and skips the installed copy, because both are named `sy` in `plugin.json`. Run `/reload-plugins` in the session to pick up edits. A shell alias such as `alias claude-sy='claude --plugin-dir /path/to/shipyard'` makes it your default.

**A release candidate.** Pin the marketplace to a branch, a tag, or a full 40-character commit SHA, then install from it:

```bash
claude plugin marketplace remove shipyard   # only if already added; this also uninstalls sy@shipyard
claude plugin marketplace add nearmap/shipyard#<branch|tag|sha>
claude plugin install sy@shipyard --scope user
```

`@<ref>` works the same as `#<ref>`. A SHA pin needs Claude Code 2.1.325 or later; older clients cannot clone it. A branch or tag pin moves to that ref's latest commit on `claude plugin marketplace update shipyard`, and `claude plugin update` then installs it only if `version` moved. A SHA pin stays put. To go back, run the same three commands with plain `nearmap/shipyard`.

The `remove` is required: Claude Code refuses to re-add `shipyard` from a different ref (`its source doesn't match its extraKnownMarketplaces entry`). Removing the marketplace uninstalls `sy@shipyard`, but `sy` keeps no plugin options or data, so reinstalling loses nothing.

**No second local marketplace.** A marketplace is keyed by the `name` in its `marketplace.json`, and two cannot both be `shipyard`. `claude plugin marketplace add /path/to/shipyard` does not add a second one. It re-points your existing `shipyard` at the checkout, and an installed `sy@shipyard` then updates from your working tree. Use `--plugin-dir` instead.

Test every new install or marketplace command in a throwaway config before you document it: `CLAUDE_CONFIG_DIR=$(mktemp -d) claude plugin ...` never touches your real `~/.claude`.

## Declined ideas

Before proposing a change, check `.out-of-scope/`: one file per idea Shipyard deliberately declined, with the reason and every request that raised it. A declined idea can come back when its reason no longer holds; say why and update or delete the file in the same PR. When you decline a proposal for a durable reason, record it there.

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

Each verb has **exactly one** documented, implemented path: the MCP tool of the same name. There is deliberately no parallel CLI recipe for a tracker operation anywhere outside `sy_tools/` — a second path is a second thing to keep correct, and the recipes this replaced had drifted into a truncating read that looked like it worked. If you need tracker-specific behaviour, it belongs in an adapter, reached through a contract verb.

Pull requests are not tracker objects: they live on the code host whichever tracker a repository uses. So `sy_tools/pr/` and its tests may call `gh`, for pull requests only, and `sy_tools/tests/test_tracker_seam.py` exempts that one token there and nothing else; the code host's name and every tracker name still fail the scan in both places.

## Where a test lives

Tests mirror the package they cover from one root: `sy_tools/tests/`, never co-located with their source. `sy_tools/tests/test_layout.py` enforces it. `sy_tools/tests/tracker/` mirrors `sy_tools/tracker/` and is the one place a test may name a concrete tracker — an adapter's tests have to, which is the exemption `sy_tools/tests/test_tracker_seam.py` documents and scopes to that directory. A test written beside its adapter would silently keep passing while sitting outside the mirror, which is why the layout is asserted rather than assumed.

## Adding a new tracker adapter

1. Create `skills/tracker/<name>/ADAPTER.md` and `sy_tools/tracker/<name>/adapter.py`, one per zone above.
2. Implement **every** contract verb from `CONTRACT.md` on the `TrackerAdapter` protocol in `sy_tools/tracker/__init__.py`, mapping each to the native system, and document what the native system does with it in `ADAPTER.md`. Document any deliberate asymmetry (e.g. GitHub's attachments are a private gist, not a native file; its `done` transition is native project automation). `sy_tools/tests/tracker/test_canonical.py` fails the build on a missing verb.
3. Include a **status mapping table** (canonical → native) and a **type mapping table** (`epic`/`task`/`bug` → native), matching the existing adapters' layout (`jira/ADAPTER.md`, `github/ADAPTER.md`).
4. Keep all tracker-native names, helper scripts, and node-id juggling inside `skills/tracker/<name>/` — never in core.
5. Declare the adapter's required config in `skills/tracker/<name>/config-map.json` (`legacy_env`, `required`, `secret_env`) so the `validate_config` tool enforces it, and fail fast when it is missing. Select it at runtime with `"tracker": "<name>"` in `.shipyard/config.json`.
6. Verify every write by reading it back; treat empty results or errors as failure.

## Before every PR

- Run `pixi run validate`, `pixi run pytest`, `pixi run ruff check sy_tools/`, and `pixi run ty check sy_tools/`, and make sure all four pass.
- `docs/smoke_mcp.py` exercises every canonical verb live, against whichever tracker `.shipyard/config.json` configures, through the real MCP server (it creates real issues on a real board — read its header first and point it at a scratch project).
- Keep PR descriptions short: the diff shows *what* changed; the description explains *why*.
- If the change is consumer-visible (anything under `skills/`, `agents/`, `hooks/`, `sy_tools/`, `scripts/`, or `docs/`), bump `version` in `.claude-plugin/plugin.json`. `claude plugin update` gates entirely on that string, not on the git SHA — it will happily keep the marketplace clone fetched to the latest commit while reporting "already at the latest version" forever if the version never moves, and installed copies stay pinned to stale content at their old version-keyed cache path. After merging, tag the release with `claude plugin tag --push` so the tag and `plugin.json` agree.
