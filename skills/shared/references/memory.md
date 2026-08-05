# Durable cross-session memory

A trap learned in one session should not have to be relearned in the next repo, next month. This reference defines the one durable, user-global memory surface Shipyard maintains — lessons about tools, skills, and workflow mechanics that outlive any single ticket — and the discipline for writing to it and reading it back.

The store is owned by the plugin, not the repo: one Markdown file per lesson plus a greppable `index.md`, managed only through the `sy` MCP server's memory tools, never hand-edited (tool names resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`):

```
memory_add {"title": "<one-line lesson>", "scope": "<tool/skill/workflow area>", "tags": "<a,b>", "body": "<what to do differently and why>"}
memory_search {"query": "<term>"}
memory_list {}
```

Storage root: whatever the resolver reports for `memory.dir` (see `docs/configuration.md`). It is cross-repo by design; never write repo paths or secrets into a lesson. Writes are idempotent — re-adding the same title replaces the entry — so a retry never duplicates.

## When to write

At the `/sy:ship` retrospective (and after any session that earned one the hard way), distill **at most a few** lessons that are all three of:

- **durable** — will still be true next month, not tied to this branch's state;
- **cross-cutting** — about a tool, CLI, model, skill, or workflow mechanism, not this repo's business logic;
- **actionable** — states what to do differently, not just what happened.

Examples of the right altitude: a CLI flag whose semantics are inverted relative to its docs; an agent-dispatch parameter that silently falls back to a default on resume; an automated reviewer whose identity differs across API surfaces. The wrong altitude: repo trivia, one-off ticket facts, anything a `CLAUDE.md` or the tracker already records.

## When to read

Read memory back at the start of work, before decisions harden: `/sy:plan` and `/sy:spec` during their early research, and `/sy:ship` at START. `memory_list` is cheap (the index is one small file); `memory_search` with the tools and surfaces the task touches when the index is long. A hit that bears on the task enters the working brief as a known anchor.

## Curation

Memory earns its keep only while it stays small enough to read. Before adding, `memory_search` for an existing entry and extend it (same title, replaced body) rather than writing a near-duplicate. Delete-by-hand is deliberate friction: a lesson wrong enough to remove is usually a lesson to rewrite with what replaced it. No mechanism enforces this — the discipline is the convention.
