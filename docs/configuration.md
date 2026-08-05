# Configuration

Shipyard is configured through a layered JSON file that it owns and resolves itself. `sy_tools/config.py` is the only reader — the `sy` MCP server exposes it as the config tools below, and nothing else parses config, and nothing else supplies a default.

Environment variables are reserved for **secrets**. A setting left in the environment is an error, not an override — see [Migrating](#migrating-from-the-env-block).

Two tools answer "what is configured" (tool names resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`) — `show_config` reports every value with the layer it came from, `validate_config` reports every problem, each naming its key and layer:

```
show_config {}
validate_config {}
```

From a terminal rather than inside a session, `/sy:config` is the human-facing form: the same two reads with explanation.

## The three layers

Deep-merged, lowest precedence first. A later layer overrides a single key without replacing its siblings.

| Layer | File | Committed? | For |
|---|---|---|---|
| user-global | `~/.shipyard/config.json` | no | your preferences across every repo |
| repo-committed | `<repo>/.shipyard/config.json` | **yes** | shared by everyone who clones the repo |
| repo-local | `<repo>/.shipyard/config.local.json` | no (gitignore it) | per-person or per-machine values |

Put a value in the layer matching what it *is*. The tracker, the board, and the five column names are the same for every collaborator, so they belong in the committed layer. An account email or an absolute worktree path differs per person, so it belongs in the local layer — committing one breaks preflight for every teammate whose home directory or account differs.

Start a new file with `"$schema": "https://raw.githubusercontent.com/nearmap/shipyard/main/config/schema.json"` so your editor validates it as you type.

## Every setting

Shipped defaults live in exactly one place, `config/defaults.json` — this table documents what
each key does and whether it's required, not its current value, so the two can never disagree. For
what a key actually resolves to right now, in this repo, with every layer applied:

```
show_config {}
```

| Key | Required | What it does |
|---|---|---|
| `tracker` | yes | Which adapter under `skills/tracker/` to use. |
| `columns.backlog` | **yes** | Board column name for the `backlog` status. |
| `columns.ready` | **yes** | Board column name for `ready`. |
| `columns.in_progress` | **yes** | Board column name for `in-progress`. |
| `columns.in_review` | **yes** | Board column name for `in-review`. |
| `columns.done` | **yes** | Board column name for `done`. |
| `tracker_config.*` | adapter-specific | The selected adapter's own settings. Each adapter declares its required keys in its `config-map.json` and its `ADAPTER.md`. |
| `worktree.root` | no | Where `/sy:ship` builds worktrees. Unset derives a sibling `<repo>-worktrees/` directory beside the *main checkout*, resolved from the shared git directory so a build worktree cannot nest a second worktrees directory inside the first. Absolute paths only — a literal `~` is not expanded. |
| `memory.dir` | no | Cross-session memory store. Unset derives a directory under `~/.claude/shipyard/`. |
| `scratch.dir` | no | Root for per-identifier ephemeral working directories (verbose command output, throwaway artefacts, `/sy:ship` state). Resolve one identifier's with `scratch_dir {"identifier": "<identifier>"}`, or the repository's own with `scratch_dir {"repo": true}`. Unset derives a directory under `~/.claude/shipyard/`. This backs `sy_tools/guards/review_guard.py`'s hunt-mode write sandbox: a relative value, or one that resolves to any worktree of the repository — main or linked — or an ancestor of one, is refused rather than resolved. A value that resolves to a *subdirectory inside* a worktree is not itself refused — keep it outside every worktree it is asked to provide a scratch directory for. |
| `debug.evals` | no | Write the trigger/trace event log. |
| `ci.poll_timeout` | no | Seconds before the CI poller gives up. Raise it for matrices that routinely run longer, so one poll call spans the wait. |
| `ci.poll_interval` | no | Seconds between CI poll attempts. |
| `models.tiers.*` | no | Named tiers mapped to concrete model aliases. |
| `models.agents.<name>.model` | no | Which tier or model an agent runs at. **Live at dispatch.** |
| `models.agents.<name>.effort` | no | Effort policy for an agent. **Not applied at dispatch** — see [Effort](#effort-is-not-a-runtime-knob). |
| `limits.max_depth_agents` | no | Cross-cutting cap on simultaneous depth-investigation subagents (`sy:trace`/`sy:hunt`/etc.) in flight per phase — `/sy:ship`, `/sy:spec`, `/sy:spike`, and `sy:gate` all read this one key. |
| `plan.max_active_tasks` | no | Cap on `/sy:spec`-ready Tasks a roadmap keeps active under one Epic at once. |
| `spec.light_tier_max_files` | no | File-count proxy for "small": the `light` process tier is allowed only when the plan's declared file set is at most this many files and no risk lenses are activated. Tune per repo. |
| `ship.request_ci_reviewer` | no | Whether GATE requests an automated code-review bot (e.g. Copilot) on the PR, on top of `sy:gate`'s own independent review. Safe to disable: `sy:gate` remains the non-negotiable floor either way, this is an additive second opinion. |
| `ship.merge_strategy` | no | `squash`, `merge`, or `rebase`, passed to `gh pr merge`. Only `squash` composes a subject/body from the PR description. |
| `ship.escalation.max_needs_decision` | no | A ship phase exceeding this many `needs-decision` returns without reaching `done` escalates to `/sy:spec` as underspecified. |
| `ship.escalation.max_needs_trace` | no | A ship phase exceeding this many `needs-trace` returns without reaching `done` escalates to `/sy:spec` as missing evidence, on its own separate count. |
| `transcript.attach` | no | Whether `/sy:plan`, `/sy:spec`, and (full-tier) `/sy:ship` render and attach the session transcript to the tracker. A debug/observability tool for measuring Shipyard itself. |
| `transcript.truncation_limits.tool_input` | no | Character limit per tool-input block when `sy_tools/usage.py` renders a readable transcript. |
| `transcript.truncation_limits.tool_result` | no | Character limit per tool-result block. |
| `transcript.truncation_limits.thinking` | no | Character limit per thinking block. |
| `redaction.extra_words` | no | Org-specific credential-name fragments merged into the built-in secret-word list (`sy_tools/secrets.py`) that `sy_tools/guards/secret_guard.py` and `sy_tools/secrets.py`'s scrub pass both match against. Each entry must be a single alphanumeric word — the matcher compares whole split words, never substrings, so a multi-word entry like `"ID_RSA"` is refused rather than silently never matching. |

The five column names are matched case-insensitively against the real board, and must be distinct under that same case-insensitive comparison — two statuses sharing one name would leave one of them unreachable through the canonical vocabulary, so `validate_config` names both offending keys instead of letting the first match win. `blocked` is deliberately not a column: blocking is a dependency relationship, not a lifecycle state.

## Example: a committed repo config

```json
{
  "$schema": "https://raw.githubusercontent.com/nearmap/shipyard/main/config/schema.json",
  "tracker": "jira",
  "columns": {
    "backlog": "Created",
    "ready": "Ready for Build",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "done": "Closed"
  },
  "tracker_config": {
    "site": "yourorg.atlassian.net",
    "project": "AM"
  }
}
```

With the per-person half in `.shipyard/config.local.json`:

```json
{
  "tracker_config": { "email": "you@yourorg.com" },
  "worktree": { "root": "/Users/you/worktrees" }
}
```

And the one secret in your environment, never in either file:

```bash
export ACLI_TOKEN=...
```

## Models: tiers, then per-agent bindings

Tiers exist so that raising the frontier tier is one edit rather than thirteen:

```json
{
  "models": {
    "tiers": { "frontier": "fable", "frontier_fallback": "opus", "standard": "opus", "cheap": "sonnet" },
    "agents": { "hunt": { "model": "cheap" } }
  }
}
```

An agent's `model` is either a tier name or a concrete alias (`haiku`, `sonnet`, `opus`, `fable`). Shipped defaults are in `config/defaults.json`. Resolution happens at dispatch: the resolved value is passed as the `Agent` invocation's model override, so it takes effect on the next dispatch with no restart.

### Floors are enforced, not advised

`config/floors.json` declares a `min_model` and `min_effort` per agent that **no config layer can lower**. It is plugin-shipped precisely so it is not locally negotiable. Cost-scaling may raise a floor; never lower it.

A config that tries is refused by name, with the reason:

```
models.agents.gate.model is 'sonnet' (from repo-local) but gate has a floor of 'fable':
the independent reviewer is the one thing cost-scaling may never touch; a weaker reviewer
silently weakens every verdict. Cost-scaling may raise a floor, never lower it.
```

`python scripts/validate.py` fails too, so a shipped default below a shipped floor cannot land. This is the mechanical replacement for what used to be a sentence repeated across six files and checked by nothing.

Most agents have permissive floors, so economising is expected — `{"models": {"agents": {"hunt": {"model": "cheap"}}}}` is fine. The reviewer is the exception.

### Effort is not a runtime knob

**There is no `effort` parameter on the `Agent` tool.** A subagent's effort comes only from its definition frontmatter, which Claude Code parses when the plugin loads — before any substitution pass runs. So:

- `models.agents.<name>.effort` is **policy the resolver validates and clamps**. It records intent and enforces floors. It does not change what runs.
- Changing it and reloading changes nothing about a dispatch. Do not expect otherwise.
- Per-agent effort cannot vary per repo at all. Frontmatter ships with the plugin.

This is a limitation of the current runtime, stated plainly rather than papered over. Claude Code's native plugin config (`userConfig`) is not a way around it: `${user_config.KEY}` does not substitute inside agent frontmatter — the load-time validator receives the literal string and rejects it — and `pluginConfigs` is ignored from project settings anyway, so it could never carry a per-repo value.

One related trap the resolver does guard: effort is silently dropped for a model Claude Code does not treat as effort-capable, so an agent pinned to such a model loses its declared effort with no error and inherits the session's. Binding an agent with a declared effort to a non-effort-capable model is refused by name.

### Evidence, and a rejected alternative

Recorded so neither is re-derived. Verified against Claude Code 2.1.220:

- An agent declaring `effort: ${user_config.KEY}` is **rejected at plugin load**, with the validator quoting the raw literal (`has invalid effort '${user_config.PROBE_EFFORT}'`). Substitution therefore happens after frontmatter is parsed into the agent registry, so it cannot reach frontmatter at all. The docs only ever promised "skill and agent *content*", and say "frontmatter" explicitly elsewhere when they mean it.
- Frontmatter `model:` is **not** validated at load, so silence about a bad model there proves nothing either way.
- Frontmatter `effort:` **does** take effect on an effort-capable model (`effort: low` on a `sonnet` subagent was recorded as `low` while the session ran `xhigh`) and is **absent** on one that is not, which is what the effort-capability check above exists to prevent.

**Rejected: generating `agents/*.md` frontmatter from resolved config at `SessionStart`.** `${CLAUDE_PLUGIN_ROOT}` for a marketplace install is the plugin cache, which the docs describe as ephemeral and explicitly warn against writing state into — it is replaced on every update. The mechanism would work exactly until the next `claude plugin update`, then silently revert.

## Secrets

Secrets live in the environment and never in a config file. This is not a stylistic preference:

- a config file is greppable and committed-adjacent, and any skill that `cat`s one burns the value into permanent transcript history, where every future transcript render reproduces it;
- `sy_tools/guards/secret_guard.py` covers environment-variable dumps as a `PreToolUse` hook; it does not cover file reads.

The resolver enforces the boundary three ways: `get_config` refuses to *read* a credential-shaped key, `validate_config` refuses any config layer that *declares* one, and `show_config` refuses to report anything at all rather than risk echoing one — a secret returned even once is a permanent part of whatever transcript asked for it.

`validate_config` doesn't stop at secrets, either: every key in every layer is checked against `config/schema.json`, which declares every legitimate setting — an undeclared key is refused by name whether or not it looks like a secret (a typo like `columns.raedy` is caught the same way `api_token` is), and a declared key with the wrong type, an out-of-enum value, or a value outside its `minimum`/`pattern` is refused too. An undeclared key that *also* looks credential-shaped gets the sharper, specific reason ("keep it in the environment") rather than the generic "not a key config/schema.json declares." Detection of "looks credential-shaped" is two-layered: a word heuristic (`sy_tools/secrets.py`, shared with the secret guard and the transcript scrub pass, so `ACLI_TOKEN` matches and `TOKENIZER_PATH` doesn't) for a secret Shipyard never named, plus an exact match against every tracker adapter's own declared `secret_env` name for the ones it did — the word heuristic alone would miss a declared secret name that happens not to contain a trigger word.

Which value is the secret is adapter-specific — each adapter's `config-map.json` names it under `secret_env` (e.g. jira's is `ACLI_TOKEN`; github currently declares none), and its `ADAPTER.md` explains the one-time login it needs beyond that. `validate_config` also checks that every name an adapter lists there is actually *present* in `os.environ` — set, non-empty — reporting it by name if not, the same way it reports a missing required config key. This is presence, not liveness: a token can be set and still be revoked or expired, which only the adapter's own liveness check (below) can tell.

## Migrating from the `env` block

Shipyard used to be configured through the `env` block of `.claude/settings.json`. Convert it rather than retyping:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" migrate \
  --settings .claude/settings.json --out .shipyard/config.json
```

It maps every recognised legacy name to its config key, coerces numbers and booleans, and refuses to copy anything credential-shaped. Half the map is one adapter's own (below), and which adapter is the `env` block's own answer: the `SY_TRACKER` in the block being converted, falling back to the currently resolved tracker only if the block names none. That distinction is load-bearing because `migrate` normally runs *before* any tracker has been configured, so the resolved value is still the shipped default — deriving the adapter's names from it dropped every `tracker_config.*` variable in the block. `migrate` resolves the configuration first and refuses outright if it cannot, and refuses a tracker that names no adapter rather than converting a subset: a conversion that dropped the adapter's keys and still wrote the file would look complete and would not be. An `--out` file that already exists is **merged into**, never overwritten — migrated values win on conflict, every other key already in the file survives, and a destination that is not valid JSON is a refusal rather than a file this command replaces. The summary names three separate outcomes, never one list: what migrated, what was deliberately left in the environment because it is credential-shaped, and what matched no config key at all (a typo, or a stale setting). Then **remove the migrated keys from the `env` block** — leaving both in place is a deliberate hard failure. `/sy:init-repo` does all of this interactively, including splitting per-person values into the local layer.

The mapping:

| Retired variable | Config key |
|---|---|
| `SY_TRACKER` | `tracker` |
| `SY_BACKLOG_COLNAME` | `columns.backlog` |
| `SY_READY_COLNAME` | `columns.ready` |
| `SY_IN_PROGRESS_COLNAME` | `columns.in_progress` |
| `SY_IN_REVIEW_COLNAME` | `columns.in_review` |
| `SY_DONE_COLNAME` | `columns.done` |
| `SY_WORKTREE_ROOT` | `worktree.root` |
| `SY_MEMORY_DIR` | `memory.dir` |
| `SY_DEBUG_EVALS` | `debug.evals` |
| `SY_CI_POLL_TIMEOUT` | `ci.poll_timeout` |
| `SY_FRONTIER_MODEL` | `models.tiers.frontier` |
| `SY_FRONTIER_FALLBACK` | `models.tiers.frontier_fallback` |
| `SY_IMAGE_MODEL` | `models.agents.img-inspector.model` |
| `SY_DEBATE_MODEL` | `models.agents.debate.model` |
| `ACLI_EMAIL` | `tracker_config.email` |
| `ACLI_SITE` | `tracker_config.site` |
| `ACLI_PROJECT` | `tracker_config.project` |
| `SY_GH_PROJECT` | `tracker_config.project` |
| `SY_GH_REPO` | `tracker_config.repo` |
| `ACLI_TOKEN` | **stays in the environment** |

### Why a set variable is an error rather than an override

Silent precedence is exactly what made the old boundary illegible: `ACLI_TOKEN` and `SY_READY_COLNAME` sat in one mechanism with nothing but prose separating them. A loud rejection naming both values is what makes the new boundary obvious. A variable that merely *agrees* with config is still an error, because two live resolution paths for one key is the thing being removed.

There is no escape hatch, including for CI and for scripts run outside a session: `install.sh` and `docs/smoke_mcp.py` read through the resolver like everything else.

`CLAUDE_CODE_SUBAGENT_MODEL` is also a hard failure. It outranks the per-invocation model parameter, so setting it silently reroutes every agent off whatever the resolver decided — including the independent reviewer.

## Reloading mid-session

Nothing caches a resolved value on disk; every consumer reads through the resolver at call time. So an edit needs no restart, and `/sy:config reload` means: re-resolve, re-validate, reprint with provenance.

The boundary is **the next dispatch**. Workers already running keep what they were given.

`/sy:ship` stamps the config fingerprint into its recorded state alongside its pinned base/head SHAs, and a mismatch at a fix round stops the run rather than continuing on quietly changed settings — a reviewer whose model changed between rounds invalidates the comparison the gate exists to make.

The preflight cache invalidates itself: its fingerprint folds in the resolved config, so a changed setting is a cache miss and the next tracker call re-verifies liveness for real.

## Preflight: presence isn't liveness

`validate_config` proves the config is *present* and internally consistent. It cannot prove a credential still works — a token can be set and revoked. Each adapter declares a real, minimal read for that, which the `preflight` tool runs and caches with a short TTL. See `skills/shared/references/preflight.md`.

## Trigger/trace event log

With `debug.evals` true, every hook firing appends one compact JSON line to `~/.claude/shipyard/eval-events/<session_id>.jsonl`: which skill or subagent triggered, and the tool-call sequence around it. Off by default and zero-cost when off. Useful for building eval harnesses against real runs.
