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
| `preflight` | Confirm the tracker's credential and account are usable, naming nothing secret. Run once, first. |
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
| `type-convert` | Change an existing issue's canonical type in place. Best-effort; loud failure — see below. |
| `post-comment` | Post a Markdown comment. The TL;DR-first convention applies here, in core. |
| `post-log` | Post a **standalone** machine log comment (fenced JSON). Never combined with prose — see below. |
| `attach-artifact` | Attach a durable file (the session transcript) to the issue — see below. |
| `attachment-download` | Fetch an artifact already attached to an issue to a local path. |
| `attachment-update` | Replace the attached artifact of the same filename. Destructive; sanitised like `attach-artifact`. |
| `attachment-delete` | Remove one attached artifact from an issue. Destructive. |
| `link-pr` | Associate a PR with an issue. |

### Every verb is one MCP tool call

There is **exactly one** documented, implemented path per verb: the tool of the same name on the `sy`
MCP server (`sy_tools/server.py`), which dispatches to the configured adapter. Core names no tracker
and no tracker CLI; an adapter doc explains what its tracker does with a verb, not a command to run
instead of it. There is deliberately **no parallel CLI recipe** for any verb anywhere outside
`sy_tools/`: a second path is a second thing to keep correct, and the recipes this replaced had
drifted into three separately-wrong link directions and a truncating read.

Resolve a tool's exposed name from the tools actually available to you rather than typing a literal
identifier: the name carries a deployment-dependent prefix — `mcp__plugin_sy_sy__<verb>` for a
marketplace install, `mcp__sy__<verb>` where a project-level `.mcp.json` provides the server instead.
Both point at the same tool; hardcoding either breaks the other deployment.

Three verbs have no tool of their own, because each is another verb's write carrying different
content: `create-child` is `create-issue` with `parent` set, `post-log` is a `post-comment` carrying
only a fenced JSON block, and `link-pr`'s durable half is a `post-comment` carrying the PR URL. That
keeps one write path per effect.

If the server itself is unavailable, there is no maintained fallback recipe to copy — deliberately.
`preflight` is what tells you that up front rather than mid-workflow.

### `type-convert` is best-effort

Converting an issue's type is verified by reading the new type back, and a tracker that refuses the
change (a workflow rule, a required field, a hierarchy constraint) fails loudly naming the type the
issue still has. Fall back to create-new + link + close-old when it refuses. Side effects follow the
type — parent links, board membership — and converting back does not undo them.

### Attachment lifecycle is destructive

`attachment-update` deletes every attachment of the same filename before uploading, and
`attachment-delete` removes an artifact from the issue's durable record. Neither is undoable; confirm
the target first. Both resolve by filename, taking the tracker-native id instead to disambiguate
duplicates — an ambiguous match fails rather than picking one. Zero matches is where the two part:
`attachment-delete` has nothing to remove and fails, while `attachment-update` is a plain first
upload that reports having replaced nothing.

### Machine logs are standalone (`post-log`)

Usage and metrics logs are small, machine-readable, and posted as their own comments — never appended to a retrospective, plan, decomposition, or checkpoint comment. Schemas:

- `# Claude Code usage` → `{"schema": "shipyard.claude_usage.v1", ...}`
- `# Claude Code ship metrics` → `{"schema": "shipyard.ship_metrics.v1", ...}`

Generate usage from the on-disk transcript tree with `${CLAUDE_PLUGIN_ROOT}/scripts/session_usage.py` (tracker-agnostic). The adapter only posts the resulting JSON.

`shipyard.ship_metrics.v1` is **enforced**, not just documented: a body naming that id must carry exactly one fenced JSON block whose top-level `schema` key claims it, and `post-comment` refuses the whole comment when that block does not match the schema — and equally when the body carries more than one such block, since which is the log is then ambiguous and validating the first would post the rest unchecked. A malformed metrics log therefore cannot land and then be read as authoritative. Field definitions live in exactly one place, `${CLAUDE_PLUGIN_ROOT}/skills/ship/references/handoff-accounting.md`; the executable copy is `sy_tools/ship_metrics.py`. Every field is optional except `schema` and `task` — an unknown metric is posted as `null`, never as a plausible zero — with one exception the model states and the reference explains. A body carrying no such block passes through unvalidated.

### Attachments may degrade to a link

`attach-artifact` uploads a file where the tracker supports it (Jira work-item attachments). Where it does not (GitHub issues have no CLI-scriptable attachment), the adapter substitutes an equivalent durable artifact (a private gist) and links it from a comment. Either way the artifact is secret-scanned before it leaves the machine, and the log comment references it by name/URL. This asymmetry is documented per adapter and in the deliberate-asymmetries section of the README. The lifecycle verbs act on whatever the adapter created, so `attachment-download` on a GitHub issue reads the gist back.

`attach-artifact` and `attachment-update` are the two verbs that upload, and both perform the whole path in one call: check the gate, run both sanitisation passes in order, then dispatch to the adapter — so no pass can be skipped and no credential reaches argv or stdout. Attaching or replacing a transcript is gated on `transcript.attach`, and a `ship` caller additionally needs the `full` process tier; when the gate is off the call is a no-op skip and nothing is read, scrubbed, scanned, or uploaded. There is no unscanned upload path: that is the point of the sanitisation living inside the tool rather than in whoever calls it.

## Exactly one ACTIVE plan

A `task`/`bug` carries at most one execution plan whose status is ACTIVE. Superseding is explicit: mark the old plan comment SUPERSEDED and the new one ACTIVE, then re-read to confirm exactly one ACTIVE. Never use a "latest-looking comment wins" heuristic. This is a core convention; the adapter only provides `post-comment`/`get-issue`.

## Configuration

- `tracker` = `jira` | `github` (default `jira`). Selects the adapter.
- **Required column names** (all trackers), from the repo's `.shipyard/config.json`: `columns.backlog`, `columns.ready`, `columns.in_progress`, `columns.in_review`, `columns.done`. Missing values fail fast.
- Each adapter declares its own additional configuration in its `config-map.json` and fails fast when it is missing.
- Secrets are never config: they stay in the environment. See `docs/configuration.md`.
