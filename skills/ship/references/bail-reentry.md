# Bail and re-entry

What `/sy:ship` records when a run bails to `/sy:spec`, what you see when it stops, and how the next `/sy:ship` picks the run back up. `${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` § Worker contract and § State router route into this file; `${CLAUDE_PLUGIN_ROOT}/skills/spec/SKILL.md` § Revising after a bail reads the record it writes.

## The bail record

On every `bail-to-spec` — a worker's return, a `reason: max_gate_rounds` or `reason: plan_contract_choice` answer that picks re-spec, a pre-gate checkpoint reply that asks for a fresh `/sy:spec` revision, a closed blocker PR, or an escalation threshold reached — the parent builds one record:

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
  anchors: [<the return's ANCHORS>]
  pending_fixes: <the bailing round's dispositions file path(s), whose accepted fixes were not applied, or null>
  verdict: <verdict file path(s), or null>
  branch: <branch>
  pr: <PR number, or null>
  head_sha: <`git rev-parse HEAD` in the recorded build worktree, pushed first when unpushed; null only when no branch exists>
```

The worker's `BAIL-TO-SPEC` return supplies `kind`, `reason`, `invalidates`, `anchors` and the paths; the parent supplies the rest from state.

The parent posts the record first, then writes it. It posts one `post-comment` on the Task: `human` is `# Bail to spec — <id>` with the reason and what the revision must decide, in two or three sentences; `agent_detail` is the record as a fenced YAML block. Then one `ship_state_update` writes `bail` as the whole mapping, `bail_resolved_by: null`, `reentry_pending: null`, and `phase_active: null`. A later bail in the same run replaces both fields the same way. A bail from a fresh START, before `ship-state.yaml` exists, posts the comment only.

## The stop message

The parent ends the turn with one `## Action needed` block rendered from the record, never composed freehand:

```text
## Action needed

<task> stopped at <phase> round <r> and needs plan v<N+1>: <reason>.
Invalidated: <invalidates>. Owner options: <owner_options, or "none listed">.
<PR #<pr> stays a draft at <head_sha>, or: no PR yet>.

Run `/sy:spec <task>` — it reads bail <id> off the Task, so nothing needs pasting. Then `/sy:ship <task>` resumes the run.
```

## Planned re-entry

`bail` is unresolved when it is set and `bail_resolved_by` is `null`. The parent reads the new plan's `resolves bail` value with one `grep -m1 'resolves bail' <plan path>` on the file `plan_file` materialised, never the whole plan. On a resume, § State router routes here:

- **`reentry_pending` set and pin unchanged.** A re-entry was written but START never finished it. Dispatch START with `RE-ENTRY: <reentry_pending>` again. With the pin changed, a newer plan landed first: route it by the bullets below.
- **Pin unchanged, `bail` unresolved.** Never dispatch. Re-post the Task comment if the Task carries none for `bail.id`, then re-render the stop message.
- **Pin changed, `bail` unresolved, and the new plan's `resolves bail` names `bail.id`.** This is a planned re-entry. Write one `ship_state_update` with:
  - `bail_resolved_by`: the new version;
  - `reentry_pending`: the bail id, which START's § Re-entry step 4 clears;
  - the new `plan_path`, `plan_comment_id` and `plan_version`;
  - `phase_checkpoint: null`;
  - `gate_rounds_budget_base`: the current `gate_rounds_total`, so the revised plan starts with a full round budget while `gate_rounds_total` stays monotonic;
  - `plan_supersede_events`: the existing list with `{from, to, bail: <id>}` appended;
  - for a `blocker-closed` bail, all five `stacked_on_*` fields set to `null`, so the run no longer stacks on the closed PR.

  Then dispatch START with `RE-ENTRY: <bail id>` in its prompt (`${CLAUDE_PLUGIN_ROOT}/skills/ship/references/start-resume.md` § Re-entry). On its `done`, dispatch BUILD: a null `phase_checkpoint` after a re-entry always classifies to BUILD, never GATE.
- **Pin changed, `bail` unresolved, and the new plan does not name it.** Re-post the Task comment if the Task carries none for `bail.id`, then stop with `## Action needed`: plan v<N> does not resolve bail `<id>`, so re-run `/sy:spec <task>`, which revises against any bail no plan has resolved yet.
- **Pin changed, no unresolved `bail`.** The plan was revised by hand mid-run. Re-enter as above, without `bail_resolved_by`, with `reentry_pending` and the `RE-ENTRY` value set to `plan-revised`, and with `bail: null` in the appended supersede event.
