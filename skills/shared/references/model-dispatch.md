# Model at dispatch: resolve from config, pass as an override, reconcile against observed

Every subagent's model is configurable per repo, and this is the one place that says how. The rule used to be re-pasted per dispatch site, which is precisely why most agents never got one: a pattern that must be copied is a pattern that will be missed. Copy the rule from here rather than restating it.

## The rule

Resolve the model for the agent you are about to dispatch, by its own name:

```bash
MODEL=$(python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" agent <agent-name>)
```

Pass `MODEL` as the `Agent` invocation's **actual model override parameter**, never as prompt text — an agent told in prose which model to be is still running whatever the frontmatter said. Record it as the dispatch's requested model.

Three properties of the mechanism make this non-optional rather than a nicety:

- **A nested `Agent` call does not inherit a model override.** An agent dispatched from inside another agent falls back to its frontmatter unless the model is passed again on that inner call. Resolve and pass on every dispatch, at every depth.
- **Frontmatter is the load-time fallback, not the operative value.** It is what runs when nobody passes an override, so it must never sit below the agent's declared floor — `scripts/validate.py` checks that — but the resolved value is what should actually run.
- **The resolver has already clamped the value to the agent's declared floor.** `config/floors.json` holds a `min_model`/`min_effort` per agent that no config layer can lower, so a config trying to drop `sy:gate` below the frontier tier is refused by name before it reaches you. You do not need to re-check the floor at the dispatch site — but you must not substitute a weaker model of your own either.

## Requested is not observed

`CLAUDE_CODE_SUBAGENT_MODEL` outranks the per-invocation parameter, and an org model allowlist can silently drop an excluded model back to the inherited one. Both mean a dispatch can run on a model nobody asked for, with no error. So requested and observed are separate facts:

- record the resolved value as `<phase>_model_requested` when you dispatch;
- read the transcript-observed model back from the usage JSON's `by_agent` entry (`${CLAUDE_PLUGIN_ROOT}/scripts/session_usage.py`) and record it as `<phase>_model_observed`;
- if they disagree, stop and investigate rather than claiming the requested agent ran.

`python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" validate` fails when `CLAUDE_CODE_SUBAGENT_MODEL` is set, so the common cause is caught at the front door rather than discovered in accounting.

## Unavailability falls back once, and says so

If a dispatch returns no usable result because the requested model is unavailable — a spend cap, a rate limit, a `<synthetic>` refusal in place of real work — do not retry the same model and do not read the empty return as success. Re-dispatch once at `python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get models.tiers.frontier_fallback`, set the observed model to what actually ran, and note the substitution. If the fallback also cannot run, return `blocked` naming the model, never a silent pass.

## Effort is not symmetrical with model, and must not be described as if it were

There is no effort parameter on the `Agent` tool. Effort exists only as agent frontmatter, which Claude Code parses when the plugin loads, before any substitution pass runs — verified empirically, not assumed. So:

- **frontmatter `effort:` is the only surface that sets a subagent's effort**, and it is per-plugin-build, not per-repo;
- `models.agents.<name>.effort` in config is **policy the resolver validates and clamps**, not a value anything applies at dispatch. It documents intent and enforces floors; it does not change what runs;
- never write an instruction that implies effort can be passed at dispatch, and never report a per-dispatch effort as though it were requested.

One live consequence worth knowing: effort is silently dropped for a model Claude Code does not treat as effort-capable — a subagent pinned to such a model records no effort and inherits the session's. The resolver refuses that combination by name, which is why `min_model` never drops to a non-effort-capable tier.
