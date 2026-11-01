# Reaching a repository's MCP servers through an agent's `tools:` allowlist

Shipyard agents carry no `tools:` allowlist, neither one with an `mcp__*` wildcard nor one naming a repository's servers.

## Why

On Claude Code 2.1.316 an `mcp__*` entry in a subagent's `tools:` grants nothing: the agent launches with no tool from any MCP server. Naming a server instead (`mcp__<server>__<tool>`) works only for the one repository that runs that server, and every Shipyard agent serves every repository. An agent with no `tools:` field inherits the session's whole tool pool, the repository's own MCP servers included, so each agent instead carries a `disallowedTools:` deny-list derived from `AGENT_GRANTS` in `scripts/validate.py` and checked against it by exact equality. `skills/shared/references/docs-mcp.md` governs how agents use what they inherit.

## Prior requests

- AM-1801: the allowlist variants were probed and rejected in favour of the deny-list.
