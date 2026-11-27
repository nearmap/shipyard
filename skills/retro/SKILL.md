---
name: retro
description: >-
  Learn how you work from your own session history and PR outcomes. Discovery mode builds a
  session table from your local transcripts, links sessions to PRs, measures PR outcomes from git
  and GitHub, reads only the flagged sessions, interviews you, and writes a discovery.json you
  approve before anything leaves your machine.
argument-hint: "[discover] [--since YYYY-MM-DD]"
disable-model-invocation: true
---

Discovery mode: evidence of how one person works, linked to what their PRs did. Nothing here reads the tracker, and nothing leaves this machine until the person approves it.

$ARGUMENTS

Only `discover` exists so far; any other mode stops with that sentence. The window defaults to the last 30 days. A run resumes from its output directory: continue at the first step whose output is missing, and never redo a finished one.

## 1. Collect, in code

Resolve the output directory as `scratch_dir {"identifier": "retro-discovery"}` plus a UTC date folder. Build the session table, in the background, and wait for its completion notice; it prints the candidate repositories:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro discover --since <date> --out <dir>
```

Ask which repositories to include with one multi-select `AskUserQuestion` (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`), the busiest first. Then confirm each chosen repository's standards resolve from this session: they may need that repository's own MCP servers. For any that cannot, ask whether to drop it or to finish its step 2 in a session started in that repository. Then run the same command with `--repo <owner/name>...` added; it reuses the session table and writes `sessions.jsonl`, `prs.jsonl`, `links.json`, `flags.json`, one rendered text file per flagged span under `spans/`, comment batches under `comments/`, and `discovery.json` with every measured field filled and every interview field `unknown`. Read `flags.json` and the measured half of `discovery.json`; never read `sessions.jsonl` or `prs.jsonl` whole.

## 2. Tag review comments

For each repository, resolve its standards rules with `sy:repo-standards` in resolve mode and write them to `comments/rules-<owner>__<name>.json` as a list of `{id, text}`. A rule with no ID of its own takes `<file>#<heading>`. Dispatch `sy:comment-tag` once per batch file (`comments/batch-<owner>__<name>-<n>.json`), with that repository's rules file, writing `comments/tags-<same suffix>.json`; at most the resolved `limits.max_depth_agents` in flight (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`). Then fold the tags in:

```
PYTHONPATH=${CLAUDE_PLUGIN_ROOT} pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml python -P -m sy_tools.retro fold-tags --out <dir>
```

## 3. Read the flagged sessions

`flags.json` ranks sessions and caps them at a fixed budget. Dispatch `sy:session-read` once per flagged session, with its span files, each span's flag reason, and `profiles/<session_id>.md` as the write path, at most the same cap in flight. Read only the returns, never the spans yourself.

Every dispatch resolves its model and effort with `agent_model` and passes them as the `Agent` invocation's `model` and `effort`, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`.

## 4. Interview

Only once every repository's tags are folded in and every profile is written, ask the person, one `AskUserQuestion` per turn, at most 12 questions in all. Each question names the evidence it rests on (a PR, a session pointer, a count) and fills one field of `discovery.json`. Cover, in this order and only where the evidence leaves the field `unknown`:

1. the approach behind each of the busiest PRs, in the person's own words: name its `approach_marker` when it has one, and offer a few plain examples (planned first, prototype first, one small PR at a time), never as the only answers;
2. the stages that approach moves through, and where the person wants control;
3. which Shipyard skills they tried or skipped, and why;
4. their pain points, starting from the ones the session profiles show.

Word every question from what the person did, in their own tools' terms. `sessions.shipyard.level` in `discovery.json` decides how much Shipyard enters it: at `regular`, ask as listed; at `light`, ask question 3 about only the skills they ran, as a first impression; at `none`, skip question 3 (`shipyard_usage` stays empty and is not `unknown`), and name no Shipyard skill, path or term in any question.

A field the person does not answer stays `unknown`. Never fill one from a guess. While a question is open, ask nothing else and never repeat it.

## 5. Write

Write the answers into `discovery.json` (schema: `references/discovery-schema.json`; `discover.extra_fields` adds a repository's own fields) and a `summary.md` of at most 400 words: what was measured, the approaches in the person's words, and the pain points, each with its pointer. Report by approach, never rank the person.

## 6. Approve

Show `summary.md` in full, then ask with one `AskUserQuestion`: approve as written; edit or redact entries (name them in Other); or delete the run. Apply any edit to both files and ask again. On approval set `approval.status` to `approved` with the time. The person's review is the privacy control: the credential scrubber only catches values set in the current environment. Close with the two file paths; sharing them is the person's own step.
