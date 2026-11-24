# Recycling a large main-thread context

A main-thread request that follows a gap longer than the prompt-cache TTL (5 minutes by default) re-writes the whole context into cache. At 300k tokens, one gap like that costs more than dozens of ordinary turns. Most gaps happen while the user is away, while an `AskUserQuestion` is open, or while a long background run is in flight. Shipyard's `context_watch` hook adds a `sy: main-thread context is about <N>k tokens` line when the main thread crosses 250k, and again at each 150k step above that. That line is the only size signal: never estimate the size yourself.

## At 250k: checkpoint before you wait

After that line has appeared, before any turn that asks a Question (`user-interaction.md`) or ends while a background run is expected to take more than a few minutes:

1. Write the skill's checkpoint (table below), so a fresh session can resume from it without this context.
2. End the turn with one Optional suggestion (`user-interaction.md`): `/clear`, then `<resume command>`, resumes from `<checkpoint>`.

Never write a checkpoint mid-step. Write it at a phase boundary, where the next step needs only what the checkpoint holds.

| Skill | Checkpoint | Resume command |
|---|---|---|
| `/sy:ship` | `ship-state.yaml`, already current after every phase | `/sy:ship <key>` |
| `/sy:spec` | the drafted plan, both halves plus the dispositioned debate and spec-gate findings, written to `<scratch>/<key>/plan-draft.md` | `/sy:spec <key>` |
| `/sy:spike` | a `# Spike checkpoint` comment: the question, the baseline's status, each experiment run with its result and artifact path, and the next step | `/sy:spike <key>` |
| `/sy:mvp` | a `# MVP ledger` entry | `/sy:mvp <key>` |

## At 400k: recycle spike and mvp

In `/sy:spike` and `/sy:mvp`, once the line reports 400k or more, start no new experiment or loop step. Write the checkpoint, then end the turn with one `## Action needed` block: run `/clear`, then the resume command. Spike and mvp sessions are the ones that stay open for days, and each idle gap at that size costs several dollars.
