# Recycling a large main-thread context

A main-thread request that follows a gap longer than the prompt-cache TTL re-writes the whole context into cache. The TTL is 5 minutes, or an hour where the session's main thread gets the one-hour cache. Shipyard's `context_watch` hook adds a `sy: main-thread context is about <N>k tokens` line when the main thread crosses 250k, and again at each 150k step above that. That line is the only size signal: never estimate the size yourself. This applies to `/sy:ship`, `/sy:spec`, `/sy:spike` and `/sy:mvp`.

## At 250k: checkpoint before you wait

After that line has appeared, write the skill's checkpoint at the boundary its row names, then end the turn with one Optional suggestion (`user-interaction.md`): `/clear`, then `<resume command>`, resumes from `<checkpoint>`.

| Skill | Boundary | Checkpoint | Resume command |
|---|---|---|---|
| `/sy:ship` | after the parent has processed a worker's return and before it dispatches the next one, so no worker is in flight | `ship-state.yaml`, already current | `/sy:ship <key>` |
| `/sy:spec` | §7 Step 1's sign-off question, the only point where the debate and spec-gate passes have both run | `plan-draft.md` in the directory `scratch_dir {"identifier": "<key>"}` reports: both halves of the drafted plan and the dispositioned findings | `/sy:spec <key>` |
| `/sy:spike` | between experiments, before a Question | a `# Spike checkpoint` comment: the question, the baseline's status, each experiment run with its result and artifact path, and the next step | `/sy:spike <key>` |
| `/sy:mvp` | between loop steps, before a Question | a `# MVP ledger` entry | `/sy:mvp <key>` |

## At 400k: recycle spike and mvp

In `/sy:spike` and `/sy:mvp`, once the line reports 400k or more, start no new experiment or loop step. Write the checkpoint, then end the turn with one `## Action needed` block: run `/clear`, then the resume command.
