# Transcript render and attach

The same mechanism backs every caller that attaches a session transcript (`/sy:plan`, `/sy:spec`, `/sy:ship`) — restate only the caller-specific parts (which surface it attaches to, `$KIND`, and any timing note), not this rule.

Resolve `transcript.attach` — `get_config {"key": "transcript.attach"}` (see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md` and `docs/configuration.md`). When it resolves false, skip the step entirely.

When true: delegate a subagent (added to `agents_used`) to render this session's transcript straight from the on-disk session tree and attach it, following the `tracker` skill's attachment flow with the caller's `$KIND`. This is how the reasoning trail lands on the tracker with no manual `/export`, and the rendered text stays out of the caller's context.

Subagent delegation is primary. When the delegation itself is denied under auto-mode, the identical render-and-attach may run inline as an explicit permitted fallback — the authorized-alternate-route case of the denied-write boundary in `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/write-integrity.md`, not a reroute to force through a write the operator blocked. Either way the rendered transcript is handled by path only and never read back into the caller's context.

The inline fallback path is deterministic-scan-only: it drops the primary path's contextual review, since reading the transcript to review it would defeat the isolation invariant the delegation exists to preserve. Treat a clean scan there as evidence, not proof, per the `tracker` skill's attachment flow.

If neither path completes, surface it loudly — never silently skip the attachment.
