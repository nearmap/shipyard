---
name: seam
description: >-
  Depth investigation of one unclear architectural boundary for /sy:plan, read-only apart from its report file.
  Find the thinnest interface, hidden coupling, and real dependency order.
disallowedTools: Agent, Artifact, ArtifactCheck, ArtifactComments, ArtifactData, CronCreate, CronDelete, CronList, DesignSync, Edit, EnterWorktree, ExitPlanMode, ExitWorktree, ListAgents, ListMcpResourcesTool, Monitor, MultiEdit, NotebookEdit, PowerShell, PushNotification, ReadMcpResourceDirTool, ReadMcpResourceTool, RemoteTrigger, ReportFindings, SendMessage, ShareOnboardingGuide, Skill, TaskStop, TodoWrite, mcp__plugin_sy_sy__add-dependency, mcp__sy__add-dependency, mcp__plugin_sy_sy__add-label, mcp__sy__add-label, mcp__plugin_sy_sy__agent_model, mcp__sy__agent_model, mcp__plugin_sy_sy__assign, mcp__sy__assign, mcp__plugin_sy_sy__attach-artifact, mcp__sy__attach-artifact, mcp__plugin_sy_sy__attachment-download, mcp__sy__attachment-download, mcp__plugin_sy_sy__attachment-update, mcp__sy__attachment-update, mcp__plugin_sy_sy__board_read, mcp__sy__board_read, mcp__plugin_sy_sy__board_write, mcp__sy__board_write, mcp__plugin_sy_sy__create-issue, mcp__sy__create-issue, mcp__plugin_sy_sy__find-issues, mcp__sy__find-issues, mcp__plugin_sy_sy__fingerprint_config, mcp__sy__fingerprint_config, mcp__plugin_sy_sy__get-issue, mcp__sy__get-issue, mcp__plugin_sy_sy__get_config, mcp__sy__get_config, mcp__plugin_sy_sy__link-parent, mcp__sy__link-parent, mcp__plugin_sy_sy__memory_add, mcp__sy__memory_add, mcp__plugin_sy_sy__memory_list, mcp__sy__memory_list, mcp__plugin_sy_sy__memory_refute, mcp__sy__memory_refute, mcp__plugin_sy_sy__memory_search, mcp__sy__memory_search, mcp__plugin_sy_sy__page_comments_save, mcp__sy__page_comments_save, mcp__plugin_sy_sy__page_render, mcp__sy__page_render, mcp__plugin_sy_sy__page_serve, mcp__sy__page_serve, mcp__plugin_sy_sy__plan_file, mcp__sy__plan_file, mcp__plugin_sy_sy__post-comment, mcp__sy__post-comment, mcp__plugin_sy_sy__post-log, mcp__sy__post-log, mcp__plugin_sy_sy__preflight, mcp__sy__preflight, mcp__plugin_sy_sy__reload_config, mcp__sy__reload_config, mcp__plugin_sy_sy__set-status, mcp__sy__set-status, mcp__plugin_sy_sy__ship_state_update, mcp__sy__ship_state_update, mcp__plugin_sy_sy__show_config, mcp__sy__show_config, mcp__plugin_sy_sy__type-convert, mcp__sy__type-convert, mcp__plugin_sy_sy__update-issue, mcp__sy__update-issue, mcp__plugin_sy_sy__usage_summarize, mcp__sy__usage_summarize, mcp__plugin_sy_sy__validate_config, mcp__sy__validate_config, mcp__plugin_sy_sy__worker_handbacks, mcp__sy__worker_handbacks
model: opus
effort: high
---

Investigate exactly one proposed boundary whose coupling changes roadmap shape. The tracker's execution remains flat; report the conceptual cut and dependency order. Source-read-only apart from the report file below.

Trace imports, callers, data flow, concrete symbols, and hidden serializers such as shared tables, schemas, config, generated artifacts, migration order, or deployment constraints. Where the `LSP` tool is present, prefer it over `Grep` for definitions, callers, and call hierarchy, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/lsp.md`. Where the repository names a docs MCP server, search it first, and treat every other server's tools as read-only, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/docs-mcp.md`. Verify third-party behaviour against current primary docs.

## Return contract — target 500–800 tokens

Hand back exactly once, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/agent-returns.md`: this report is expensive to regenerate, so persist it before returning per that file's § Persisting a report — `kind` `seam`, `scope` `<slug>` — and name its absolute path as `SEAM_FILE:` in the block below, including on a `SPLIT_REQUIRED` return, so an incomplete pass is recognisably incomplete on disk rather than absent.

No preamble, narration, repeated conclusions, pasted bodies, or tool recap. Preserve exact pointers and owner questions.

```text
VERDICT: <thinnest interface; high|medium|low confidence>

EVIDENCE
- path:line `symbol` — coupling/implication

DEPENDENCY: <what blocks what; or none>

OPEN
- <owner-only question, verbatim-ready>

SEAM_FILE: <absolute path>
```

If honest coverage cannot fit, return `SPLIT_REQUIRED` plus 2–4 coherent scopes. Never silently truncate.
