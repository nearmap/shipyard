# Tracker contract

The single vocabulary the toolbox uses to talk to an issue tracker. Core skills and agents (everything outside `skills/tracker/`) reference **only** the verbs, statuses, and types below. Each adapter (`jira/ADAPTER.md`, `github/ADAPTER.md`) maps them to its native system. The code host (GitHub PRs and CI) is a separate concern owned by `/sy:pr` and `/sy:ci`; it is not the tracker and never varies.

## Issue IDs are opaque

An issue ID is an opaque string that round-trips untouched: `PROJ-123` and `#123` both pass through core without being parsed, split, or constructed. Only an adapter may interpret an ID's shape.

## Rich text is Markdown

Every body and comment the toolbox produces is **Markdown**. The adapter renders it to the native format (Jira: ADF; GitHub: Markdown passthrough). Core never emits tracker-native markup.

## Issue types

| Canonical | Meaning |
|---|---|
| `epic` | The living roadmap container. One per objective. |
| `task` | One executable unit ≈ one coherent PR. Direct child of an epic. |
| `bug` | Same lifecycle as `task`; a defect fix rather than new work. |

## Statuses (the kanban columns)

Shipyard is opinionated about the **five** lifecycle columns and their workflow mapping — not their names. Core uses only these canonical tokens; each adapter maps a token to the tracker's actual column via a **required per-repo env var**. Matching is case-insensitive.

| Canonical | Column config key | Set when | Meaning |
|---|---|---|---|
| `backlog` | `columns.backlog` | `/sy:plan` creates the item | queued, not yet specced |
| `ready` | `columns.ready` | `/sy:spec` plan approved | specced, ready to build |
| `in-progress` | `columns.in_progress` | `/sy:ship` builds | active build |
| `in-review` | `columns.in_review` | gate on a reviewable PR | reviewable gated PR exists |
| `done` | `columns.done` | merge | terminal (inspect closure reason — see below) |

The column names are **required** and resolved from the repo's `.shipyard/config.json`, so different repos on one machine can use different board labels while every adapter reads the same keys — the same config drives whichever tracker the repo uses.

**`blocked` is not a status.** A blocking relationship is expressed with `add-dependency`; the tracker surfaces it natively (Jira link / GitHub blocked indicator).

**Closure semantics.** `done` is not automatically *delivered*. Merged/delivered closure satisfies a dependency; decomposed/superseded closure does not. Follow replacement links until delivered capability is reached. `/sy:ship` resolves this chain before branching.

## Verbs

The complete set of tracker operations. An adapter must implement every verb.

| Verb | Semantics |
|---|---|
| `create-issue` | Create an issue with `type`, `title`, Markdown `body`. Returns an opaque issue ID. |
| `create-child` | Create an issue of `type` parented to a given issue. Returns an opaque ID. |
| `get-issue` | Fetch title, body, canonical status, type, parent, children, labels, dependencies, linked PRs. |
| `update-issue` | Replace an issue's Markdown body. |
| `find-issues` | Query by status / type / parent / free text within the configured project. |
| `set-status` | Move an issue to a canonical status. |
| `assign` | Assign an issue (self-assign is the default caller need). |
| `link-parent` | Re-parent an existing issue under another. |
| `add-dependency` | Record that issue X is blocked by issue Y. |
| `add-label` | Add a label, preserving existing labels. |
| `post-comment` | Post a Markdown comment. The TL;DR-first convention applies here, in core. |
| `post-log` | Post a **standalone** machine log comment (fenced JSON). Never combined with prose — see below. |
| `attach-artifact` | Attach a durable file (the session transcript) to the issue. Performed by the `sy` MCP server's tool of the same name, not by hand — see below. |
| `link-pr` | Associate a PR with an issue. |

### Machine logs are standalone (`post-log`)

Usage and metrics logs are small, machine-readable, and posted as their own comments — never appended to a retrospective, plan, decomposition, or checkpoint comment. Schemas:

- `# Claude Code usage` → `{"schema": "shipyard.claude_usage.v1", ...}`
- `# Claude Code ship metrics` → `{"schema": "shipyard.ship_metrics.v1", ...}`

Generate usage from the on-disk transcript tree with `${CLAUDE_PLUGIN_ROOT}/scripts/session_usage.py` (tracker-agnostic). The adapter only posts the resulting JSON.

### Attachments may degrade to a link

`attach-artifact` uploads a file where the tracker supports it (Jira work-item attachments). Where it does not (GitHub issues have no CLI-scriptable attachment), the adapter substitutes an equivalent durable artifact (a private gist) and links it from a comment. Either way the artifact is secret-scanned before it leaves the machine, and the log comment references it by name/URL. This asymmetry is documented per adapter and in the deliberate-asymmetries section of the README.

This is the one verb core does not drive command by command. The `sy` MCP server's `attach-artifact` tool (`mcp__sy__attach-artifact`) performs the whole path in one call: it checks the gate, runs both sanitisation passes in order, and dispatches to the configured tracker's adapter — so the caller still never names a tracker, no pass can be skipped, and no credential reaches argv or stdout. Attaching a transcript is gated on `transcript.attach`, and a `ship` caller additionally needs the `full` process tier; when the gate is off the call is a no-op skip and nothing is read, scrubbed, scanned, or uploaded. Each adapter's upload helper still ships and still works for recovery, but it uploads exactly what it is given: off this path both passes are the caller's problem.

## Exactly one ACTIVE plan

A `task`/`bug` carries at most one execution plan whose status is ACTIVE. Superseding is explicit: mark the old plan comment SUPERSEDED and the new one ACTIVE, then re-read to confirm exactly one ACTIVE. Never use a "latest-looking comment wins" heuristic. This is a core convention; the adapter only provides `post-comment`/`get-issue`.

## Configuration

- `tracker` = `jira` | `github` (default `jira`). Selects the adapter.
- **Required column names** (all trackers), from the repo's `.shipyard/config.json`: `columns.backlog`, `columns.ready`, `columns.in_progress`, `columns.in_review`, `columns.done`. Missing values fail fast.
- Each adapter declares its own additional configuration in its `config-map.json` and fails fast when it is missing.
- Secrets are never config: they stay in the environment. See `docs/configuration.md`.
