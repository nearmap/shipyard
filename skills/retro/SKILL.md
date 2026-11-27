---
name: retro
description: >-
  Learn how you work from your own session history and PR outcomes. Discovery mode builds a
  session table from your local transcripts, costs every session by token class, links sessions to
  PRs, classifies every session, scores the evidence against Shipyard's goals, then interviews you
  about why each pattern exists. Nothing leaves your machine until you approve it.
argument-hint: "[discover] [--since YYYY-MM-DD]"
disable-model-invocation: true
---

Discovery mode: evidence of how one person works, linked to what their PRs did, scored against the goals in `${CLAUDE_PLUGIN_ROOT}/docs/vision.md`. Analysis comes first and the interview asks why its patterns exist. Nothing here reads the tracker, and nothing leaves this machine until the person approves it.

$ARGUMENTS

Only `discover` exists so far; any other mode stops with that sentence. The window defaults to the last 30 days. A run resumes from its output directory: continue at the first step whose output is missing, and never redo a finished one.

## 1. Collect, in code

Resolve the output directory as `scratch_dir {"identifier": "retro-discovery"}` plus a UTC date folder. Build the session table, in the background, and wait for its completion notice; it prints the candidate repositories:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro discover --since <date> --out <dir>
```

Ask which repositories to include with one multi-select `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`), the busiest first. Then confirm each chosen repository's standards resolve from this session: they may need that repository's own MCP servers. For any that cannot, ask whether to drop it or to finish its step 2 in a session started in that repository. Then run the same command with `--repo <owner/name>...` added. It reuses the session table and writes `prs.jsonl`, `links.json`, one digest per session under `digests/`, facet batches under `facets/`, comment batches under `comments/`, and `discovery.json` with every measured field filled, costs included. Never read `sessions.jsonl`, `prs.jsonl` or a digest yourself.

## 2. Tag review comments

For each repository, resolve its standards rules with `sy:repo-standards` in resolve mode and write them to `comments/rules-<owner>__<name>.json` as a list of `{id, text}`. A rule with no ID of its own takes `<file>#<heading>`. Dispatch `sy:comment-tag` once per batch file (`comments/batch-<owner>__<name>-<n>.json`), with that repository's rules file, writing `comments/tags-<same suffix>.json`. Then fold the tags in:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro fold-tags --out <dir>
```

## 3. Classify every session, then analyse

Dispatch `sy:session-read` once per `facets/batch-<n>.json`, writing `facets/facets-<n>.json`. Read only the returns. Then fold the facets in:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro analyze --out <dir>
```

It writes `analysis.json` (one section of metrics per goal, each with the sessions or PRs it rests on), `facets.jsonl`, and the same analysis into `discovery.json`.

In steps 2 and 3, keep at most the resolved `limits.max_depth_agents` dispatches in flight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`). Every dispatch resolves its model and effort with `agent_model` and passes them as the `Agent` invocation's `model` and `effort`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`.

## 4. Write the analysis

Read `analysis.json` and `${CLAUDE_PLUGIN_ROOT}/docs/vision.md`, then write `analysis.md`, at most 700 words. Give each goal, P1 to P6, T1 and T3, a short section: what the numbers show against the goal, then at most 3 patterns, each a plain statement with its pointers (a PR, `<session>:L<line>`, or a count). A pattern is something that helps or hurts the goal and that the numbers alone cannot explain. Under T3, lead with the cost shares: cache reads, cache writes by TTL, rewrites after idle gaps, subagents, and the costliest skills and agents. Say once that the dollar figures are list-price estimates, so the shares matter more than the totals.

Record each pattern in `discovery.json` `patterns` as `{id, goal, pattern, evidence, why: "unknown"}`. Write facts and patterns only, never a judgement of the person.

## 5. Interview: why the patterns exist

Show `analysis.md` in full. Then ask, one `AskUserQuestion` per turn, at most 12 in all:

1. First, whether the analysis misreads anything. Correct `analysis.md` and the patterns before going on.
2. Then one question per pattern, highest-cost or most-repeated first: why it happens. Name the pattern and its evidence in the question. Offer as options the likely reasons the evidence suggests, never as the only answers. Record the answer, in the person's words, as that pattern's `why`.
3. If questions remain: where the person wants to decide rather than delegate (`control`), and which Shipyard skills they skipped, and why (`shipyard_usage`).

Word every question from what the person did, in their own tools' terms. `sessions.shipyard.level` in `discovery.json` decides how much Shipyard enters it: at `regular`, ask as listed; at `light`, ask about only the skills they ran, as a first impression; at `none`, leave `shipyard_usage` empty (not `unknown`) and name no Shipyard skill, path or term in any question.

A field the person does not answer stays `unknown`. Never fill one from a guess. While a question is open, ask nothing else and never repeat it.

## 6. Write

Add each answer under its pattern in `analysis.md`, as a line starting `Why:`. In `discovery.json` (schema: `references/discovery-schema.json`; `discover.extra_fields` adds a repository's own fields), fill `patterns[].why`, `control`, `shipyard_usage`, and `pain_points`: each pattern the person calls a pain, with its evidence and `source: interview`. Report by approach and by goal; never rank the person.

## 7. Approve

Show `analysis.md` in full and say that `facets.jsonl` holds one line per session: its classes and a one-sentence summary. Then ask with one `AskUserQuestion`: approve as written; edit or redact entries (name them in Other); or delete the run. Apply any edit to every file it touches, then ask again. On approval set `approval.status` to `approved` with the time. The person's review is the privacy control: the credential scrubber only catches values set in the current environment. Close with the paths of the three files to share: `discovery.json`, `analysis.md` and `facets.jsonl`. Sharing them is the person's own step.
