# Capture, checkpoint, handoff

## Epic body = current map

Re-render:

- North Star;
- conceptual horizon ladder, deepened where evidence warrants;
- completed branches marked done;
- current active set (≤ the resolved `plan.max_active_tasks` cap, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`) with Task keys and kickoff lines;
- queued/future work marked not yet decomposed;
- critical path, blockers, and parallel-safe set.

## Decision log = adaptation delta

For each planning run that changes the tracker, append one clear human-facing comment. Keep it short and plain: one short sentence or single-line dot point per item below, no nested outline and no restated roadmap — a reader should take in the whole delta at a glance. That bar is `${CLAUDE_PLUGIN_ROOT}/skills/tighten/SKILL.md`'s durable-comment human half; run the pass over these bullets rather than judging brevity by eye. These bullets are the comment's `human` part; the footer below is its `agent_detail`, which is what re-entry reads back.

- what changed and why;
- what shipped since prior checkpoint;
- what retrospectives and usage evidence taught you;
- Tasks created, decomposed, blocked, or superseded;
- how the remaining path changed.

End with, as `agent_detail`:

```text
Plan checkpoint: <n>
Processed retros: TASK-123, TASK-124
Current active leaves: TASK-125, TASK-126
```

## Handoff

Give `/sy:spec <task>` for each active leaf and identify parallel-safe work. Stop. Re-enter with `/sy:plan <epic>` after meaningful shipping evidence.

End with a concise TL;DR: Epic key/link, active Tasks and `/sy:spec` kickoffs, blockers/parallel-safe set, and re-entry line.
