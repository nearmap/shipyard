# Language-server navigation

A repository may declare language servers in its own Claude Code layer — `lspServers` in a plugin manifest or a plugin-root `.lsp.json`, or in `.claude/settings.json`. Where it does, an agent whose `tools:` allowlist names `LSP` gets one tool of that name carrying `goToDefinition`, `findReferences`, `hover`, `documentSymbol`, `workspaceSymbol`, `goToImplementation`, and `prepareCallHierarchy`/`incomingCalls`/`outgoingCalls`. Shipyard configures none of it and has no config key for it: Claude Code owns server startup, so a Shipyard-side server list would be a second source of truth that never takes effect.

## Availability is discovered, never assumed

The tool is absent in a repository that declares no server, and present-but-dead when the server's command is not on the session's PATH — a `ty` or `gopls` living in a project environment fails `ENOENT` in a session started outside that environment. Neither is an error to fix, a blocker, or a finding: fall back to `Grep`/`Read` and name the navigation you could not do type-aware rather than presenting the fallback as equivalent. Never spend a ship round repairing a repository's language-server configuration — it is outside every phase's scope, and belongs to the repository's own Claude Code setup. A server rooted at the session's checkout does answer for files in a ship worktree of the same repository, so working in a worktree is not itself a reason to expect it unavailable.

## Prefer it where types beat text

Use it for the symbol questions `Grep` answers badly: which definition a call actually binds to, every real caller of a re-exported or overloaded name, where a symbol lives given only its name (`workspaceSymbol`), and the blast radius of a signature change (call hierarchy). `Grep` stays right for text, config, comments, and non-code surfaces, and for any pattern that no symbol names.

## Diagnostics arrive when you ask

A configured server publishes a changed file's diagnostics into the editing agent's context, but it starts lazily: edits made before the session's first `LSP` call surface nothing. An agent that changes code and wants its type errors early makes one `LSP` call against a file it just changed. What comes back is a fast pre-check, never a substitute for the repository's own tests and checks — a clean diagnostic set discharges no verification obligation.
