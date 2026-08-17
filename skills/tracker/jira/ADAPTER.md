# Jira adapter

Implements the tracker contract (`../CONTRACT.md`) against Jira Cloud. This is the default tracker (`tracker: "jira"`).

Every canonical verb is one call to the `sy` MCP server's tool of the same name; the server dispatches to this adapter's REST implementation in `sy_tools/tracker/jira/adapter.py`. This file documents what Jira does with each verb and where its behaviour is Jira-specific — it is not a list of commands to run instead. There is no maintained `acli` or raw-REST recipe for any verb, by design: see `../CONTRACT.md`, "Every verb is one MCP tool call", for the tool-name resolution rule and why the second path is gone.

## Configuration (self-check before any work)

Required config (`.shipyard/config.json`): `tracker_config.email`, `tracker_config.site`, `tracker_config.project`. Required secret, environment only: `ACLI_TOKEN`. The split is declared machine-readably in this directory's `config-map.json`, which the `validate_config` tool enforces. Never put a token in a config file or in a command argument.

`tracker_config.email` is per-person but not a secret — it belongs in the gitignored `.shipyard/config.local.json` layer. `ACLI_TOKEN` is a personal Atlassian API token that stays in the environment, so no `cat` of a committed file can burn it into transcript history. The server inherits it from the launching process's environment and puts it in an `Authorization` header and nowhere else: never a URL, never a returned value, never a failure message.

**One auth mechanism.** The adapter authenticates with `tracker_config.email` plus `ACLI_TOKEN` over REST, and that is the whole story. `acli`'s own separate login session — established with `acli jira auth login` and cached under `~/.config/acli/`, entirely outside Shipyard's config — used to be a second thing to verify because most verbs shelled out to `acli`. Nothing on this path shells `acli` any more, so that check is inapplicable: `acli` being logged out no longer breaks anything Shipyard does, and `acli jira auth status` passing no longer evidences that Shipyard can reach Jira.

**Preflight (the adapter's declared hook for `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`).** A credential can be present and still be dead, and a project key can be set and still name a board this account cannot see, so the canonical `preflight` verb performs two real authenticated reads rather than a presence check — `/myself` for the account, and the configured project itself — and reports the site, account id and project key without ever naming a secret value. The project read is not decoration: Jira answers a search naming an unknown or invisible project with zero issues rather than an error, so nothing else here notices a wrong key. The two reads do not repeat on every invocation — the `preflight` tool's shared cache covers it (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`), keyed here on the plugin build, `jira`, the resolved config and `ACLI_TOKEN`, with a short TTL.

A missing or invalid `ACLI_TOKEN`, `tracker_config.email`, `tracker_config.site`, or `tracker_config.project` fails `preflight` with an error naming which one, and that text is exactly what `preflight.md`'s `## Action needed` block relays — never a bare crash discovered later inside an attachment upload.

## Type mapping

| Canonical | Jira type |
|---|---|
| `epic` | Epic |
| `task` | Task |
| `bug` | Bug |

Execution is flat: one tracking Epic, every executable Task/Bug directly beneath it; conceptual hierarchy lives in the Epic body/comments.

## Status mapping

Each canonical status maps to the Jira status named by the shared, required per-repo column config key (the Jira workflow must have statuses with those names):

| Canonical | Jira status (transition target) |
|---|---|
| `backlog` | `columns.backlog` |
| `ready` | `columns.ready` |
| `in-progress` | `columns.in_progress` |
| `in-review` | `columns.in_review` |
| `done` | `columns.done` |

`set-status` resolves the column name, matches it against each reachable transition's *target* status (not the transition's own name — they often coincide but are different fields, and matching the wrong one is a silent move to the wrong column), performs the transition, and re-reads to confirm. When nothing reachable matches, it fails **listing the reachable targets** so a workflow gap is visible rather than absorbed; transition to the correct reachable target, or surface the gap loudly if none maps to the requested canonical status.

Inspect the closure reason before treating a `done` issue as delivered — decomposed/superseded closure is not delivery.

## Rich text: Markdown → ADF

Jira comments and descriptions are ADF. The conversion happens inside the server, in process, on every body and comment it writes — there is no staging file and no converter to provision. It is done there rather than client-side because the node classes a Markdown-ish client drops silently, bullet lists and fenced code, are exactly the ones a ship log is made of.

## Verb behaviour on Jira

Everything below is Jira-specific behaviour a caller can rely on. Where a verb is unremarkable — `update-issue` replaces the description, `assign` self-assigns — it is not listed.

- **`create-issue`** creates into the configured project with the mapped native type. It deliberately does **not** send `reporter`, even though Jira's own `createmeta` lists it as required: omitting it makes Jira default the reporter to the authenticated account, where sending it would let a shared config file decide whose issues these are. Passing `parent` is the canonical verb **`create-child`**; Jira enforces its own hierarchy here, so a type that cannot be parented to the parent's type is rejected with the field named.
- **`post-comment`** joins `human` and `agent_detail` into the one body, converts that Markdown to ADF, and returns the created comment's id and a deep link to it. `link-pr`'s durable half is this same verb — `human` noting that a PR now exists for this work, `agent_detail` carrying the PR URL — so Jira gets no separate write for it. `post-log` is its own verb whose assembled heading and fenced block convert to ADF the same way. A `shipyard.ship_metrics.v1` block is schema-validated before anything is posted; see `../CONTRACT.md`. The agent-facing half lands as a native Jira **Expand** section, collapsed by default with its caption as the title. The mechanism is narrow on purpose: `adf.py` rewrites the one fixed `<details>`/`<summary>` opening core emits into the `adf="expand"` attributed form `marklas` converts to an Expand node, matching that literal and nothing else. It is not a general `<details>` convention: an unrelated hand-authored disclosure block in some other body passes through the rewrite untouched, and `marklas` then discards its tags *and* its summary text, leaving only the enclosed content. Collapsing is a guarantee core's one caption earns, not a property of writing `<details>` into a Jira comment.
- **`get-issue`** reads REST directly and untruncated, naming the fields it needs rather than `*all` (which fetches every custom field on the board — kilobytes nothing above the seam reads). It returns the description as Markdown, canonical status and type, parent, children, `Blocks`-derived dependencies, and up to 50 comments newest-first, plus `comments_truncated` saying whether that bound actually cut anything off. A silently short thread reads as a complete ship log, so check that flag before concluding "no one raised this". The description read is not guaranteed byte-faithful to the stored ADF — unknown or extension nodes can round-trip through the Markdown conversion lossily — so a caller must never treat a `get-issue` read of the description as a faithful copy to merge, edit, or write back around other content; every `update-issue` description write is a full, deliberate replacement, never a conditional overwrite based on comparing against a prior read. A **comment** read is that same caveat with a known, narrower shape, which `plan_file` depends on: structure survives — headings, lists, fenced blocks, and the one collapsed agent-facing section, which normalises back to the bare `<details>`/`<summary>` opening core wrote, so a later split on that boundary still finds it — while inline punctuation is transformed. Un-backticked Markdown punctuation comes back backslash-escaped (a bare `some_name` as `some\_name`), a link target comes back wrapped in `<>`, and one paragraph's several lines come back joined into one. Backticked spans and fenced blocks are verbatim. So a comment read is faithful enough to split on that boundary and hand the half on, and not faithful enough to diff against what was posted or to write back around. Jira's `subtasks` field carries sub-task-level children only, so it is read for a known leaf type (Task, Bug) and nothing else: on any other type — Epic, Story, Initiative, a custom hierarchy level, or an issue whose `issuetype` is missing — `children` come from a `parent = <key>` search instead, because `subtasks` is empty on every one of them however decomposed they are. That search is scoped by `parent` alone (a key prefix is not a project — issues move, and hierarchies cross projects), and it is one page, so `children_truncated` says whether it left any child out.
- **`find-issues`** posts JQL to `/search/jql` (the classic `/search` endpoint is gone — 410). One page only: `is_last` and `next_page_token` are how a caller asks for more, and every interpolated value is a quoted JQL literal so a title containing a quote cannot widen the search.
- **`add-dependency`** creates the `Blocks` link with `issue` as the blocked side and `blocked_by` as the blocker — measured against a live board rather than derived from Jira's REST model, because the model reads as self-consistent either way round and both directions write and read back without error. It then re-reads to prove the link arrived, not that the direction is right: a write verified by a read through the same coordinate system passes either way round, so `verified: True` never doubles as a direction proof. It still fails rather than warns if the write cannot be confirmed at all: a reversed dependency reads as entirely plausible and misleads every later decomposition.
- **`add-label`** reads the current set and writes it back with the new label unioned in, because Jira has no append. A labels field that does not read back as a list of strings aborts the write instead of being coerced; coercing it would delete labels.
- **`link-parent`** is a field write on Jira, not a link. Ask before crossing into another project or portfolio hierarchy.
- **`link-pr`**: PRs surface in the Jira development panel when the branch or commit names the issue key. The verb's durable half is a comment whose `human` notes that a PR now exists and whose `agent_detail` is the PR URL, so the association survives regardless of dev-panel wiring.
- **`type-convert`** rewrites the work item's type in place and verifies by reading it back. Some site workflows restrict type changes (required fields, hierarchy rules); it then fails loudly rather than leaving the type silently unchanged. Irreversible side effects — parent links, board membership — follow the type.

This adapter's body limit is **32,767 characters**, applied by the shared whole-write refusal in `../CONTRACT.md`. Atlassian's own JCMA migration KB states that on Cloud "it's not possible to bypass the 32,767 character limit for both description and comments" (citing JRACLOUD-59124); the `jira.text.field.character.limit` property behind it is documented and admin-tunable in Data Center only — JRACLOUD-63007 is Atlassian declining to expose it in Cloud without disputing the reporter's premise that the same default applies there, and JRACLOUD-68949 corroborates the description-field limit specifically. What is still left undocumented is the *unit* for an ADF body, which is why the limit stays best-effort.

Deleting a dependency link is not a contract verb: no workflow drives it, so it stays a manual `acli jira workitem link delete --id <id> --yes` outside Shipyard.

**Upgrading to 1.25.1**: `BLOCKER_SIDE`/`BLOCKED_SIDE` (`adapter.py:45-46`) were corrected on that version boundary. Any `Blocks` link this tool created *before* 1.25.1 was written with those two slots swapped; `get-issue`'s `dependencies` now reads every link's slots the corrected way round, so a pre-1.25.1 Shipyard-created link reads inverted after the upgrade — the blocked issue's `dependencies` loses that entry, and the blocker's own `dependencies` gains a spurious one pointing at the issue it blocks. This is a read-time consequence of the fix, not a write: no existing link is rewritten or deleted. Review any `Blocks` link this tool created before 1.25.1 before trusting its `dependencies`; a link created directly in Jira, or by anything that never went through the old constants, is unaffected.

## `attach-artifact` and the attachment lifecycle

Jira supports native work-item attachments. Render the artifact, then hand the path to the `attach-artifact` tool: it checks the gate, sanitises on the rule `../CONTRACT.md` states, and uploads over this adapter's REST path, so no pass can be skipped at the call site and `ACLI_TOKEN` never reaches argv or stdout. This adapter uploads bytes, so a payload the scrub cannot read reaches it as far as the transport is concerned; whether it may is the caller's declaration, not this adapter's. Load `references/attachments.md` for the gate, the two passes, and the verification the caller still owns.

`attachment-update` is the other uploading verb and runs the identical gate and the identical sanitisation before it writes. The only upload the scrub has not looked at is one the caller declared with `allow_opaque` on a payload it cannot decode -- what the scanner still does and what the report carries then is stated once in `../CONTRACT.md`.

`attachment-download` resolves the target by filename, taking a Jira attachment id instead to disambiguate duplicates. An ambiguous match (several namesakes, no id given) fails rather than guessing, and so does an absent one.

`attachment-update` takes no id: it resolves purely by filename, and — unlike `attachment-download` — does not refuse on multiple namesakes. It uploads the new file first, then deletes every same-named attachment it supersedes, each delete verified gone; an absent match is a first upload instead, reported as `replaced: 0`. Confirm the target first — the deletes are real and there is no undo. That upload-then-delete order is the safety margin: an upload that fails (a timeout, a 413, a permission change) leaves the old artifact(s) still attached, where deleting first would have left the issue with nothing (see `../CONTRACT.md` on why there is no `attachment-delete` verb).

## References

- `references/attachments.md` — transcript render/scan/redact/upload/verify.
- `references/migration.md` — GitHub-issue → Jira migration (separate workflow, not used in the loop).
