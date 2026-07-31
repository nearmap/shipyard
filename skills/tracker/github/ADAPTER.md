# GitHub adapter

Implements the tracker contract (`../CONTRACT.md`) against GitHub Issues + Projects v2 via the `gh` CLI. Select with `tracker: "github"`. Deterministic and already authenticated; no MCP server.

**Works the same on a personal (user-owned) project and an org project — no organization required.** Issue **Type** and **Status** are both driven as **Projects v2 single-select fields**, not native `issue_type` (which is org-only) and not labels. Sub-issues, dependencies, comments, and the board all work on GitHub Free for a personal private repo. Shipyard is opinionated about the two fields the project must carry (below).

## Preflight (fail fast before any work)

1. **`gh` ≥ 2.94.0** — sub-issue and dependency flags landed in the CLI there. Check `gh --version`.
2. **Authenticated:** `gh auth status` with `project` + `read:project` scopes (`gh auth refresh -s project,read:project` if missing).
3. **Config present** — one command covers this and step 4: `python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" validate`. Resolve the two values once and reuse them:
   ```bash
   PROJECT=$(python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get tracker_config.project)   # <owner>/<number>, the Projects v2 board. Required.
   REPO=$(python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get tracker_config.repo)          # <owner>/<repo>. Optional; empty means the current repo.
   ```
   Board owner is `@me` or your login for a user-owned board, or the org login for an org board. Pass `gh -R "$REPO"` on every issue command when `REPO` is non-empty.
4. **The five column names are set** (shared across trackers; from `.shipyard/config.json`): `columns.backlog`, `columns.ready`, `columns.in_progress`, `columns.in_review`, `columns.done`. The helper fails loudly if any is unset.
5. **The project has the two required single-select fields** (create once; see `docs/github-setup.md`):
   - **`Status`** with an option for each of the five columns above (names matched case-insensitively).
   - **`Type`** with options `Epic`, `Task`, `Bug`.

**Preflight (the adapter's declared hook for `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`).** `gh_project.py check` is the real, live read that proves steps 2 and 5 together — the board resolves and every canonical value has a matching option — not just that config is present; it prints the board's actual options and fails loudly on a gap. Gate it behind the shared cache so it does not repeat on every invocation:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_preflight.py" check --tracker github
# exit 0 → cached fresh, skip the read below. (No --vars: this adapter holds no secret in the
# environment, and the config fingerprint already covers the board and repo.)
# exit 2 → run it now:
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/github/gh_project.py" check --project "$PROJECT"
# succeeds →
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_preflight.py" record --tracker github
```

Unlike Jira's split credential, `gh auth` is the single mechanism both this board read and `/sy:pr`'s code-host operations share, so a working `gh auth status` (step 2) is rarely a fresh gap by the time someone reaches this adapter — the board-field check above is the part actually specific to Shipyard's config and worth caching.

Do all board field/node-id work only through `${CLAUDE_PLUGIN_ROOT}/skills/tracker/github/gh_project.py`; never touch raw project/field/option IDs.

## Type and status mapping (both are Projects v2 single-select fields)

`Type` options are fixed (`Epic`/`Task`/`Bug`, case-insensitive). `Status` options are the five per-repo column names — the helper reads them from the env vars, so the table shows the env source:

| Canonical type | `Type` option | | Canonical status | `Status` option (from config) |
|---|---|---|---|---|
| `epic` | Epic | | `backlog` | `columns.backlog` |
| `task` | Task | | `ready` | `columns.ready` |
| `bug` | Bug | | `in-progress` | `columns.in_progress` |
| | | | `in-review` | `columns.in_review` |
| | | | `done` | `columns.done` |

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/github/gh_project.py" set-type   --project "$PROJECT" --issue "<url>" --type task
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/github/gh_project.py" set-status --project "$PROJECT" --issue "<url>" --status in-review
```

Each helper call adds the issue to the board if it is not yet an item, resolves and caches the node IDs, and sets the field. A field/option value only exists on an issue **once it is a project item** — so `set-type`/`set-status` are what put an issue on the board and give it a type. `done` is also set automatically by native automation on issue close / PR merge; still call `set-status ... done` for parity and for boards without automation.

## Rich text

Markdown passthrough — GitHub renders Markdown natively. No conversion step.

## Verb implementations

`gh` uses the current repo unless `-R "$REPO"` is given; include it when `REPO` is non-empty. `GHP` below is `python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/github/gh_project.py"`.

```bash
# Staging files are namespaced by the issue they target (a slug before the issue exists,
# the parent number for children), matching .scratch/<ID>-ship-transcript.txt: parallel
# sessions and successive writes must never clobber each other's staged bodies.

# create-issue  (epic): create the issue, then put it on the board with a Type (and initial Status)
url="$(gh issue create --title "<title>" --body-file .scratch/<slug>-body.md)"   # prints the URL (opaque ID)
$GHP set-type   --project "$PROJECT" --issue "$url" --type epic
$GHP set-status --project "$PROJECT" --issue "$url" --status backlog

# create-child  (task/bug under a parent issue number) — native sub-issue via --parent
url="$(gh issue create --title "<title>" --body-file .scratch/<PARENT_NUMBER>-body.md --parent <PARENT_NUMBER>)"
$GHP set-type --project "$PROJECT" --issue "$url" --type task

# get-issue: native fields from the issue + Type/Status from the board item
gh issue view <NUMBER> --json number,title,body,state,parent,subIssues,blockedBy,blocking,assignees,url
$GHP get --project "$PROJECT" --issue "<url>"        # -> {number,title,url,type,status}

# find-issues: by type/status through the board; by text via gh; children via get-issue subIssues
$GHP list --project "$PROJECT" --type epic           # all epics
$GHP list --project "$PROJECT" --status in-progress  # active leaves
gh issue list --search "<query>" --json number,title,state,parent,url

# update-issue  (replace body)
gh issue edit <NUMBER> --body-file .scratch/<NUMBER>-body.md

# assign  (self)
gh issue edit <NUMBER> --add-assignee @me

# link-parent  (re-parent — native sub-issue)
gh issue edit <NUMBER> --parent <PARENT_NUMBER>     # --remove-parent to detach

# add-dependency  (X blocked by Y — native, works on personal repos too)
gh issue edit <X_NUMBER> --add-blocked-by <Y_NUMBER>

# add-label  (preserves existing labels)
gh issue edit <NUMBER> --add-label decomposed

# post-comment / post-log  (post-log body is only fenced JSON)
gh issue comment <NUMBER> --body-file .scratch/<NUMBER>-comment.md

# link-pr  -> reference the issue from the PR body as a plain "#<NUMBER>" (NOT a closing keyword);
#            the done-transition is owned by native project automation on merge, not by the PR text.
```

Verify every write by reading it back (`gh issue view ... --json ...` / `$GHP get`); treat empty results, HTTP errors, or a mismatch as failure. Issue IDs stay opaque — pass the URL or `#<number>`.

### `attach-artifact` — gist + link (deliberate asymmetry)

GitHub issues have no CLI-scriptable file attachment, so the transcript is uploaded as a **secret** (private) gist and linked from the log comment. Hand the rendered path to the `sy` MCP server's `attach-artifact` tool: it checks the gate and runs both sanitisation passes — the same ones, in the same order, as on the Jira path — before creating the gist, and returns the gist URL as its evidence. The caller names no tracker; the asymmetry lives here, in the adapter.

Reference the returned gist URL from the `# Claude Code ship metrics` comment (`transcript_attachment: <gist-url>`). A skipped call means no gist exists — say so rather than inventing a URL. `gh gist create --desc "shipyard transcript <ID>" <FILE>` still works as a recovery path, but it uploads exactly what it is given: scan first yourself, and if safe redaction is uncertain, stop rather than publishing.

## Deliberate asymmetries vs Jira

- **Type and status live on the Projects v2 board, not the issue.** An issue must be a board item to carry a Type/Status; Shipyard adds it on create. This is what lets one adapter serve both personal and org projects with no native issue types.
- **Done transition** is driven by native Projects automation (issue close / PR merge → Done); the ship/gate path still calls `set-status done` for parity.
- **Transcript attachment** is a private gist link, not a native file attachment.

## References

- `docs/github-setup.md` — creating the user-owned (or org) board with the required `Type` and `Status` single-select fields, options, and automations.
