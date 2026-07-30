# Jira adapter

Implements the tracker contract (`../CONTRACT.md`) against Jira via the Atlassian `acli`, with a small REST helper for operations where `acli` is ambiguous or inverted. This is the default tracker (`tracker: "jira"`).

## Configuration (self-check before any work)

Required config (`.shipyard/config.json`): `tracker_config.email`, `tracker_config.site`, `tracker_config.project`. Required secret, environment only: `ACLI_TOKEN`. The split is declared machine-readably in this directory's `config-map.json`, which `sy_config.py validate` enforces. Never put tokens in command arguments, and never put one in a config file. Resolve the project once and build JQL from it, never a hard-coded key:

```bash
PROJECT=$(python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get tracker_config.project)
```

**Two independent auth mechanisms, both required.** Raw `acli jira ...` calls (most verbs below) use `acli`'s own login session, established once with `acli jira auth login` and cached under `~/.config/acli/` — entirely outside Shipyard's config. `jira_rest.py` (the REST fallback, and every attachment operation) authenticates separately with `tracker_config.email` plus the `ACLI_TOKEN` secret. A repo's shared config can supply everything except the two things that are genuinely per-person: the one-time `acli jira auth login`, and this person's own `ACLI_TOKEN` (a personal Atlassian API token that stays in the environment and never enters a config file, so no `cat` of a committed file can burn it into transcript history). `tracker_config.email` is per-person too, but it is not a secret — it belongs in the gitignored `.shipyard/config.local.json` layer. Verifying one does not verify the other.

**Preflight (the adapter's declared hook for `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`).** `acli jira auth status` proves only local credential presence, not liveness — validate both mechanisms with a real read, gated by the shared cache so this does not repeat on every invocation:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_preflight.py" check --tracker jira --vars ACLI_TOKEN
# exit 0 → cached fresh, skip both reads below.
# exit 2 → run both real reads now:
acli jira workitem search --jql "project = ${PROJECT:?}" --count              # proves acli's own login session
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" preflight     # proves email/site/project + ACLI_TOKEN via REST
# both succeed →
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_preflight.py" record --tracker jira --vars ACLI_TOKEN
```

A missing or expired `acli` login fails the first read with `acli`'s own auth error (`acli jira auth status` first, if unclear, to confirm which of the two is actually broken — it only proves credential presence, so a fresh error from the real read above is the one to act on); a missing or invalid `ACLI_TOKEN`, `tracker_config.site`, or `tracker_config.project` fails `jira_rest.py preflight` with its own error naming the problem. Either failure is the exact text `preflight.md`'s `## Action needed` block relays — never a bare crash discovered later inside an attachment upload.

## Type mapping

| Canonical | Jira type |
|---|---|
| `epic` | Epic |
| `task` | Task |
| `bug` | Bug |

Execution is flat: one tracking Epic, every executable Task/Bug directly beneath it; conceptual hierarchy lives in the Epic body/comments.

## Status mapping

Each canonical status maps to the Jira status/transition named by the shared, required per-repo column env var (the Jira workflow must have statuses with those names):

| Canonical | Jira status (transition target) |
|---|---|
| `backlog` | `columns.backlog` |
| `ready` | `columns.ready` |
| `in-progress` | `columns.in_progress` |
| `in-review` | `columns.in_review` |
| `done` | `columns.done` |

`set-status` transitions to the resolved name:

```bash
acli jira workitem transition --key <ID> --status "$(python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get columns.in_review)" --yes
```

(`--yes` only where the installed command supports it). Inspect the closure reason before treating a `done` issue as delivered — decomposed/superseded closure is not delivery.

**Transition fallback.** When a transition is rejected (the target status is not reachable from the current state, or the name does not resolve), do not retry blind and do not treat the old status as acceptable: list the valid transitions from the current state and surface them —

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" transitions <ID>
```

— then transition to the correct reachable target (or surface the workflow gap loudly if none maps to the requested canonical status).

## Rich text: Markdown → ADF

Jira comments and descriptions are ADF. Stage the Markdown, convert, then write:

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/md_to_adf.py" .scratch/<ID>-body.md > .scratch/<ID>-body.adf.json
```

Namespace every staging file by the issue key it targets (`<ID>`; before a key exists — issue creation — use the parent key or a goal slug), matching the `.scratch/<ID>-ship-transcript.txt` convention: parallel sessions and successive writes must never clobber each other's staged bodies. Load `references/adf.md` for converter setup/verification.

## Verb implementations

```bash
# create-issue  (epic)
acli jira workitem create --project "${PROJECT:?}" --type Epic \
  --summary "<title>" --description-file .scratch/<slug>-epic.adf.json

# create-child  (task/bug under a parent)
acli jira workitem create --project "${PROJECT:?}" --type Task \
  --parent <PARENT_ID> --summary "<title>" --description-file .scratch/<PARENT_ID>-task.adf.json

# get-issue  (first rung of the read ladder below)
acli jira workitem view <ID> --fields '*all'
acli jira workitem comment list --key <ID>

# update-issue  (replace body)
acli jira workitem edit --key <ID> --description-file .scratch/<ID>-body.adf.json

# find-issues  (JQL against the configured project)
acli jira workitem search --jql "project = ${PROJECT:?} AND status = 'In Progress'"

# assign
acli jira workitem assign --key <ID> --assignee @me

# post-comment / post-log  (both are comments; post-log carries only fenced JSON)
acli jira workitem comment create --key <ID> --body-file .scratch/<ID>-comment.adf.json

# link-pr  (Jira dev-panel link is driven from the commit/PR; record the URL in a comment too)
#   PRs surface in the Jira development panel when the branch/commit names the key.
#   Post the PR URL as a comment so it is durable regardless of dev-panel wiring.
```

### `get-issue` read ladder (acli → REST → MCP, never a silent empty read)

`acli`'s `view --fields '*all'` truncates relational fields — parent, issue links, and labels can render empty even when set — and some field selections are rejected outright. When a field you need is missing/empty in the `acli` view, or the command rejects, fall through in order:

1. **REST** — raw JSON, untruncated:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" get <ID> --fields parent,issuelinks,labels,status,issuetype
   python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" comment-get <ID> <COMMENT_ID>   # one full comment body
   ```

2. **Atlassian MCP** — when the MCP server is connected in this session, `getJiraIssue` (and its comment/transition siblings) is the last resort.

If every rung fails, fail loudly with the last error. An empty read is never evidence the field is empty — relational reads (blockers, parents) drove real false negatives before this ladder existed.

### `add-dependency` (X blocked by Y) — use the REST helper

`acli`'s `link --out`/`--in` flags are inverted relative to Jira's model (empirically `--in` is the blocker), so it silently creates the reverse link. The helper sets direction via the REST model and verifies it:

```bash
# ID_Y blocks ID_X  (ID_X is blocked by ID_Y)
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" link --blocker <ID_Y> --blocked <ID_X>
```

Deleting a link is unambiguous on `acli`: `acli jira workitem link delete --id <id> --yes`.

### `add-label` — use the REST helper

`acli` append-vs-replace label semantics are ambiguous. The helper reads the current set and writes it back plus the requested label:

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" add-label <ID> decomposed
```

### `link-parent` — use the REST helper

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" set-parent <ID> <PARENT_ID>
```

Ask before crossing into another project or portfolio hierarchy.

### `attach-artifact`

Jira supports native work-item attachments. Scan first (secrets), then upload with the helper so credentials never appear in argv. Load `references/attachments.md` for the render/scan/upload/verify flow:

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" attach <ID> .scratch/<ID>-ship-transcript.txt
```

Attachment lifecycle beyond upload (all resolve by filename with an exactly-one match rule; pass `--id` to disambiguate duplicates):

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" attachment-download <ID> <FILENAME> [--output <PATH>]
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" attachment-update <ID> <FILE>     # replace-by-filename: deletes same-name attachments, re-uploads, verifies
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" attachment-delete <ID> <FILENAME> [--id <ATTACHMENT_ID>]
```

`attachment-delete` is destructive and `attachment-update` deletes before it uploads — confirm the target first; there is no undo.

### `type-convert` (Task ↔ Epic) — best-effort, loud failure

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py" type-convert <ID> Epic   # or Task
```

Rewrites the work item's type in place and verifies by reading it back. Some site workflows restrict type changes (required fields, hierarchy rules); the helper then fails loudly rather than leaving the type silently unchanged — treat it as best-effort, and fall back to create-new + link + close-old when the workflow refuses. Irreversible side effects (parent links, board membership) follow the type; confirm before converting.

## References

- `references/adf.md` — Markdown→ADF converter setup/verification.
- `references/attachments.md` — transcript render/scan/redact/upload/verify.
- `references/accounting.md` — usage/metrics JSON shapes and standalone-comment rules.
- `references/acli-cookbook.md` — extended commands and REST parent updates.
- `references/migration.md` — GitHub-issue → Jira migration (separate workflow, not used in the loop).
