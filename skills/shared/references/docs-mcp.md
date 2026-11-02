# Repository MCP servers

A repository may connect MCP servers in its own Claude Code setup — `.mcp.json`, `.claude/settings.json`, or a plugin it enables — and every Shipyard agent inherits them along with every other MCP server in the session, user-scope servers and connectors included: an agent's frontmatter denies only the built-in and `sy` tools it is not granted, so another server's tools reach it untouched. Shipyard configures none of them and has no config key for them. Which servers exist, and which one carries the repository's documentation, is the repository's to say, normally in its `CLAUDE.md`.

## Availability is discovered, never assumed

A repository may connect no docs server, or one that is unreachable or still connecting from this session. An absent or failing server is not an error to fix, a blocker, or a finding: fall back to `Read`/`Grep` over the files themselves and name what you could not check against the docs rather than presenting the fallback as equivalent. MCP tools arrive deferred, `sy` tools included: load one with `ToolSearch` before its first call. Never spend a ship round repairing a repository's MCP configuration; it belongs to the repository's own Claude Code setup.

## Search the docs first

When the repository's `CLAUDE.md` names a docs MCP server, search it before reading the files it covers, and follow that file's own rules for using it — session ids, hit verdicts, feedback on a search. What the docs say is a lead: code, `LSP` where you hold it, and `Grep` verify and deepen it, and the code wins where the two disagree.

## Read only

A tool from another MCP server is for reading. Never call one that sends, posts, creates, or writes — a message, a comment, a page, a file — beyond the search bookkeeping the repository's `CLAUDE.md` asks of its docs server. The tracker is reached only through `sy` verbs. The one exception is `/sy:whiteboard` publishing its own decision page to a private hosted page, and only when the user picked that through `AskUserQuestion`.
