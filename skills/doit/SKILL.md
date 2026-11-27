---
name: doit
description: >-
  Take one small, low-risk task from a goal to a reviewable PR in one session: a short
  spec, a light debate, one sign-off, then /sy:ship unchanged. Work that needs deep research, a risk
  lens or an owner decision goes to /sy:spec instead.
argument-hint: "[a goal, or an existing task key (<task>)]"
disable-model-invocation: true
effort: high
---

Write a short spec for one small task, get one sign-off, post it as the Task's plan, then ship it in this session. The plan has the same shape as a `/sy:spec` plan, so `/sy:ship` reads it unchanged and a session that ends after the plan is posted resumes with `/sy:ship <task>`. Never merge automatically.

Before anything else, run the tracker preflight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/preflight.md`). A failure stops here with its single `## Action needed` block.

$ARGUMENTS

## 1. Scope it

Plan against fresh `origin/main`. Do `/sy:spec` §1's premise and prior-work check, and stop on its terms when the premise fails (`${CLAUDE_PLUGIN_ROOT}/skills/spec/SKILL.md`). Read an existing Task's body and comments directly. Read the surface directly: doit dispatches no `sy:sweep` and no `sy:trace`. Read durable memory early (`memory_list`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/memory.md`), and refute a lesson this research contradicts with `memory_refute`. Resolve standards in the `repo-standards` delegate, as `/sy:spec` §3's first bullet does.

## 2. Route bigger work to /sy:spec

Stop and route to `/sy:spec` when any of these holds:

- the standards contract's `LENSES` lists any lens;
- the surface is too large to read directly, or one decisive span stays unverified after a direct read;
- an open decision changes scope, design or acceptance and needs the owner;
- the work does not fit one PR.

Doit runs a narrowed `sy:debate` pass and skips the `sy:spec-gate` review, and these conditions are what keep that safe. Send a status update that names the condition and the evidence, then end with `/sy:spec <the same argument>`. Write nothing to the tracker on this path.

## 3. Draft the short plan

Draft both halves `/sy:spec` §7 defines, with every field it requires: `/sy:ship` and its workers read those fields by name. §7's requirement that spec-gate runs first, and its `reviewer orientation` question, do not apply here: this section replaces them. Write each one as short as the task allows, under `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/context-economy.md`.

- Before the sign-off, run the `sy:debate` pass over the core decision unconditionally, with `scale: small` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/debate.md`). The strongest rejected alternative comes from it, as in `/sy:spec` §7. A `CONTESTED` item that needs research beyond a direct read routes as §2 does.
- A Bug keeps `/sy:spec` §3's standing regression obligation.
- `pre-gate checkpoint` is `none` unless the user asks for one.
- When `skills.reviewer` resolves non-null (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`), write `reviewer orientation` yourself and show it in the sign-off summary. Do not ask a separate question.
- The ship profile line follows `/sy:spec` §7. `sy:gate` stays at its frontier floor, and BUILD at least opus.

For a new goal, also draft the Task: type (Bug for a defect fix, Task otherwise), parent Epic, and a body of a few sentences, per `/sy:spec` §2. Ask for the Epic through `AskUserQuestion` only when research cannot infer it.

Run `/sy:tighten` over the sign-off half before it is shown, with the `/sy:ship` half as its companion, and over the `/sy:ship` half before it is posted.

## 4. One sign-off

Send the sign-off summary as direct text first: what this builds, why this way, the risks, and what it leaves out. Then close the turn with one `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`): approve and ship / request changes / take it to `/sy:spec` / other. A debate `values-call` replaces approve and ship with one approve option per candidate. Name what approval authorizes: create the Task under its Epic when the goal is new, post the plan as one new `# Execution Plan v<N>` comment, set the Task `ready`, then run `/sy:ship`, which branches, pushes, opens a draft PR and moves the Task through its statuses. Do not infer approval from a reply that does not answer the question.

Request changes revises the draft and returns to this step. Take it to `/sy:spec` stops as §2 does.

## 5. Post and ship

1. When the goal is new, create the Task under its Epic and read it back.
2. Post the plan, verify that it is the highest version, and set the Task `ready`, as `/sy:spec` §7 Step 2 items 2-4 do.
3. Read `${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` and follow it in full for the Task key, starting with its preflight. From here `/sy:ship` owns the run, including every question it asks and its merge rule.
