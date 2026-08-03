---
name: tracker
description: >-
  Own issue-tracker mechanics behind one contract, dispatching to the configured adapter
  (Jira or GitHub Projects): hierarchy, lifecycle, dependencies, comments, machine logs,
  attachments, and decomposition. Workflow skills own decisions; this skill owns durable
  representation and mutation.
argument-hint: "[tracker operation or issue key]"
---

Own tracker mechanics behind the contract in `${CLAUDE_PLUGIN_ROOT}/skills/tracker/CONTRACT.md`. Workflow skills (`/sy:plan`, `/sy:spec`, `/sy:ship`, `/sy:spike`) decide *what* the lifecycle should do in canonical terms; this skill turns that into concrete calls against the selected tracker.

$ARGUMENTS

## Select the adapter

Resolve the tracker through the config resolver, which owns the layer precedence:

```bash
TRACKER=$(python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get tracker)
```

Then load exactly two files and use nothing else for tracker mechanics:

1. `${CLAUDE_PLUGIN_ROOT}/skills/tracker/CONTRACT.md` — the canonical verbs, statuses, and types.
2. `${CLAUDE_PLUGIN_ROOT}/skills/tracker/${TRACKER}/ADAPTER.md` — the native implementation.

This is the **single point** where tracker selection happens. No other skill or agent branches on the tracker. One command covers every presence check below, and fails with the offending key and the layer it came from:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" validate
```

It fails fast before any work when:

- `tracker` names no adapter under `skills/tracker/`;
- any required column name is unset — `columns.backlog`, `columns.ready`, `columns.in_progress`, `columns.in_review`, `columns.done` (shared by every adapter);
- the selected adapter's required configuration is absent (each adapter declares it in its own `config-map.json`);
- a retired `SY_*` environment variable is still set, which is an error rather than an override.

Report the actionable error and stop; never fall through to a default that silently writes to the wrong system.

### Liveness: cached, not just presence

The presence checks above do not prove the config is *live* — a credential can be set and still be dead. Once presence passes, run the adapter's declared preflight hook (its own `ADAPTER.md` documents what "a real read" means for that tracker), gated by the shared cache so the network read does not repeat on every invocation:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_preflight.py" check --tracker "$TRACKER" --vars <adapter's secret env var list, comma-separated; omit if it has none>
```

Exit `0` means a prior live check for this exact tracker/config is still fresh — proceed with no network call. Exit `2` means run the adapter's live-check command now; on success, record it so the next invocation gets the cached exit:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_preflight.py" record --tracker "$TRACKER" --vars <same list>
```

A failure at any step — presence or liveness — stops here with the single named `## Action needed` block `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md` defines; never a fall-through and never a crash discovered later inside a write.

## What core may ask for

Core speaks only the contract: canonical verbs (`preflight`, `create-issue`, `create-child`, `get-issue`, `update-issue`, `find-issues`, `set-status`, `assign`, `link-parent`, `add-dependency`, `add-label`, `type-convert`, `post-comment`, `post-log`, `attach-artifact`, `attachment-download`, `attachment-update`, `link-pr`), canonical statuses (`backlog`, `ready`, `in-progress`, `in-review`, `done`), and canonical types (`epic`, `task`, `bug`). Issue IDs are opaque; bodies and comments are Markdown. The adapter maps all of it.

## Conventions that live here, not in an adapter

- **Standalone machine logs.** `post-log` output (`# Claude Code usage`, `# Claude Code ship metrics`) is its own comment, never merged into prose. Generate usage from the transcript tree with `${CLAUDE_PLUGIN_ROOT}/scripts/session_usage.py`.
- **Exactly one ACTIVE plan** per task/bug; supersede explicitly and re-read to confirm.
- **Closure is not delivery.** Merged/delivered closure satisfies a dependency; decomposed or superseded closure does not — follow replacement links until delivered capability is reached.
- **Canonical decomposition** when replacing one task with smaller ones:
  1. create/approve replacements as direct children of the same epic;
  2. record replacement dependencies with `add-dependency`;
  3. `post-comment` a `# Decomposition` note with reason and replacement IDs;
  4. `add-label` the old task `decomposed` (labels preserved);
  5. `set-status` the old task `done`;
  6. ensure the old and replacement work are never simultaneously actionable;
  7. represent the old task as a conceptual parent branch in the epic roadmap.

## Durable comment types

- `# Execution Plan vN` — human-readable plan plus explicit ACTIVE/SUPERSEDED status.
- `# SEAMS` — oversized-leaf evidence for `/sy:plan`.
- `# Decomposition` — replacement IDs and terminal reason.
- `# Ship retrospective` — human-readable shipped-vs-plan lessons.
- `# Claude Code usage` — standalone JSON usage log (`post-log`).
- `# Claude Code ship metrics` — standalone JSON outcome log (`post-log`).
- Epic decision logs ending in a `Plan checkpoint` footer.

## Loop mapping

- `/sy:plan` ↔ epic roadmap + direct executable children created in `backlog`, max the resolved `plan.max_active_tasks` cap active (resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`).
- `/sy:spec` ↔ task/bug + sole ACTIVE versioned plan; the approved plan moves the task to `ready`.
- `/sy:ship` ↔ `in-progress` on build, `in-review` at a reviewable gated PR, `done` after merge;
  retrospective, standalone logs, and transcript live on the task.
- `/sy:spike` ↔ task under the selected experiment epic; `in-progress` during, `done` at verdict.
- dependencies ↔ `add-dependency` plus the closure semantics above.

## References: load only when needed

- `${TRACKER}/references/*` — adapter-specific cookbooks and setup.
