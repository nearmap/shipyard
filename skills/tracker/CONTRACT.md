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
| `get-issue` | Fetch title, body, canonical status, type, parent, children, labels, dependencies, comments. |
| `update-issue` | Replace an issue's Markdown body. |
| `find-issues` | Query by status / type / parent / free text within the configured project. |
| `set-status` | Move an issue to a canonical status. |
| `assign` | Assign an issue (self-assign is the default caller need). |
| `link-parent` | Re-parent an existing issue under another. |
| `add-dependency` | Record that issue X is blocked by issue Y. |
| `add-label` | Add a label, preserving existing labels. |
| `type-convert` | Change an existing issue's canonical type in place. Best-effort; loud failure — see below. |
| `post-comment` | Post a Markdown comment as two required parts: `human` (the TL;DR and the reasoning, leading) and `agent_detail` (pointers, SHAs, URLs, footers). The tool writes the boundary between them, so a caller never composes one. That boundary is not a flat separator: it wraps `agent_detail` in a captioned section both trackers render collapsed by default, so a human reader meets the leading half first and opens the rest only on demand. |
| `post-log` | Post a **standalone** machine log comment from a `title` and a `payload` object, which the tool serialises and fences itself. It can carry nothing else — see below. |
| `attach-artifact` | Attach a durable file (the session transcript) to the issue — see below. |
| `attachment-download` | Fetch an artifact already attached to an issue to a local path, named by filename or the tracker-native id (the disambiguator when two attachments share a filename). |
| `attachment-update` | Replace the attached artifact of the same filename. Destructive; sanitised like `attach-artifact`. |
| `link-pr` | Associate a PR with an issue: `human` is a short note that a PR now exists for this work, `agent_detail` is the PR URL. |

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

Two verbs have no tool of their own, because each is another verb's write carrying different
content: `create-child` is `create-issue` with `parent` set, and `link-pr`'s durable half is a
`post-comment` whose `human` notes that a PR now exists for this work and whose `agent_detail` is the
PR URL. Today no call site makes that write standalone — the content rides in the ship retrospective's
own `agent_detail` — but the shape is defined for a caller that does. That keeps one write path per
effect. `post-log` was a third until it was pulled out: a machine log has no human-judgment half to
pair with, so it has nothing to put in `post-comment`'s leading part, and giving it its own signature
makes the standalone rule structural instead of something a caller has to remember.

If the server itself is unavailable, there is no maintained fallback recipe to copy — deliberately.
`preflight` is what tells you that up front rather than mid-workflow.

### `type-convert` is best-effort

Converting an issue's type is verified by reading the new type back, and a tracker that refuses the
change (a workflow rule, a required field, a hierarchy constraint) fails loudly naming the type the
issue still has. Fall back to create-new + link + close-old when it refuses. Side effects follow the
type — parent links, board membership — and converting back does not undo them.

### Attachment lifecycle has no delete

`attachment-update` replaces an attachment matching the same filename, which is undoable only by
uploading a further replacement — confirm the target first. It takes no id: it resolves purely by
`path`'s filename. Zero matches is a plain first upload, never a failure. More than one existing
attachment sharing that filename is adapter-specific — see each `ADAPTER.md` — because the two
trackers offer no common primitive for "replace all of these": at least one namesake is always
either replaced or refused, never silently left untouched.

There is deliberately no `attachment-delete` verb: removing an artifact from an issue's durable record
with no undo is a real safety cost for a capability nothing in the documented plan/spec/ship/spike
loop ever calls. `attachment-update` already covers the corrective case — replacing a bad or stale
artifact — without exposing standalone deletion.

### Machine logs are standalone (`post-log`)

Usage and metrics logs are small, machine-readable, and posted as their own comments — never appended to a retrospective, plan, decomposition, or checkpoint comment. `post-log` makes that structural for anything written through it: it takes a single-line `title` and a `payload` object and writes the heading and the single fenced block itself, so its signature offers no field in which other content could ride along. Reaching for `post-log` rather than `post-comment` remains the caller's call — `post-comment` does not turn a pasted-in log away. Schemas:

- `# Claude Code usage` → `{"schema": "shipyard.claude_usage.v1", ...}`
- `# Claude Code ship metrics` → `{"schema": "shipyard.ship_metrics.v1", ...}`

Generate usage from the on-disk transcript tree with the `usage_summarize` tool (tracker-agnostic, and named the same way as every verb above). The adapter only posts the resulting JSON.

`shipyard.ship_metrics.v1` is **enforced**, not just documented: a body naming that id must carry exactly one fenced JSON block whose top-level `schema` key claims it, and `post-log`, `post-comment`, `create-issue` and `update-issue` alike refuse the whole write when that block does not match the schema — the identical gate on all four bodies, so a machine log written into an issue body is held to exactly what a comment is — and equally when the body carries more than one such block, since which is the log is then ambiguous and validating the first would write the rest unchecked. That count is not limited to well-formed fences: an unclosed block, a closing marker with trailing text, or a bare prose mention of the id all count as a candidate too, so naming the id anywhere outside the one valid block is also a refusal, not a silent pass-through. A malformed metrics log therefore cannot land and then be read as authoritative. Field definitions live in exactly one place, `${CLAUDE_PLUGIN_ROOT}/skills/ship/references/handoff-accounting.md`; the executable copy is `sy_tools/ship_metrics.py`. Every field is optional except `schema` and `task` — an unknown metric is posted as `null`, never as a plausible zero — with the few exceptions the model states and the reference explains. A body that neither names the id — literally or via a JSON `\uXXXX` escape — nor carries a block whose parsed content resolves to it passes through unvalidated.

**Body size is the second such guard, over the identical four writers.** A body longer than the selected adapter's limit is refused whole by `post-log`, `post-comment`, `create-issue` and `update-issue` alike, before the write is attempted, with a message naming the measured length, the limit, and the overflow — the tracker would refuse an oversized body outright anyway, though a refusal here is not proof it would have — and nothing here truncates a body to fit, because a silently shortened plan or retrospective reads as a complete one. The limit is **per adapter and best-effort**, not a spec: each adapter states its own number and where that number comes from, and a body under it is one the tracker has not been observed to refuse rather than one it promises to accept. Shorten oversized content, or split it across writes — with one exception where only shortening is available: a plan comment is never split across comments. A plan spread over two is one no later phase can read back whole, since `plan_file` selects a single comment and materialises a single agent-facing half; an oversized plan is tightened in its `/sy:ship` half instead.

### Attachments may degrade to a link

`attach-artifact` uploads a file where the tracker supports it (Jira work-item attachments). Where it does not (GitHub issues have no CLI-scriptable attachment), the adapter substitutes an equivalent durable artifact (a private gist) and links it from a comment. Either way the artifact is sanitised before it leaves the machine — on the rule below rather than unconditionally — and the log comment references it by name/URL. This asymmetry is documented in the adapter's own `ADAPTER.md`, whose "Deliberate asymmetries" section is the list of them. The lifecycle verbs act on whatever the adapter created, so `attachment-download` on a GitHub issue reads the gist back.

`attach-artifact` and `attachment-update` are the two verbs that upload, and both perform the whole path in one call: check the gate, sanitise, then dispatch to the adapter — so no pass can be skipped at the call site and no credential reaches argv or stdout. Attaching or replacing a transcript is gated on `transcript.attach`, and a `ship` caller additionally needs the `full` process tier; when the gate is off the call is a no-op skip and nothing is read, scrubbed, scanned, or uploaded. What sanitising means is stated once, here: both passes, in order, over a payload that is UTF-8 text; the **scanner pass alone** over one the caller declares opaque with `allow_opaque` — the known-value scrub is the pass that needs a decode such a payload does not have, while the scanner reads bytes and still runs and can still refuse the upload — which is then reported as `opaque` with its reason and carries no pass result at all -- the scanner still runs behind the scenes as a best-effort check and a genuine finding still blocks the upload, but its coverage of non-text content cannot be guaranteed, so the report never credits it with a clean scan either; and an undeclared opaque payload is refused rather than uploaded — and a store that cannot hold a non-text payload at all still refuses it outright whatever the scan finds; see the adapter's own `ADAPTER.md`. `allow_opaque` is a declaration, not a permission: the caller says what it is shipping, and the exception lives inside the tool with everything else, which is why the sanitisation is there rather than in whoever calls it. Even where both passes do run, the scanner skips some paths by extension, so a zero-finding result is evidence and not proof.

## Exactly one ACTIVE plan

A `task`/`bug` carries at most one execution plan whose status is ACTIVE. Superseding is explicit: mark the old plan comment SUPERSEDED and the new one ACTIVE, then re-read to confirm exactly one ACTIVE. Never use a "latest-looking comment wins" heuristic. This is a core convention; the adapter only provides `post-comment`/`get-issue`. For `/sy:ship` it is not left to the caller to uphold: the `plan_file` tool is what enforces it, refusing zero or several ACTIVE plans by count and comment id rather than picking one, so no run ships against a plan nobody approved.

## Configuration

- `tracker` = `jira` | `github` (default `jira`). Selects the adapter.
- **Required column names** (all trackers), from the repo's `.shipyard/config.json`: `columns.backlog`, `columns.ready`, `columns.in_progress`, `columns.in_review`, `columns.done`. Missing values fail fast.
- Each adapter declares its own additional configuration in its `config-map.json` and fails fast when it is missing.
- Secrets are never config: they stay in the environment. See `docs/configuration.md`.
