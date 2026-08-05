# Preflight: verify the tracker is usable before any other work

`/sy:plan`, `/sy:spec`, `/sy:ship`, and `/sy:spike` all read or write the tracker, so each runs this check as its very first action — before a research turn, an interview question, or a tracker read. `/sy:pr`, `/sy:explain`, and `/sy:help` never call the tracker skill and are out of scope here, the same exclusion `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md` already makes for `/sy:ci` and `/sy:standards`. `/sy:init-repo` is the setup wizard this check exists to make unnecessary once run; its last step is this same check, live, to confirm the config it just wrote actually works.

A repo's committed `.shipyard/config.json` carries the shared, non-secret config for every collaborator who clones it — which tracker, the board/project identifiers, the five column names. It can never carry two things that are genuinely per-person: a one-time login the adapter needs outside Shipyard's own config, and a personal credential, which stays in the environment and never enters a config file at all. A run that discovers either gap for the first time deep inside a write — an attachment upload at ship handoff, say — has already spent the whole run's context on work that cannot land. This check exists to fail at the front door instead.

## Check order

Delegate the mechanics to the tracker skill (`${CLAUDE_PLUGIN_ROOT}/skills/tracker/SKILL.md`), which already owns adapter selection and fails fast in this order:

1. `tracker` resolves to a known adapter.
2. The five required column names are set — free, no network.
3. The selected adapter's own required configuration is present — each adapter declares and self-checks its list, in its `config-map.json`'s `required` (non-secret config keys) and `secret_env` (environment-only credential names) — both checked, the config keys against resolved config and the secrets against `os.environ` directly.
4. A **liveness** check — presence is not enough. A credential can be set and still be dead: revoked, expired, or never actually logged in to begin with. The adapter performs a real, minimal read against the tracker to tell the two apart, exactly the "validate with a real work-item read" guidance each `ADAPTER.md` already gives for its own operations, just run once up front instead of discovered mid-write.

Steps 1–3 are one call — the `validate_config` tool — which names every offending key or missing secret and the layer (or "the environment") it should have come from, so a misconfiguration is one read rather than three separate discoveries.

## The liveness check is cached, not repeated

A live read on every invocation is neither quick nor free, and what it verifies changes rarely, so the `preflight` tool caches its own result rather than re-reading: the cache is keyed on a fingerprint of the plugin build, the selected tracker, the resolved config, and any secret the adapter names, with a short TTL. A hit skips the network call entirely and is reported as cached; a miss — first run, changed config, expired TTL — runs the adapter's live check once and records success itself, so the next call gets the hit.

The caller's whole obligation is therefore **one `preflight` call**. There is no separate check-then-record pair to sequence, forget half of, or get the order of wrong — the tool decides whether the live read is needed and records it when it runs. Only one case needs more than that: a caller that has just changed the config knows a still-fresh entry predates the change and is stale by construction, and passes `force` to demand the live read regardless of the cache.

The fingerprint, cache and TTL mechanics are tracker-agnostic and live inside the tool (`sy_tools/`); what "a real read" means for a given tracker is adapter knowledge and stays in that adapter's own `ADAPTER.md`, never here.

## On failure: name it once, then stop

A failed check — presence or liveness — never surfaces as a raw tool error partway through a run. It closes the turn with exactly one `## Action needed` block (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`), stating:

- exactly which variable, file, or command is missing — the adapter's own error text, verbatim, since it already names the specific gap;
- the one-line fix — set the key in `.shipyard/config.json`, export the adapter's credential, run its one-time login command, or run `/sy:init-repo`;
- a link to `docs/configuration.md`, the complete configuration reference.

No other status prose shares that turn, and nothing downstream runs against a tracker that failed this check: a ship session stops before its parent dispatches any worker; `/sy:plan`, `/sy:spec`, and `/sy:spike` stop before their first tracker read.
