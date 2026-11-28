---
name: retro
description: >-
  Learn how you work from your own session history and PR outcomes. Discovery mode builds a
  session table from your local transcripts, costs every session by token class, links sessions to
  PRs, classifies every session, scores the evidence against Shipyard's goals, then interviews you
  about why each pattern exists. Nothing leaves your machine until you approve it.
argument-hint: "[discover [--since YYYY-MM-DD] [--reuse latest|<dir>] | collate <folder>]"
disable-model-invocation: true
---

Discovery mode: evidence of how one person works, linked to what their PRs did, scored against the goals in `${CLAUDE_PLUGIN_ROOT}/docs/vision.md`. Analysis comes first; the interview then asks what stands in the way of each goal. Nothing here reads the tracker, and nothing leaves this machine until the person approves it.

$ARGUMENTS

Two modes exist: `discover` (steps 1 to 7) and `collate` (§ Collate); any other mode stops with that sentence. The window defaults to the last 30 days. A run resumes from its output directory: continue at the first step whose output is missing, and never redo a finished one. `--reuse <dir>`, or `--reuse latest` for the newest dated folder under the output root, reruns an approved run from its own folder: keep its session table, digests, facets and analysis, ask only the step 5 questions whose fields are still `unknown` (an empty `shipyard_usage` at level `none` counts as unknown), then do steps 6 and 7 again, the name and zip included.

## 1. Collect, in code

Resolve the output directory as `scratch_dir {"identifier": "retro-discovery"}` plus a UTC date folder. When Shipyard's tools refuse because this session did not start in a git checkout, use `$HOME/.claude/shipyard/scratch/retro-discovery`, the folder that tool resolves to, and in steps 2 and 3 dispatch with the agent's own default model and effort. Build the session table, in the background, and wait for its completion notice. It reads every Claude Code transcript folder it finds (`CLAUDE_CONFIG_DIR`, and every `.claude*` folder in the home folder or one level below), and prints those folders, the candidate repositories, and how many sessions touched no repository:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro discover --since <date> --out <dir>
```

Name the transcript folders it read and ask, in the same turn as the repositories, whether any folder is missing; add a missing one with `--projects <dir>...`, which replaces the folders it found. Ask which repositories to include with one multi-select `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`), the busiest first. Sessions tied to no repository are always kept; a person who picks no repository passes `--repo` with no names. Then confirm each chosen repository's standards resolve from this session: they may need that repository's own MCP servers. For any that cannot, ask whether to drop it or to finish its step 2 in a session started in that repository. Then run the same command with `--repo <owner/name>...` added. It reuses the session table and writes `prs.jsonl`, `links.json`, one digest per session under `digests/`, facet batches under `facets/`, comment batches under `comments/`, and `discovery.json` with every measured field filled, costs included. Never read `sessions.jsonl`, `prs.jsonl` or a digest yourself.

## 2. Tag review comments

Skip a repository with no comment batch. For each other repository, resolve its standards rules with `sy:repo-standards` in resolve mode, naming that repository's checkout as the scope: resolving from this session's configuration reads this repository's standards, not the target's. Write the rules yourself from the return to `comments/rules-<owner>__<name>.json` as a list of `{id, text}`; a subagent may not be allowed to write there. A rule with no ID of its own takes `<file>#<heading>`. When an earlier run folder beside this one has a rules file for the same repository, or a tags file whose batch is byte-identical to this run's, copy it instead of dispatching again. Dispatch `sy:comment-tag` once per batch file (`comments/batch-<owner>__<name>-<n>.json`), with that repository's rules file, writing `comments/tags-<same suffix>.json`. Then fold the tags in:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro fold-tags --out <dir>
```

## 3. Classify every session, then analyse

Dispatch `sy:session-read` once per `facets/batch-<n>.json`, writing `facets/facets-<n>.json`, in the background. Read only the returns, and say nothing between dispatches beyond one progress line per handful of returns. Then fold the facets in:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro analyze --out <dir>
```

It writes `analysis.json` (one section of metrics per goal, each with the sessions or PRs it rests on, plus a `weekly` trend of outcomes, cost and friction per session), `facets.jsonl`, and the same analysis into `discovery.json`.

This is the run's checkpoint. If Shipyard's context line has appeared (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/context-recycle.md`), suggest `/clear` and `/sy:retro discover` before step 4: the run resumes from its output directory.

In steps 2 and 3, keep at most the resolved `limits.max_depth_agents` dispatches in flight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`). Every dispatch resolves its model and effort with `agent_model` and passes them as the `Agent` invocation's `model` and `effort`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`.

## 4. Write the analysis

Read `analysis.json` and `${CLAUDE_PLUGIN_ROOT}/docs/vision.md`, then write `analysis.md`, at most 750 words. Open with the vision in two sentences, in plain words: each engineer leads a team of agents, scoping the work, delegating it and judging the result, and every path ends in the same outputs. Give each goal, P1 to P6, T1 and T3, a short section: the goal in one plain sentence, how far the numbers are from it, then at most 3 patterns, each a plain statement with its pointers (a PR, `<session>:L<line>`, or a count). A pattern is something that stands between the person's work and the goal, or carries it toward the goal, and that the numbers alone cannot explain. Under P1, name unplanned PRs from `unplanned_prs`, never from another list. Where a goal's numbers move across `weekly`, say which way. Under T3, lead with the cost shares: cache reads, cache writes by TTL, rewrites after idle gaps, subagents, and the costliest skills and agents. Say once that the dollar figures are list-price estimates, so the shares matter more than the totals.

Record each pattern in `discovery.json` `patterns` as `{id, goal, pattern, evidence, why: "unknown"}`. Write facts and patterns only, never a judgement of the person.

## 5. Interview: what stands in the way of each goal

Ask one `AskUserQuestion` per turn, at most 16 in all, keeping 4 for the person's own follow-ups:

1. First, before showing anything of the analysis, so it does not anchor the answer: state the vision in two plain sentences, then ask how close the person feels agentic coding is to it for their own work. Options: `there`, `close`, `halfway`, `far`, `not_yet_useful`. Invite them to say why, in a note or in Other. Record `vision_closeness` as `{rating, why}`, the why in their words. If they give no reason, ask once more, offering the likely reasons as options; record `unknown` only if they decline again. Each obstacle the why names also becomes a `pain_points` entry with `source: interview`.
2. Then show `analysis.md` and ask whether it misreads anything. Correct `analysis.md` and the patterns before going on.
3. Then one question per pattern, highest-cost or most-repeated first: what stands in the way of the goal it touches, or what makes it work. Name the goal in plain words, the pattern and its evidence. Ask about the work, never the person: the obstacle may sit in the tools, the repository, the platform, the team's habits or the time available. Offer as options the likely obstacles the evidence suggests, including ones outside the person's control, never as the only answers. Record the answer, in the person's words, as that pattern's `why`.
4. If questions remain: where the person wants to decide rather than delegate (`control`), and which Shipyard skills they skipped, and why (`shipyard_usage`).

Word every question from what the person did, in their own tools' terms. `sessions.shipyard.level` in `discovery.json` decides how much Shipyard enters it: at `regular`, ask as listed; at `light`, ask about only the skills they ran, as a first impression; at `none`, name no Shipyard skill, path or term in any question except one: ask what kept them off Shipyard, describing it in one plain sentence (skills that plan work, write a spec and ship it as a PR). Offer likely reasons: did not know of it, my own skills already do this, it does not fit my work, it failed to set up. Record the answer as `shipyard_usage.skipped` `[{skill: "shipyard", why}]`.

When the person asks a question of the evidence, such as whether something is improving, answer it from `analysis.json`, `facets.jsonl` and `sessions.jsonl` in code, never from memory, and add the answer to the pattern it belongs to.

A field the person does not answer stays `unknown`. Never fill one from a guess. While a question is open, ask nothing else and never repeat it.

## 6. Write

Add each answer under its pattern in `analysis.md`, as a line starting `Why:`. In `discovery.json` (schema: `references/discovery-schema.json`; `discover.extra_fields` adds a repository's own fields), fill `patterns[].why`, `vision_closeness`, `control`, `shipyard_usage`, and `pain_points`: each pattern the person calls a pain, with its evidence and `source: interview`. Report by approach and by goal; never rank the person.

Then suggest two things to try, each built on what the person already does: a skill of their own, or a pattern, that a Shipyard skill could carry; or, at level `regular`, a change to how they use Shipyard that targets their costliest pattern. Before suggesting a skill, read its `SKILL.md` and `${CLAUDE_PLUGIN_ROOT}/docs/configuration.md` for what it needs and what it writes: a start in a git checkout, tracker writes and the tracker project (a person can set their own in `<repo>/.shipyard/config.local.json`), worktrees, and points where it never asks. Name the one catch that matters for this person, from their `control` and pain points. Add a `## Try next` section to `analysis.md` and record `suggestions` in `discovery.json` as `[{try, why, evidence, catch}]`. Then write the agent companion beside `analysis.md`:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro report --out <dir>
```

`analysis-agents.md` holds the full evidence: every metric, every pattern with its answer, the weekly trend, the costliest sessions, every session short of its goal, every merged PR, and every friction detail. Rerun it after any later edit to `discovery.json`.

## 7. Approve

Show `analysis.md` in full. Say that `analysis-agents.md` holds the full evidence for agents, and that `facets.jsonl` holds one line per session: its classes and a one-sentence summary. Then ask with one `AskUserQuestion`: approve as written; edit or redact entries (name them in Other); or delete the run. Apply any edit to every file it touches, then ask again. On approval set `approval.status` to `approved` with the time. The person's review is the privacy control: the credential scrubber only catches values set in the current environment.

Then ask the person's name with one `AskUserQuestion`: offer `git config user.name` when it is set, and `anonymous`; Other takes any name. Zip the four files to share (`discovery.json`, `analysis.md`, `analysis-agents.md`, `facets.jsonl`), each prefixed with the name:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro package --out <dir> --name "<name>"
```

Close with the zip's path. Sharing it is the person's own step.

## Collate

`collate <folder>` joins the team's approved retros: each `retro-<name>-<date>.zip`, or a folder holding `[<name>-]discovery.json`, anywhere under the folder. Write into `<folder>/collation`:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro collate --src <folder> --out <folder>/collation
```

It writes `collation.json` and `collation.md`: one column per person for the headline, human turns, friction, cost shares by token class, outputs and work types, then every pattern by goal with each person's why, and every pain point, control wish, Shipyard use and suggestion. Never open a person's zip or digest yourself.

Read `collation.md` and `${CLAUDE_PLUGIN_ROOT}/docs/vision.md`, then write `<folder>/collation/themes.md`, at most 900 words:

1. Who: one line per person, with their window, kind of work and Shipyard level.
2. Per goal: each theme that two or more people share, with the count and names, their whys in their own words, and the pointers. T2 is the bar: only a shared theme may become a proposed Shipyard change.
3. Proposed changes: each with the theme it answers, the people behind it, and the goal it moves.
4. Watch: themes from one person only; no change yet.
5. Retro fixes: what the runs show about the retro itself, such as fields left `unknown`, a large `other` class, or a person whose work the measures miss.

Facts and the people's words only; never rank people. Rerun after each new retro arrives: the newest run per person wins.

