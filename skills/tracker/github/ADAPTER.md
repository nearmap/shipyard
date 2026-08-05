# GitHub adapter

Implements the tracker contract (`../CONTRACT.md`) against GitHub Issues + Projects v2. Select with `tracker: "github"`.

Every canonical verb is one call to the `sy` MCP server's tool of the same name; the server dispatches to this adapter's implementation in `sy_tools/tracker/github/adapter.py`, which drives `gh` under the hood and offloads each blocking call to a worker thread. This file documents what GitHub does with each verb and where its behaviour is GitHub-specific — it is not a list of commands to run instead. There is no maintained `gh` recipe for any verb, by design: see `../CONTRACT.md`, "Every verb is one MCP tool call", for the tool-name resolution rule and why the second path is gone. Credentials stay `gh`'s own business; nothing reads, passes, or echoes a token.

**Works the same on a personal (user-owned) project and an org project — no organization required.** Issue **Type** and **Status** are both driven as **Projects v2 single-select fields**, not native `issue_type` (which is org-only) and not labels. Sub-issues, dependencies, comments, and the board all work on GitHub Free for a personal private repo. Shipyard is opinionated about the two fields the project must carry (below).

## Preflight (fail fast before any work)

1. **`gh` ≥ 2.94.0** — sub-issue and dependency flags landed in the CLI there. Check `gh --version`.
2. **Authenticated:** `gh auth status` with `project` + `read:project` scopes (`gh auth refresh -s project,read:project` if missing).
3. **Config present** — one call covers this and step 4: the `validate_config` tool. Resolve the two values once and reuse them:
   ```
   PROJECT = get_config {"key": "tracker_config.project"}            → <owner>/<number>, the Projects v2 board. Required.
   REPO    = get_config {"key": "tracker_config.repo", "default": ""} → <owner>/<repo>. Optional, so it is read with a
                                                                       default; empty means the current repo.
   ```
   Board owner is `@me` or your login for a user-owned board, or the org login for an org board. Pass `gh -R "$REPO"` on every issue command when `REPO` is non-empty.
4. **The five column names are set** (shared across trackers; from `.shipyard/config.json`): `columns.backlog`, `columns.ready`, `columns.in_progress`, `columns.in_review`, `columns.done`. The helper fails loudly if any is unset.
5. **The project has the two required single-select fields** (create once; see `docs/github-setup.md`):
   - **`Status`** with an option for each of the five columns above (names matched case-insensitively).
   - **`Type`** with options `Epic`, `Task`, `Bug`.

**Preflight (the adapter's declared hook for `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`).** The canonical `preflight` verb is the real, live check: it confirms `gh` is installed and authenticated, and confirms Projects v2 is actually reachable rather than merely named — every `set-status` and every `Type` write goes through the board, so a `repo`-only credential passes an authentication check and then dies on the first board write. Reachability is confirmed by reading the configured board, on every path: a scope is a property of the credential, and the board it points at can still have been deleted, renamed, or made invisible to it. It also checks the `project` scope where the token has scopes to check, as the cheap pre-check that names the one credential fault diagnosable without touching the board; where it does not (a fine-grained PAT or an App token prints no `Token scopes:` line at all, which cannot be read as "unscoped" without failing a working setup) the board read is the whole of the evidence and `scopes` comes back as null rather than as an invented list. The board read does not repeat on every invocation, because the `preflight` tool gates itself on the shared cache and records its own success, so one call is the caller's whole obligation and `force` is how a caller whose config just changed demands the live read anyway (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`). This adapter names no secret in the environment, so the cache here is keyed on the plugin build, `github` and the resolved config alone — which already covers the board and the repo.

Unlike Jira, `gh auth` is the single mechanism both this board read and `/sy:pr`'s code-host operations share, so a working `gh auth status` (step 2) is rarely a fresh gap by the time someone reaches this adapter — the board reachability check is the part actually specific to Shipyard's config and worth caching.

A drifted board option — the real `Status` column or `Type` option name no longer matching the `columns.*` config key naming it — fails loudly against the board's own field options, on the read path as well as the write path, listing what the board does offer. It is never answered with an empty page: `count: 0, is_last: true` from a board that has work on it is the one wrong answer a caller cannot tell from an empty queue. Raw project, field and option node IDs are the adapter's business and appear nowhere above it.

## Type and status mapping (both are Projects v2 single-select fields)

`Type` options are fixed (`Epic`/`Task`/`Bug`, case-insensitive). `Status` options are the five per-repo column names, read from config, so the table shows the config source:

| Canonical type | `Type` option | | Canonical status | `Status` option (from config) |
|---|---|---|---|---|
| `epic` | Epic | | `backlog` | `columns.backlog` |
| `task` | Task | | `ready` | `columns.ready` |
| `bug` | Bug | | `in-progress` | `columns.in_progress` |
| | | | `in-review` | `columns.in_review` |
| | | | `done` | `columns.done` |

A field/option value only exists on an issue **once it is a project item**, so any verb that writes one adds the issue to the board first if it is not already on it: `create-issue` sets `Type` on creation, `set-status` sets `Status`, and `type-convert` rewrites `Type` on an issue that already exists.

Both writes are verified by re-reading the board, because `gh project item-edit` reports success whether or not the value changed and a card that did not move is exactly the failure a caller cannot see. The re-read is retried with backoff: the board's item list is eventually consistent, and a card added and edited moments earlier can be entirely absent from the very next read, then present with the right value a second later. It stays bounded — a genuinely unset field still fails.

`done` is also set automatically by native automation on issue close / PR merge; still call `set-status ... done` for parity and for boards without automation.

## Rich text

Markdown passthrough — GitHub renders Markdown natively. No conversion step.

## Verb behaviour on GitHub

Everything below is GitHub-specific behaviour a caller can rely on. Where a verb is unremarkable — `update-issue` replaces the body, `assign` self-assigns and reports the resolved login — it is not listed.

An issue's opaque id is its **URL** in everything this adapter returns. `gh` accepts a URL wherever it accepts a number, and adding a card to the board accepts nothing else, so the URL is the one reference every call works with; a number or `#number` is accepted from a caller and resolved. The GraphQL node id is deliberately never used as an id — no issue command accepts one. Writes are scoped to `tracker_config.repo` when set, otherwise to the repository resolved from the working directory, in either case resolved by `gh` itself so any spelling it accepts names the same repository.

- **`create-issue`** creates the issue and then puts it on the board with its `Type` set. The type is mapped before the write, so an unknown canonical token cannot leave an issue created with no type on it. Passing `parent` is the canonical verb **`create-child`**, using GitHub's native sub-issue relation — which works on GitHub Free for a personal private repo.
- **`get-issue`** reads the issue's native fields and takes `Type`/`Status` from its board item, because the issue read exposes no project single-select value. Relations (`children`, `dependencies`) come back as reference lists; a relation present but unreadable fails rather than coming back as "nothing is blocking it", which reads identically to a genuinely unblocked issue. `gh` reads each of those relations one page deep (100 sub-issues, 50 blocked-by), so `children_truncated` and `dependencies_truncated` report whether that cap cut anything off — a clipped list is indistinguishable from a complete short one, and only one of them means what a caller acts on.
- **`find-issues`** has no cursor, so `next_page_token` is always null rather than one that cannot be resumed. With no status or type filter it is one repository page. **With a status or type filter the board is the candidate set**, enumerated board-first, and `text` is matched here — case-insensitively, as a substring of title or body — rather than through GitHub's search. That is a deliberate divergence from server-side search ranking and syntax, in exchange for a result set complete for the board instead of silently capped at the Search API's thousandth row. Three properties are the caller's to rely on: only issues come back, never a pull request or draft card sharing the column; the page is scoped to one concrete repository, never the board at large; and the per-candidate reads are bounded, past which the call fails saying what to narrow rather than returning a page it cannot honestly call complete.
- **`add-dependency`** uses GitHub's native blocked-by relation and re-reads it to prove it took.
- **`add-label`** returns every label the re-read reports, so nothing looks dropped. A label that does not exist on the repository is rejected rather than created.
- **`link-parent`** re-parents through the native sub-issue relation.
- **`post-comment`** takes the comment's id from the URL the write printed. `post-log` is this same verb carrying only a fenced JSON block, and `link-pr`'s durable half is it carrying the PR URL. A `shipyard.ship_metrics.v1` block is schema-validated before anything is posted; see `../CONTRACT.md`.
- **`link-pr`**: reference the issue from the PR body as a plain `#<NUMBER>`, **not** a closing keyword — the done transition is owned by native project automation on merge, not by the PR text.
- **`type-convert`** rewrites the board `Type` field on an existing issue, verified by the same bounded re-read every board write uses.

### `attach-artifact` and the attachment lifecycle — gist + link (deliberate asymmetry)

GitHub issues have no CLI-scriptable file attachment, so the artifact is uploaded as a **secret** (private) gist and linked from a comment on the issue. Hand the rendered path to the `attach-artifact` tool: it checks the gate and runs both sanitisation passes — the same ones, in the same order, as on the Jira path — before creating the gist, and returns the gist URL as its evidence. The caller names no tracker; the asymmetry lives here, in the adapter. Privacy is verified by reading the created gist back rather than assumed from the flags passed: a public gist would publish a transcript irrevocably.

`attachment-update` is the other uploading verb and runs the identical gate and both passes before it writes. There is no unscanned upload path.

The lifecycle verbs — `attachment-download` and `attachment-update` — act on that gist, which they locate from the link comment `attach-artifact` posted. `attachment-download` resolves by artifact filename, taking a gist id instead to disambiguate; an ambiguous match (several namesakes, no id given) fails rather than guessing, and so does an absent one.

`attachment-update` takes no id: it shares the same filename-based lookup, but with no id ever available to it, so it refuses just the same on an ambiguous match — unlike Jira, this adapter has no way to replace more than one namesake in a single call. An absent match is a first upload instead: the same gist, privacy re-read and link comment `attach-artifact` writes, reported as `replaced: 0`. `attachment-update` replaces before it verifies — there is no undo. There is no standalone `attachment-delete` verb: this seam has no way to remove an artifact from an issue's durable record without replacing it with something.

Reference the returned gist URL from the `# Claude Code ship metrics` comment (`transcript_attachment: <gist-url>`). A skipped call means no gist exists — say so rather than inventing a URL.

## Deliberate asymmetries vs Jira

- **Type and status live on the Projects v2 board, not the issue.** An issue must be a board item to carry a Type/Status; Shipyard adds it on create. This is what lets one adapter serve both personal and org projects with no native issue types.
- **Done transition** is driven by native Projects automation (issue close / PR merge → Done); the ship/gate path still calls `set-status done` for parity.
- **Transcript attachment** is a private gist link, not a native file attachment.

## References

- `docs/github-setup.md` — creating the user-owned (or org) board with the required `Type` and `Status` single-select fields, options, and automations.
