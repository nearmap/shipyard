# Configuration

Shipyard is configured **per repository**, through the `env` block of that repo's `.claude/settings.json`. Claude Code sets those variables for any session run in the repo, and every Shipyard skill, agent, and helper reads them from the environment. Because the config lives in the repo, different repos on one machine can use different trackers, boards, column names, and reviewer models with no global state — the same machine can drive a Jira repo and a GitHub repo side by side.

This page is the complete reference for every knob. For the one-time GitHub Projects board and field setup that the GitHub variables point at, see [`github-setup.md`](github-setup.md). If you installed Shipyard with `--scope project` (see [`installation.md`](installation.md#choose-an-install-scope)), the `env` block below lives in the very same `.claude/settings.json` as the plugin's `enabledPlugins` entry — one tracked file declaring both "this repo runs Shipyard" and "configured like this," though a fresh clone still needs the marketplace itself known (also covered there) before either takes effect.

## Every setting

| Variable | Tracker | Required | Default | What it does |
|---|---|---|---|---|
| `SY_TRACKER` | both | no | `jira` | Selects the tracker adapter: `jira` or `github`. This is the single point where the tracker is chosen. |
| `SY_FRONTIER_MODEL` | both | no | `fable` | The model `sy:gate` uses for the independent review, and what `frontier` resolves to for any ship-profile phase that states it. A quality floor, not a cost dial — set it to your strongest available model. |
| `SY_FRONTIER_FALLBACK` | both | no | `opus` | The model a phase falls back to once if its requested frontier model cannot run (spend cap, rate limit, or a refusal). If the reviewer's fallback also fails, ship returns `blocked` rather than promoting an unreviewed commit. |
| `SY_IMAGE_MODEL` | both | no | `sonnet` | The model `sy:img-inspector` uses to look at figures/screenshots/plots and return a text verdict, so image tokens stay out of the long-running context. A quality floor, not a cost dial. |
| `SY_DEBATE_MODEL` | both | no | `opus` | The model `sy:debate` and its `sy:debater` dispatches use to pressure-test the core decision behind every `/sy:plan` roadmap, `/sy:spec` plan, and `/sy:spike` verdict. A quality floor, not a cost dial — raise it (e.g. to your `SY_FRONTIER_MODEL`) for a fork whose blast radius justifies the extra cost. |
| `SY_WORKTREE_ROOT` | both | no | `<repo>-worktrees` beside the repo | The directory `/sy:ship` creates its isolated build/slice/review worktrees in (`$SY_WORKTREE_ROOT/<branch>`). Default is the sibling directory beside the repo (`/path/to/myrepo` → `/path/to/myrepo-worktrees/`); it is never inside the working tree. |
| `SY_MEMORY_DIR` | both | no | `~/.claude/shipyard/memory` | Where the durable cross-session memory (`scripts/sy_memory.py`) keeps its one-file-per-lesson store and greppable index. User-global and cross-repo by design — set it only to relocate the store. |
| `SY_DEBUG_EVALS` | both | no | unset (off) | When truthy (`1`/`true`/`yes`), appends a compact JSONL trigger/trace event — which skill or subagent fired, and the tool-call sequence around it — to `~/.claude/shipyard/eval-events/<session_id>.jsonl` on every tool call. For building eval harnesses against real runs; leave unset for normal use. |
| `SY_BACKLOG_COLNAME` | both | **yes** | — | Your board/workflow name for the `backlog` column (queued, not yet specced). |
| `SY_READY_COLNAME` | both | **yes** | — | Your name for the `ready` column (specced, plan approved). |
| `SY_IN_PROGRESS_COLNAME` | both | **yes** | — | Your name for the `in-progress` column (active build). |
| `SY_IN_REVIEW_COLNAME` | both | **yes** | — | Your name for the `in-review` column (a reviewable gated PR exists). |
| `SY_DONE_COLNAME` | both | **yes** | — | Your name for the `done` column (terminal). |
| `SY_GH_PROJECT` | github | **yes** (github) | — | The Projects v2 board, `<owner>/<number>` — `@me/<n>` (or `<username>/<n>`) for a user board, `<org>/<n>` for an org board. |
| `SY_GH_REPO` | github | no | current repo | Where issues live, `<owner>/<repo>`. Set it when issues live in a different repo than the one you run Claude in. |
| `ACLI_EMAIL` | jira | **yes** (jira) | — | Atlassian account email for `acli`. |
| `ACLI_TOKEN` | jira | **yes** (jira) | — | Atlassian API token. **Secret** — see below. |
| `ACLI_SITE` | jira | **yes** (jira) | — | Your Atlassian site (e.g. `your-org.atlassian.net`). |
| `ACLI_PROJECT` | jira | **yes** (jira) | — | The Jira project key new issues are created in and queries are scoped to. |

Missing required values fail fast with the name of the missing variable, rather than silently writing to the wrong place.

### The five column names

Shipyard is opinionated about the **number and role** of the lifecycle columns — five: `backlog → ready → in-progress → in-review → done` — but not their names. The five `SY_*_COLNAME` variables map each canonical role to whatever your board actually calls it (matching is case-insensitive). They are tracker-neutral: the Jira adapter reads them as workflow transition targets and the GitHub adapter reads them as `Status` field options, so one repo config drives whichever tracker the repo uses. `blocked` is deliberately not a column — a block is a dependency relationship the tracker surfaces natively.

## Example: a Jira repo

`.claude/settings.json` in the repo:

```json
{
  "env": {
    "SY_TRACKER": "jira",
    "SY_FRONTIER_MODEL": "fable",
    "SY_IMAGE_MODEL": "sonnet",
    "SY_WORKTREE_ROOT": "/abs/path/for/worktrees",
    "ACLI_EMAIL": "you@example.com",
    "ACLI_SITE": "your-org.atlassian.net",
    "ACLI_PROJECT": "PROJ",
    "SY_BACKLOG_COLNAME": "To Do",
    "SY_READY_COLNAME": "Ready",
    "SY_IN_PROGRESS_COLNAME": "In Progress",
    "SY_IN_REVIEW_COLNAME": "In Review",
    "SY_DONE_COLNAME": "Done"
  }
}
```

`ACLI_TOKEN` is intentionally absent here — keep it out of shared config (see [Secrets](#secrets)). `SY_WORKTREE_ROOT` is optional — drop the line to keep the default sibling `<repo>-worktrees/` directory. The five column names must match statuses that exist in the Jira project's workflow.

## Example: a GitHub repo

`.claude/settings.json` in the repo:

```json
{
  "env": {
    "SY_TRACKER": "github",
    "SY_FRONTIER_MODEL": "fable",
    "SY_IMAGE_MODEL": "sonnet",
    "SY_WORKTREE_ROOT": "/abs/path/for/worktrees",
    "SY_GH_PROJECT": "@me/3",
    "SY_GH_REPO": "your-name/your-repo",
    "SY_BACKLOG_COLNAME": "Backlog",
    "SY_READY_COLNAME": "Ready",
    "SY_IN_PROGRESS_COLNAME": "In progress",
    "SY_IN_REVIEW_COLNAME": "In review",
    "SY_DONE_COLNAME": "Done"
  }
}
```

`SY_WORKTREE_ROOT` is optional here too. The board this points at needs a `Status` single-select with one option per column name above, and a `Type` single-select with options `Epic`/`Task`/`Bug`. [`github-setup.md`](github-setup.md) walks through creating them and verifying the config resolves.

## Preflight: presence isn't liveness

Every command that reads or writes the tracker (`/sy:plan`, `/sy:spec`, `/sy:ship`, `/sy:spike`) checks this configuration before doing anything else — not just that the required values are set, but that they actually work, with one cheap real read against the tracker. A value can be present and still dead: a revoked token, a login that was never done, a mistyped project key. That live check is cached (fingerprinted to the plugin build, the tracker, and the resolved config, with a short TTL) so it costs a network round trip only occasionally, not on every command. A failure names exactly what's missing or broken and links back to this page — never a crash discovered later inside a write. Run `/sy:init-repo` for an interactive walkthrough that gets a repo (or just your own personal credential, if a teammate already configured the shared values) to the point where this check passes.

## Secrets and machine-specific values

`ACLI_TOKEN` is a credential and must not be committed. Put it in the repo's `.claude/settings.local.json` (which Claude Code treats as personal and is gitignored) or export it in your shell environment; keep only non-secret config in the shared `.claude/settings.json`. `settings.local.json` uses the same `env` shape:

```json
{ "env": { "ACLI_TOKEN": "your-atlassian-api-token" } }
```

Shipyard never passes tokens as command-line arguments. Never check whether one is set by dumping it (`env | grep -i token`, `echo $ACLI_TOKEN`) — that prints the value into that command's tool-call result, which is then permanent session history and resurfaces verbatim in every future transcript render; use a presence-only check instead (`[ -n "$ACLI_TOKEN" ]`, or the tracker's own `preflight` command). This isn't advisory only: `scripts/secret_guard.py` is wired as a `PreToolUse` hook on every `Bash` call and denies the dump/echo patterns outright, name-based rather than value-based, so it fires whether or not a secret is actually set.

`SY_WORKTREE_ROOT` isn't a secret, but an absolute path is machine-specific the same way a credential is personal — it belongs in `settings.local.json` too (per-repo, if teammates' home directories differ) rather than the shared `settings.json`, e.g. `{ "env": { "SY_WORKTREE_ROOT": "/home/you/worktrees" } }`. `/sy:init-repo` asks about this and writes it to the right file.

## Model routing

Leave `CLAUDE_CODE_SUBAGENT_MODEL` **unset**. It overrides model routing for every subagent and takes precedence over `SY_FRONTIER_MODEL`, `SY_IMAGE_MODEL`, and `SY_DEBATE_MODEL`, so setting it would silently reroute the independent reviewer, the image inspector, and the debate off their intended models. `./install.sh` warns if it is set.

## Trigger/trace event log (`SY_DEBUG_EVALS`)

`scripts/eval_events.py` is wired into `PreToolUse`, `SubagentStop`, and `Stop`, same as `review_guard.py` and `session_usage.py`, but it does nothing unless `SY_DEBUG_EVALS` is truthy — set it only when you're building or running an eval harness against Shipyard itself. Enabled, it appends one JSON line per hook firing to `~/.claude/shipyard/eval-events/<session_id>.jsonl`, keyed by session rather than cwd: a `/sy:ship` build/gate subagent's cwd is a worktree under `SY_WORKTREE_ROOT` that gets deleted after use, so a repo-local path would both fragment one session's trace across directories and lose the worktree-side events outright.

```json
{"schema":"shipyard.eval_events.v1","ts":"2026-07-23T10:00:00+00:00","session_id":"...","hook_event":"PreToolUse","agent_type":"gate","tool":"Skill","detail":{"skill":"ship"}}
```

`agent_type` is `"main"` for the top-level session and the namespace-stripped agent name (`sy:gate` → `gate`) inside a subagent transcript. `tool`/`detail` are only present on `PreToolUse` rows; `detail` is only populated for `Skill` calls (`{"skill": ...}`) and `Agent`/`Task` calls (`{"subagent_type": ..., "description": ...}`) — the two tool calls that answer "what triggered." Every other tool call still gets a bare `{"tool": ...}` row, enough to reconstruct call order for a trace-based eval without logging full tool arguments. `SubagentStop`/`Stop` rows carry no `tool` field; they mark segment boundaries.
