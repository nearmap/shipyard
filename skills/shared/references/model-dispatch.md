# Model and effort at dispatch: resolve from config, pass as overrides, reconcile against observed

Every subagent's model and effort are configurable per repo, and this is the one place that says how. The rule used to be re-pasted per dispatch site, which is precisely why most agents never got one: a pattern that must be copied is a pattern that will be missed. Copy the rule from here rather than restating it.

## The rule

Resolve the model for the agent you are about to dispatch, by its own name, through the `sy` MCP server's `agent_model` tool (tool names resolve per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`):

```
agent_model {"name": "<agent-name>"}
```

It returns the binding report for that agent: the model and effort to dispatch with, what config requested, and whether either was clamped up to a floor. A caller whose own source of truth names a different value — a ship profile's phase model or effort — passes it as the call's `model` (a model or tier name) or `effort` argument rather than clamping it in prose; the report then shows the override as requested and the floor-clamped value to dispatch with. Pass the reported **model** and **effort** as the `Agent` invocation's actual `model` and `effort` parameters, never as prompt text — an agent told in prose which model or effort to be is still running whatever the frontmatter said. Record them as the dispatch's requested model and effort.

Three properties of the mechanism make this non-optional rather than a nicety:

- **A nested `Agent` call does not inherit a model override.** An agent dispatched from inside another agent falls back to its frontmatter unless the model is passed again on that inner call. Resolve and pass on every dispatch, at every depth.
- **Frontmatter is the load-time fallback, not the operative value.** Its `model:` and `effort:` are what run when a dispatch passes neither, so they must never sit below the agent's declared floor — `scripts/validate.py` checks that — but the resolved values are what should actually run.
- **The resolver has already clamped the value to the agent's declared floor.** `config/floors.json` holds a `min_model`/`min_effort` per agent that no config layer can lower, so a config trying to drop `sy:gate` below the frontier tier is refused by name before it reaches you. You do not need to re-check the floor at the dispatch site — but you must not substitute a weaker model of your own either.

## Nesting is capped

Claude Code allows three levels of subagent nesting, and the cap is silent. An agent at the third level is not given the `Agent` tool at all — its frontmatter `tools:` list is not honoured, no error is raised, and nothing in its return says a dispatch was refused rather than skipped. Measured across 1,776 subagent runs of this plugin: 654 at depth 1, 685 at depth 2, 437 at depth 3, and none anywhere at depth 4.

So an agent may be instructed to dispatch only when every path that reaches it lands at depth 1 or 2. Count the path, not the intent: `/sy:ship` → `sy:ship-gate` (1) → `sy:gate` (2) → `sy:repo-review` (3) puts the reviewer at the cap, which is why it dispatches nothing and `sy:gate` refutes its candidates instead. A `fork` spends a level like any other dispatch, so dispatching `sy:gate` from a forked worker puts gate itself at the cap and costs it every `sy:hunt` it was going to run, with no sign in its verdict that depth was the reason.

An agent that hits this reports it as the structural fact it is, never as a transient. "Nested dispatch is unreliable under load" reads as bad luck and invites a retry that cannot work; a finding whose confidence is lower because nothing could ever check it must say so in those terms, so the caller can check it one level up.

## Waiting on a dispatch

A dispatched agent or a `run_in_background` command announces its own completion. Wait for that notification, ending the turn when nothing else is left to do; never `sleep`, `until`-loop, or re-list a directory or `tasks/*.output` to detect that it finished. A hand-rolled wait watches whatever path was guessed, and a wrong guess idles to its own timeout after the work is already done.

## Requested is not observed

`CLAUDE_CODE_SUBAGENT_MODEL` outranks the per-invocation model parameter on Claude Code before 2.1.251, or when `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`, and an org model allowlist can silently drop an excluded model back to the inherited one. On effort, `CLAUDE_CODE_EFFORT_LEVEL` outranks the per-invocation parameter and the `maxEffortLevel` setting caps every effort. Each means a dispatch can run on a model or effort nobody asked for, with no error. So requested and observed are separate facts:

- record the resolved value as `<phase>_model_requested` when you dispatch;
- read the transcript-observed model back from the `by_agent` entry of the `usage_summarize` tool's roll-up and record it as `<phase>_model_observed`;
- if they disagree, stop and investigate rather than claiming the requested agent ran.

The `validate_config` tool fails when `CLAUDE_CODE_SUBAGENT_MODEL` or `CLAUDE_CODE_EFFORT_LEVEL` is set, so the common causes are caught at the front door rather than discovered in accounting.

## Unavailability falls back once, and says so

If a dispatch returns no usable result because the requested model is unavailable — a spend cap, a rate limit, a `<synthetic>` refusal in place of real work — do not retry the same model and do not read the empty return as success. Re-dispatch once at the value `get_config {"key": "models.tiers.frontier_fallback"}` reports, set the observed model to what actually ran, and note the substitution. If the fallback also cannot run, return `blocked` naming the model, never a silent pass.

## Effort follows the same rule

The `Agent` tool's `effort` parameter, added in Claude Code 2.1.292, sets one dispatch's effort the way `model` sets its model: precedence is `CLAUDE_CODE_EFFORT_LEVEL`, then the per-dispatch parameter, then frontmatter `effort:`, then the session's. So:

- nested calls do not inherit an effort override (verified on Claude Code 2.1.303: a depth-2 dispatch passed no effort under a medium-effort parent ran at its own frontmatter's), so pass the resolved effort on every dispatch at every depth;
- frontmatter `effort:` is the load-time fallback and must not sit below the agent's floor;
- observed effort is the `efforts` list on the agent's `by_agent` entry of the `usage_summarize` roll-up; an observed effort that disagrees with the requested one is investigated like a model mismatch.

One live consequence worth knowing: effort is silently dropped for a model Claude Code does not treat as effort-capable — a subagent pinned to such a model records no effort and inherits the session's. The resolver refuses that combination by name, which is why `min_model` never drops to a non-effort-capable tier.
