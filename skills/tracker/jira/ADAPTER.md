# Jira adapter

Implements the tracker contract (`../CONTRACT.md`) against Jira Cloud. This is the default tracker (`tracker: "jira"`).

Every canonical verb is one call to the `sy` MCP server's tool of the same name; the server dispatches to this adapter's REST implementation in `sy_tools/tracker/jira/adapter.py`. This file documents what Jira does with each verb and where its behaviour is Jira-specific — it is not a list of commands to run instead. There is no maintained `acli` or raw-REST recipe for any verb, by design: see `../CONTRACT.md`, "Every verb is one MCP tool call", for the tool-name resolution rule and why the second path is gone.

## Configuration (self-check before any work)

Required config (`.shipyard/config.json`): `tracker_config.email`, `tracker_config.site`, `tracker_config.project`. Required secret, environment only: `ACLI_TOKEN`. The split is declared machine-readably in this directory's `config-map.json`, which `sy_config.py validate` enforces. Never put a token in a config file or in a command argument.

`tracker_config.email` is per-person but not a secret — it belongs in the gitignored `.shipyard/config.local.json` layer. `ACLI_TOKEN` is a personal Atlassian API token that stays in the environment, so no `cat` of a committed file can burn it into transcript history. The server inherits it from the launching process's environment and puts it in an `Authorization` header and nowhere else: never a URL, never a returned value, never a failure message.

**One auth mechanism.** The adapter authenticates with `tracker_config.email` plus `ACLI_TOKEN` over REST, and that is the whole story. `acli`'s own separate login session — established with `acli jira auth login` and cached under `~/.config/acli/`, entirely outside Shipyard's config — used to be a second thing to verify because most verbs shelled out to `acli`. Nothing on this path shells `acli` any more, so that check is inapplicable: `acli` being logged out no longer breaks anything Shipyard does, and `acli jira auth status` passing no longer evidences that Shipyard can reach Jira.

**Preflight (the adapter's declared hook for `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`).** A credential can be present and still be dead, and a project key can be set and still name a board this account cannot see, so the canonical `preflight` verb performs two real authenticated reads rather than a presence check — `/myself` for the account, and the configured project itself — and reports the site, account id and project key without ever naming a secret value. The project read is not decoration: Jira answers a search naming an unknown or invisible project with zero issues rather than an error, so nothing else here notices a wrong key. Gate it behind the shared cache so it does not repeat on every invocation:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_preflight.py" check --tracker jira --vars ACLI_TOKEN
# exit 0 → cached fresh, nothing to do.
# exit 2 → call the `preflight` tool now; on success:
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_preflight.py" record --tracker jira --vars ACLI_TOKEN
```

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
- **`post-comment`** converts the Markdown body to ADF and returns the created comment's id and a deep link to it. `post-log` is this same verb carrying only a fenced JSON block, and `link-pr`'s durable half is it carrying the PR URL — Jira gets no separate write for either. A `shipyard.ship_metrics.v1` block is schema-validated before anything is posted; see `../CONTRACT.md`.
- **`get-issue`** reads REST directly and untruncated, naming the fields it needs rather than `*all` (which fetches every custom field on the board — kilobytes nothing above the seam reads). It returns the description as Markdown, canonical status and type, parent, children, `Blocks`-derived dependencies, and up to 50 comments newest-first, plus `comments_truncated` saying whether that bound actually cut anything off. A silently short thread reads as a complete ship log, so check that flag before concluding "no one raised this". Jira's `subtasks` field carries sub-task-level children only, so it is read for a known leaf type (Task, Bug) and nothing else: on any other type — Epic, Story, Initiative, a custom hierarchy level, or an issue whose `issuetype` is missing — `children` come from a `parent = <key>` search instead, because `subtasks` is empty on every one of them however decomposed they are. That search is scoped by `parent` alone (a key prefix is not a project — issues move, and hierarchies cross projects), and it is one page, so `children_truncated` says whether it left any child out.
- **`find-issues`** posts JQL to `/search/jql` (the classic `/search` endpoint is gone — 410). One page only: `is_last` and `next_page_token` are how a caller asks for more, and every interpolated value is a quoted JQL literal so a title containing a quote cannot widen the search.
- **`add-dependency`** creates the `Blocks` link with `issue` as the blocked side and `blocked_by` as the blocker, taken straight from Jira's REST model, where the outward issue performs the type's outward action. It then re-reads to prove the direction took, and fails rather than warns if it cannot: a reversed dependency reads as entirely plausible and misleads every later decomposition. This is where the old `acli link --in/--out` recipe was wrong — those flags are inverted relative to Jira's model, so it silently created the reverse link.
- **`add-label`** reads the current set and writes it back with the new label unioned in, because Jira has no append. A labels field that does not read back as a list of strings aborts the write instead of being coerced; coercing it would delete labels.
- **`link-parent`** is a field write on Jira, not a link. Ask before crossing into another project or portfolio hierarchy.
- **`link-pr`**: PRs surface in the Jira development panel when the branch or commit names the issue key. The verb's durable half is a comment carrying the PR URL, so the association survives regardless of dev-panel wiring.
- **`type-convert`** rewrites the work item's type in place and verifies by reading it back. Some site workflows restrict type changes (required fields, hierarchy rules); it then fails loudly rather than leaving the type silently unchanged. Irreversible side effects — parent links, board membership — follow the type.

Deleting a dependency link is not a contract verb: no workflow drives it, so it stays a manual `acli jira workitem link delete --id <id> --yes` outside Shipyard.

## `attach-artifact` and the attachment lifecycle

Jira supports native work-item attachments. Render the artifact, then hand the path to the `attach-artifact` tool: it checks the gate, runs both sanitisation passes in order, and uploads over this adapter's REST path, so no pass can be skipped and `ACLI_TOKEN` never reaches argv or stdout. Load `references/attachments.md` for the gate, the two passes, and the verification the caller still owns.

`attachment-update` is the other uploading verb and runs the identical gate and both passes before it writes. There is no unscanned upload path.

`attachment-download` resolves the target by filename, taking a Jira attachment id instead to disambiguate duplicates. An ambiguous match (several namesakes, no id given) fails rather than guessing, and so does an absent one.

`attachment-update` takes no id: it resolves purely by filename, and — unlike `attachment-download` — does not refuse on multiple namesakes. It uploads the new file first, then deletes every same-named attachment it supersedes, each delete verified gone; an absent match is a first upload instead, reported as `replaced: 0`. Confirm the target first — the deletes are real and there is no undo. That upload-then-delete order is the safety margin: an upload that fails (a timeout, a 413, a permission change) leaves the old artifact(s) still attached, where deleting first would have left the issue with nothing. There is no standalone `attachment-delete` verb: this seam has no way to remove an artifact from an issue's durable record without replacing it with something.

## References

- `references/attachments.md` — transcript render/scan/redact/upload/verify.
- `references/migration.md` — GitHub-issue → Jira migration (separate workflow, not used in the loop).
