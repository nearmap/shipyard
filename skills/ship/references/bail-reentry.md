# Bail and re-entry

What `/sy:ship` records when a run bails to `/sy:spec`, what you see when it stops, and how the next `/sy:ship` picks the run back up. `${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Worker contract and § State router route into this file; `${CLAUDE_PLUGIN_ROOT}/skills/spec/SKILL.md` § Revising after a bail reads the record it writes.

## The bail record

On every `bail-to-spec` — a worker's return, a `reason: max_gate_rounds` or `reason: plan_contract_choice` answer that picks re-spec, a pre-gate checkpoint reply that asks for a fresh `/sy:spec` revision, a closed blocker PR, or an escalation threshold reached — the parent writes one `bail` mapping into `ship-state.yaml` with `ship_state_update`, in the same write that clears `phase_active`:

```yaml
bail:
  id: <task>-v<plan_version>-<phase>-r<gate round, or 0>
  kind: plan-contract | false-premise | owner-respec | recurrence | blocker-closed | threshold | pregate-respec
  phase: START | BUILD | GATE | parent
  plan_version: <the plan version that was invalidated>
  reason: <one sentence: what is wrong with the plan, not what the run did>
  invalidates: [<plan steps, invariants or acceptance criteria, by the plan's own labels>]
  findings: [<finding id + root_cause_key>]
  owner_options: [<the options the worker or triage listed, with the owner's steer if one was given>]
  pending_fixes: <dispositions file path(s) whose accepted fixes were not applied, or null>
  verdict: <verdict file path(s), or null>
  branch: <branch>
  pr: <PR number, or null>
  head_sha: <pushed head at the bail>
  resolved_by: null
```

The worker's `BAIL-TO-SPEC` return supplies `kind`, `reason`, `invalidates` and the paths; the parent supplies the rest from state. Then the parent posts the same record to the Task as one `post-comment`: `human` is `# Bail to spec — <id>` with the reason and what the revision must decide, in two or three sentences; `agent_detail` is the YAML above. The tracker comment is what `/sy:spec` reads, so a revision run on another machine, or after the scratch directory is gone, still has it.

## The stop message

The parent ends the turn with one `## Action needed` block rendered from the record, never composed freehand:

```text
## Action needed

<task> stopped at <phase> <round> and needs plan v<N+1>: <reason>.
Invalidated: <invalidates>. Owner options: <owner_options, or "none listed">.
PR #<pr> stays a draft at <head_sha>; <n> accepted fixes are parked for the revision.

Run `/sy:spec <task>` — it reads the bail comment, so nothing needs pasting. Then `/sy:ship <task>` resumes this branch.
```

## Planned re-entry

On a resume, § State router's pin comparison routes here whenever the plan version moved:

- `bail` set with `resolved_by: null`, and the new plan's `resolves bail` field names `bail.id`: a planned re-entry. In one `ship_state_update` write, set `bail.resolved_by` to the new version, stamp the new pin, set `phase_checkpoint` to `null`, and set `gate_rounds_budget_base` to the current `gate_rounds_total`, so the revised plan starts with a full round budget while `gate_rounds_total` stays monotonic. Then dispatch START as a resume against the new plan. It refreshes the standards contract and the plan-base drift check, and reuses the recorded branch, worktree and PR. Then dispatch BUILD, which builds the new plan on the existing branch head and folds in the plan's `carried fixes`.
- `bail` set and unresolved, but the new plan does not name it: stop with `## Action needed`. Plan v<N> does not say it resolves bail `<id>`, so re-run `/sy:spec <task>` to record that it does, or to say why it does not apply.
- No unresolved `bail`: the plan was revised by hand mid-run. Handle it as a planned re-entry without the `bail` write.

A resume with the pin unchanged and `bail` unresolved never dispatches. It re-renders the stop message above.
