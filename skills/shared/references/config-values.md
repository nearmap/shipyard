# Resolving a scalar config value named in skill prose

A bullet that names a dotted config key (`limits.max_depth_agents`, `ship.merge_strategy`,
`spec.light_tier_max_files`, and similar) is documentation, not execution: text in a skill file is
read, never run. Naming the key in backticks does not resolve it. Before treating the value as
authoritative for a decision in this run, resolve it for real:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get <dotted.key>
```

This applies specifically to a value with **no other enforcement point** — a concurrency cap, a
threshold, an escalation count — where the orchestrating session reading this prose is the only
thing that ever applies the number. (Contrast `ci.poll_timeout`: `scripts/ci_poll.sh` resolves that
one internally on every call, so a skill mentioning "default 1800s" there is safe descriptive
color, not the operative value.) For a value in this file's scope, **never restate the shipped
default as prose** (e.g. "default 3"). The default lives in exactly one place, `config/defaults.json`,
documented once in `docs/configuration.md`. A number copied into skill prose goes stale the instant
the shipped default changes there, and a caller who reads "at most 3" without resolving has no way
to tell a repo's real override from a documentation lie that happens to still parse — this is the
same duplication-drift this reference exists to close off, not a smaller version of it.

Resolve once per phase/run and use the resolved value for every decision that bullet governs in
that pass; re-resolve on a fresh dispatch the same way a model override is re-resolved per
`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md` — nothing here is cached across
invocations.
