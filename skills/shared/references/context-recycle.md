# Recycling a large main-thread context

Shipyard's `context_watch` hook adds a `sy: main-thread context is about <N>k tokens` line when the main thread crosses 250k, and again at each 150k step above that, once per session. That line is the only size signal: never estimate the size yourself. This applies to `/sy:ship`, `/sy:spec`, `/sy:spike`, `/sy:mvp` and `/sy:retro`.

## At 250k: checkpoint before you wait

After that line has appeared, write the skill's checkpoint at the boundary its row names, then add one Optional suggestion (`user-interaction.md`) before the turn's Question or `## Action needed` block, which stays last: `/clear`, then `<resume command>`, resumes from `<checkpoint>`.

| Skill | Boundary | Checkpoint | Resume command |
|---|---|---|---|
| `/sy:ship` | a wait the run already has: the pre-gate checkpoint, merge authorization, a `blocked` return, the `max_gate_rounds` question, or the end of the run. Never at another `needs-decision` question, whose options live only in the worker's return, and never mid-handover | `ship-state.yaml`, already current | `/sy:ship <key>` |
| `/sy:spec` | §7 Step 1's sign-off question, the only point where the debate and spec-gate passes have both run | `plan-draft.md` in the directory `scratch_dir {"identifier": "<key>"}` reports: both halves of the drafted plan, the pre-pass sign-off half, and the dispositioned findings. Once the file exists, rewrite it at every return to Step 1 | `/sy:spec <key>` |
| `/sy:spike` | between experiments, before a Question | a `# Spike checkpoint` comment: the question, the baseline's status, each experiment run with its result and artifact path, and the next step | `/sy:spike <key>` |
| `/sy:mvp` | between loop steps, before a Question | a `# MVP ledger` entry | `/sy:mvp <key>` |
| `/sy:retro` | after step 3's `analyze`, before writing the analysis | the run's output directory, already current | `/sy:retro discover` |

## At 400k: recycle spike and mvp

In `/sy:spike` and `/sy:mvp`, once the line reports 400k or more, start no new experiment or loop step. Write the checkpoint, then end the turn with one `## Action needed` block: run `/clear`, then the resume command.
