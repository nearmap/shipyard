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
default as prose** (e.g. "default 3"). The default lives in exactly one place, `config/defaults.json`
— not repeated, echoed, or tabulated anywhere else, including `docs/configuration.md`, which
documents what each key does and whether it's required but deliberately carries no "current value"
column of its own. A number copied into prose goes stale the instant the shipped default changes in
`config/defaults.json`, and a caller who reads "at most 3" without resolving has no way to tell a
repo's real override from a documentation lie that happens to still parse — this is the same
duplication-drift this reference exists to close off, not a smaller version of it.

Resolve once per phase/run and use the resolved value for every decision that bullet governs in
that pass; re-resolve on a fresh dispatch the same way a model override is re-resolved per
`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md` — nothing here is cached across
invocations.

The same discipline applies to a key's **rationale**, not only its default: state *why* a default
is what it is once, in `docs/configuration.md`, and cite it — do not re-paste the same explanatory
sentence at every call site. Re-pasted prose drifts in wording the same way a re-pasted number
drifts in value, and it was found duplicated word-for-word across four files during this reference's
own introduction.

**Narrower obligation for human-facing overview prose.** `README.md`, `docs/usage.md`,
`agent-guide.md`, and a skill's YAML frontmatter `description:` (parsed at plugin load, same as
`effort:`, and never re-read live mid-run) are read by a person, not executed by a session, so
"resolve it for real" above doesn't apply to them — there is no run to resolve it in. The
"never restate a default" rule still does, though: name the config key so a reader knows the
behavior is configurable, without asserting a specific number. `config/defaults.json` is the one
file a value actually lives in; `python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" show` is how a
human looks up what it currently resolves to in a given repo. Every mention anywhere else just
points there — never to a restated number.
