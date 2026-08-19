# Durable cross-session memory

A trap learned in one session should not have to be relearned in the next repo, next month. This reference defines the one durable, user-global memory surface Shipyard maintains — lessons about tools, skills, and workflow mechanics that outlive any single ticket — and the discipline for writing to it and reading it back.

The store is owned by the plugin, not the repo: one Markdown file per lesson plus a greppable `index.md`, managed only through the `sy` MCP server's memory tools, never hand-edited (tool names resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`):

```
memory_add {"title": "<one-line lesson>", "scope": "<tool/skill/workflow area>", "tags": "<a,b>", "body": "<what to do differently and why>"}
memory_search {"query": "<term>"}
memory_list {}
memory_refute {"title": "<the stored lesson's title>", "evidence": "<what was directly observed>", "correction": "<the narrower claim that still holds, or empty to tombstone>"}
```

Storage root: whatever the resolver reports for `memory.dir` (see `docs/configuration.md`). It is cross-repo by design; never write repo paths or secrets into a lesson. Writes are idempotent — re-adding the same title replaces the entry — so a retry never duplicates; a title `memory_refute` already refuted refuses instead, since replacing it would silently destroy the refutation.

## When to write

At the `/sy:ship` retrospective (and after any session that earned one the hard way), distill **at most a few** lessons that are all three of:

- **durable** — will still be true next month, not tied to this branch's state;
- **cross-cutting** — about a tool, CLI, model, skill, or workflow mechanism, not this repo's business logic;
- **actionable** — states what to do differently, not just what happened.

Examples of the right altitude: a CLI flag whose semantics are inverted relative to its docs; an agent-dispatch parameter that silently falls back to a default on resume; an automated reviewer whose identity differs across API surfaces. The wrong altitude: repo trivia, one-off ticket facts, anything a `CLAUDE.md` or the tracker already records.

## When to refute

The moment direct observation — not a hunch, not a suspicion — contradicts a stored lesson, refute it: `memory_refute` with the title, the `evidence` that contradicts it, and a `correction` when part of it still holds. Prefer the correction. A lesson that was wrong only under some condition is more use narrowed than erased, and the condition is exactly what the next reader needs; leave `correction` empty only when nothing in the lesson survives, which tombstones it. `evidence` is required either way, because a refutation overrules an earlier session's conclusion and the next reader has to be able to re-check it. The entry is rewritten in place, never deleted, and repeat refutes converge on the same file.

`/sy:plan` and `/sy:spec` call it directly, at the point of discovery — they are the parent session. A `/sy:ship` worker cannot: only the parent holds the write, so the worker reports the candidate and the parent applies it, per the worker contract in `${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md`.

## When to read

Read memory back at the start of work, before decisions harden: `/sy:plan` and `/sy:spec` during their early research, and `/sy:ship` at START. `memory_list` is cheap (the index is one small file); `memory_search` with the tools and surfaces the task touches when the index is long. A hit that bears on the task enters the working brief as a known anchor. A `status: corrected` hit enters it like any other, since its body already states the corrected claim; a `status: tombstoned` hit is never a working anchor — it is a warning against re-deriving the conclusion someone already disproved.

## Curation

Memory earns its keep only while it stays small enough to read. Before adding, `memory_search` for an existing entry and extend it (same title, replaced body) rather than writing a near-duplicate. The store is managed only through the four tools above; hand-editing and hand-deleting are unsupported, and the index's tolerance of a vanished file is a defence against corruption, not a sanctioned workflow. A refuted anchor is never left standing: it is corrected or tombstoned through `memory_refute`, never carried forward as if it still held and never silently deleted. No mechanism enforces this — the discipline is the convention.
