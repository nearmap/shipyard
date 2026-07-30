---
name: config
description: >-
  Inspect, validate, change, and reload this repo's Shipyard configuration. Shows every resolved
  value with the layer it came from, explains why a value is what it is, writes a setting at the
  right scope, and re-resolves mid-session so an edit takes effect on the next dispatch without a
  restart. Never prints or accepts a secret.
argument-hint: "[show | validate | reload | set <key> <value> [--scope user|repo|local] | agent <name>]"
disable-model-invocation: true
---

Everything non-secret about how Shipyard behaves in this repo lives in a layered config file that `scripts/sy_config.py` is the only reader of. This skill is the human front end to that resolver. It never reads a credential: secrets stay in the environment, and the resolver refuses a credential-shaped key in either direction.

$ARGUMENTS

With no argument, do `show`.

## show

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" show
```

Report it as a status update. Lead with the three layers and which exist, because "my change did nothing" is nearly always a layer question — a value set in the committed layer while the gitignored local layer overrides it, or a user-global default that no repo layer touches. Then give the resolved values with their provenance, and call out anything resolving from `derived-default` (computed from the repo, not written anywhere) so the user knows it is not in a file they can grep.

For one value, `get <key>`. For a machine-readable dump including the fingerprint, `show --json`.

## validate

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" validate
```

Exit 0 prints the fingerprint. Exit 1 lists every problem, each naming its key and the layer it came from. Relay the errors verbatim — they are written to be actionable — and do not paraphrase a floor refusal into "the model is too weak": the message already names the agent, the floor, and why that floor exists.

Four failure classes are worth recognising on sight, because each has a different fix:

- **A required key is unset.** Set it in the committed layer, or run `/sy:init-repo`.
- **A retired `SY_*` variable is still in the environment.** This is an error, not an override, deliberately: two resolution paths for one key is what made the old secret/config boundary illegible. Move the value into config and unset the variable. Note that a variable which merely *agrees* with config is still an error.
- **A floor refusal.** A config layer tried to drop an agent below `config/floors.json`. Cost-scaling may raise a floor, never lower it; the fix is to accept the floor, not to edit the floors file, which is plugin-shipped precisely so it is not locally negotiable.
- **`CLAUDE_CODE_SUBAGENT_MODEL` is set.** It outranks the per-invocation model parameter, so it silently reroutes every agent off whatever the resolver decided. Unset it.

## reload

A config edit needs no restart, because nothing caches a resolved value on disk: every consumer reads through the resolver at call time. What goes stale is the resolved block sitting in this session's context, and any preflight cache keyed on the old values.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" validate
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" show
```

Then state the scope boundary plainly, because it is the part users get wrong: **workers already dispatched keep what they were given.** The boundary is the next dispatch. A reload mid-`/sy:ship` does not retroactively change the model a running BUILD is using, and it must not silently change the reviewer between fix rounds — see below.

Report the new fingerprint and name which keys changed provenance or value since the previous `show` in this session, rather than reprinting everything unchanged. The preflight cache invalidates itself: its fingerprint folds in the resolved config, so a changed setting produces a cache miss and the next tracker call re-verifies liveness for real.

## A config change mid-ship is refused, not absorbed

`/sy:ship` stamps the resolved-config fingerprint into its recorded state, the same discipline as its pinned base/head SHAs. If the fingerprint at a fix round does not match the one recorded at START, the run **stops and reports** rather than continuing on quietly changed settings: a reviewer whose model changed between rounds invalidates the comparison the gate exists to make. Re-running `/sy:ship` after an intentional change is the supported path; silently absorbing it is not.

## set

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get <key>     # confirm the current value and layer first
```

There is no `set` subcommand on the resolver, deliberately — writing config is a small, legible JSON edit, and a generated writer would only obscure which layer changed. Edit the file directly with `Edit`, choosing the layer by what the value *is*, not by convenience:

| Scope | File | For |
|---|---|---|
| `user` | `~/.shipyard/config.json` | your preferences across every repo (model tiers, memory location) |
| `repo` | `<repo>/.shipyard/config.json` | committed, shared by everyone who clones (tracker, board, column names) |
| `local` | `<repo>/.shipyard/config.local.json` | gitignored: per-person or per-machine (an account email, an absolute worktree path) |

Include `"$schema"` in a newly created file so editors validate it. After any edit, run `validate`, then `show` the affected keys so the user sees the change actually landed in the layer they meant. If the user asks to set something credential-shaped, refuse and say where it belongs: the environment, never a config file — a config file is greppable, and any skill that `cat`s it burns the value into permanent transcript history.

## agent

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" agent <name> --json
```

Answers "what will `sy:hunt` actually run at, and why". The binding reports the resolved model, the effort policy, what config requested, and whether either was clamped up to a floor. Use it to explain a surprise: a `model_clamped: true` means config asked for something below the floor and the floor won.

State plainly, whenever effort comes up, that **`effort` in config is policy the resolver validates and clamps, not a value applied at dispatch.** There is no effort parameter on the `Agent` tool; a subagent's effort comes only from its frontmatter, which is plugin-shipped and cannot vary per repo. Never imply a config edit changes a running agent's effort. The full explanation is `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`.

## Interaction

One mode per turn — status update, `AskUserQuestion`, or an isolated `## Action needed` block — per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/user-interaction.md`. A validation failure the user must fix outside this session (unset a variable, export a credential) is an `## Action needed` block naming the exact command, not prose mixed into a status report. This skill never calls the tracker, so it runs no tracker preflight.
