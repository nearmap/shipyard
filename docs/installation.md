# Installing Shipyard

Shipyard is a Claude Code plugin. You **load** it, you do not symlink or copy it into `~/.claude`. Nothing about your global config changes; the plugin's skills, agents, and hooks become available under the `/sy:` namespace for as long as it is loaded.

Once loaded, configure the repo you want to use it in — see [`configuration.md`](configuration.md) (and [`github-setup.md`](github-setup.md) if you use the GitHub tracker). Then drive the loop as in [`usage.md`](usage.md).

## Load the plugin

Two ways, depending on whether you want it for one session or permanently.

### One session (local development / trying it out)

```bash
claude --plugin-dir /path/to/shipyard
```

The plugin is available only for that invocation — nothing is registered.

### Persistent (across sessions)

Persistent installs come from a **marketplace**: a directory or repo that contains a `.claude-plugin/marketplace.json` listing one or more plugins. This repo ships one (it lists the `sy` plugin at its root), so it is its own marketplace. Add the marketplace, then install the plugin from it — you can do this straight from GitHub without cloning, or from a local checkout.

**From GitHub (no clone).** `claude plugin marketplace add` accepts a GitHub `owner/repo` shorthand (or a full `https://`/`git@` URL); Claude Code clones it for you. Append `@<ref>` to pin a branch or tag, and private repos authenticate with your existing git credentials (`gh auth login`, SSH agent, or a credential helper). `marketplace add` itself always registers globally (`~/.claude/plugins/known_marketplaces.json`) — it just makes the catalog visible; scope is chosen at `install`, below.

```bash
claude plugin marketplace add bretttully/shipyard   # registers the marketplace, named "shipyard"
claude plugin install sy@shipyard --scope project   # install the plugin, scoped to this repo
```

**From a local checkout.** Point the same command at the directory instead:

```bash
claude plugin marketplace add /path/to/shipyard
claude plugin install sy@shipyard --scope project
```

`claude plugin install` takes `<plugin>@<marketplace>`; here the plugin is `sy` and the marketplace is `shipyard` (the `name` in `marketplace.json`). Equivalently, run `/plugin`, go to **Discover**, and choose a scope in the interactive UI.

Manage it later with `claude plugin list`. `update` and `uninstall` both default to `--scope user`, which is almost never right here since the install above used `--scope project` — pass the same scope back, and the full `plugin@marketplace` id, or they resolve against the wrong install (or nothing) and fail with `Plugin "sy" not found`:

```bash
claude plugin update sy@shipyard --scope project
claude plugin uninstall sy@shipyard --scope project
```

`update` only acts when `.claude-plugin/plugin.json`'s `version` has actually moved — it fetches the marketplace's git remote regardless, but a same-version fetch is a no-op even if new commits landed. If `update` reports "already at the latest version" right after a change you know shipped, the maintainer forgot to bump the version (see [`CONTRIBUTING.md`](../CONTRIBUTING.md)); there's nothing to do on the consumer side but wait for the bump.

Only `--plugin-dir` (the one-session dev mode above) needs a local checkout. Either way, the commands are namespaced by the plugin name (`sy`): `/sy:plan`, `/sy:spec`, `/sy:ship`, `/sy:spike`, `/sy:pr`, `/sy:ci`, `/sy:explain`, `/sy:help`, `/sy:init-repo`.

### Choose an install scope

`claude plugin install` takes an install scope. It defaults to `user` — read that default carefully, since it is *not* repo-scoped:

| Scope | Flag | Settings file | Who it declares intent for |
|---|---|---|---|
| `project` (recommended here) | `--scope project` | this repo's `.claude/settings.json` | every collaborator who clones the repo — checked into version control, alongside the committed `.shipyard/config.json` described in [`configuration.md`](configuration.md) |
| `local` | `--scope local` | this repo's `.claude/settings.local.json` | just you, in this repo only — gitignored |
| `user` | *(none — the default)* | `~/.claude/settings.json` | you, in **every** project on this machine, not just this one |

`project` is the natural fit for Shipyard: it is already configured per repository (see [`configuration.md`](configuration.md)), so committing the plugin declaration alongside `.shipyard/config.json` makes the repo itself the source of truth for "this repo runs Shipyard, configured like this."

**`--scope project` alone does not make a fresh clone work.** It writes `enabledPlugins: { "sy@shipyard": true }` — an intent — but a collaborator whose machine has never run `marketplace add` has no `shipyard` marketplace to resolve that name against. Close that gap by also adding `extraKnownMarketplaces` to the same `.claude/settings.json`, pointing at this repo, so the marketplace itself travels with the clone:

```json
{
  "extraKnownMarketplaces": {
    "shipyard": { "source": { "source": "github", "repo": "bretttully/shipyard" } }
  },
  "enabledPlugins": { "sy@shipyard": true }
}
```

Even then, a collaborator who trusts the repo folder gets the marketplace registered automatically but still confirms the plugin install themselves — Claude Code shows the exact `claude plugin install` command and will not run it silently, since a plugin can execute arbitrary code. There is no zero-touch path, only a guided one. See [Configure team marketplaces](https://code.claude.com/docs/en/discover-plugins#configure-team-marketplaces).

## `./install.sh` — validate and get load instructions

`./install.sh` does not copy anything. It runs `scripts/validate.py` (structure, frontmatter, the tracker seam, contract completeness, and the script self-tests), does a tracker-aware preflight against your environment, and prints the exact load instructions above for your checkout path. Run it after cloning and any time you want to confirm the plugin is intact:

```bash
cd /path/to/shipyard
./install.sh
```

The preflight runs `sy_config.py validate` and stops on any configuration error, then prints the resolved configuration so you can see each value and the layer it came from. It fails hard when `CLAUDE_CODE_SUBAGENT_MODEL` is set — it outranks resolved per-agent models and would reroute the reviewer — and warns non-fatally when a tool the configured tracker needs is missing, except with `"tracker": "github"`, where a missing `gh` is a hard error.

## Required tools

| Tool | Version | Used for | When you need it |
|---|---|---|---|
| Claude Code | ≥ 2.1.218 | Loading the plugin; must support plugins, `hooks`, and `effort`/`model` in agent frontmatter | Always (2.1.218 is the changelog entry where agent markdown files reject `:` in `name`, reserving it for the plugin namespacing the `sy:` agents use) |
| Python | 3.10+, on `PATH`, invoked as `python` | Validation, usage accounting, the review guard, and the tracker helper scripts | Always |
| `gh` | ≥ 2.94.0, authenticated | The code host (PRs and CI, via `/sy:pr` and `/sy:ci`) for **every** tracker, and the transport for the GitHub tracker adapter | Always (the 2.94.0 floor is required by the GitHub tracker's sub-issue and dependency flags) |
| `acli` | Atlassian CLI | The Jira tracker adapter's transport | Jira tracker only (`"tracker": "jira"`) |
| `gitleaks` | any recent | Secret-scanning a rendered session transcript before it is attached (Jira) or gisted (GitHub) | Whenever ship attaches transcripts; without it, ship stops before publishing an unscanned transcript |

For the GitHub tracker, `gh` also needs `project` + `read:project` scopes (`gh auth refresh -s project,read:project`). See [`github-setup.md`](github-setup.md) for the one-time board setup.

## Next steps

- Run `/sy:init-repo` for an interactive walkthrough of the configuration below — it asks only for what your `.claude/settings.json` doesn't already have, so a teammate joining an already-configured repo answers a short exchange, not a full interview.
- [`configuration.md`](configuration.md) — configure a repo by hand (tracker choice, column names, per-agent models) — what `/sy:init-repo` writes for you.
- [`github-setup.md`](github-setup.md) — one-time GitHub Projects board and field setup (GitHub tracker only).
- [`usage.md`](usage.md) — the day-to-day `plan → spec → ship` loop.
