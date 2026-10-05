# A Shipyard config key for language servers

Shipyard has no setting that declares or starts language servers.

## Why

Claude Code owns server startup: a repository declares servers in its own Claude Code layer (`lspServers` in a plugin manifest, a plugin-root `.lsp.json`, or settings), and Claude Code exposes them as the `LSP` tool. Shipyard spawns no processes, so a server list in `.shipyard/config.json` could never take effect. The agents that hold `LSP` use whatever the repository started. `docs/configuration.md` § Language servers are not a Shipyard setting is the user-facing statement.

## Prior requests

- AM-1521 (PR #37): proposed as the first fix, ruled out in review.
