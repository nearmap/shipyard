# GitHub tracker setup

One-time setup for the GitHub tracker adapter (`"tracker": "github"`). After it, `/sy:plan`, `/sy:spec`, and `/sy:ship` drive the board through the `sy` MCP server's canonical verbs — you never touch GraphQL or node IDs by hand.

**No organization is required.** Shipyard drives issue **Type** and **Status** as Projects v2 single-select fields, which work identically on a personal (user-owned) project and an org project. It does not use GitHub's native `issue_type` (org-only) or labels. Sub-issues, dependencies, comments, and the board all work on GitHub Free for a personal private repo. This is the same setup whether the board is owned by `@me` (a user) or an org — only the `--owner` value differs.

Prerequisites: `gh` ≥ 2.94.0 (`gh --version`) and `gh auth status` with `project` + `read:project` scopes (`gh auth refresh -s project,read:project` if missing).

Below, `OWNER` is `@me` (or your login) for a user board, or the org login for an org board.

## 1. Create a Projects v2 board

```bash
gh project create --owner OWNER --title "Shipyard" --format json --jq '{number, url}'
```

Note the `number` — it is the `<number>` in `tracker_config.project` below.

## 2. Status field: one option per lifecycle column

Shipyard drives **five** lifecycle columns — `backlog`, `ready`, `in-progress`, `in-review`, `done` — but you choose the option **names**. Your `Status` single-select needs one option per role. A fresh board defaults to only `Todo/In Progress/Done`; add options until you have five that map to the roles, for example `Backlog, Ready, In progress, In review, Done`. (GitHub's standard board template already uses exactly these.) Editing options is a web-UI action: open the board → click the **Status** column header → **Edit values**.

You point Shipyard at whatever you named them via env vars in step 5 (matching is case-insensitive), so there is no need to rename an existing board. Docs: [About single-select fields](https://docs.github.com/en/issues/planning-and-tracking-with-projects/understanding-fields/about-single-select-fields).

## 3. Add a `Type` single-select field with options `Epic`, `Task`, `Bug`

This is scriptable:

```bash
gh project field-create <number> --owner OWNER \
  --name "Type" --data-type SINGLE_SELECT --single-select-options "Epic,Task,Bug"
```

(Or add it in the UI: board → **+** field → **Single select**, named exactly `Type`, options `Epic`/`Task`/`Bug`.) The adapter resolves the field and options by these exact names, case-insensitively, and fails loudly with the available list on a mismatch.

## 4. Enable the built-in "→ Done" automations

The adapter relies on native done-transitions (the asymmetry vs Jira). On a new project both are enabled by default; confirm them at board → **⋯ → Workflows**:

- **Item closed → Set Status to `Done`**
- **Pull request merged → Set Status to `Done`**

Docs: [Using the built-in automations](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations). `/sy:ship` also calls `set-status ... done` for parity, so a board without these does not break.

## 5. Set the config in the repo's `.claude/settings.json`

Put this in the repo's `.claude/settings.json` `env` block — it is per-repo, so different repos on the same machine can use different boards and column names:

`.shipyard/config.json`:

```json
{
  "$schema": "https://raw.githubusercontent.com/nearmap/shipyard/main/config/schema.json",
  "tracker": "github",
  "tracker_config": {
    "project": "@me/<number>",
    "repo": "<owner>/<repo>"
  },
  "columns": {
    "backlog": "Backlog",
    "ready": "Ready",
    "in_progress": "In progress",
    "in_review": "In review",
    "done": "Done"
  }
}
```

- `tracker_config.project` is `<owner>/<number>` — `@me/<number>` (or `<username>/<number>`) for a user board, `<org>/<number>` for an org board.
- `tracker_config.repo` is optional; it defaults to the current repo.
- The five `columns.*` keys are **required** and name your `Status` options for each lifecycle role. They are the tracker-neutral column config — the Jira adapter reads the same keys. Set them to whatever your board calls those columns.

## 6. Verify, then smoke-test

Confirm the configuration resolves and the credential reaches the board (read-only):

```bash
python "${CLAUDE_PLUGIN_ROOT:-.}/scripts/sy_config.py" validate
```

Then call the `preflight` verb, which is the live half: it proves `gh` is authenticated and that Projects v2 is actually reachable with this credential, not just that config is present. Once it succeeds, `docs/smoke_mcp.py` exercises every canonical verb end to end (it creates real issues; see the script header for the opt-in and the cleanup switch).

---

## Note on organizations and native issue types

You do **not** need an org. If you already have one, an org-owned board works the same way — just set `tracker_config.project=<org>/<number>`. GitHub's native `issue_type` field (Epic/Task/Bug shown on the issue itself) is org-only, but Shipyard deliberately does not use it: the project `Type` field is the single mechanism across personal and org projects, so behaviour is identical either way and a personal private project (which cannot be moved into a Free org) is fully supported.
