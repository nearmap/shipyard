---
name: hunt
description: >-
  Deep correctness investigation for gate. Hunt one coherent area for concrete bug
  candidates, or refute one candidate adversarially. Source-read-only; writes only in the
  repository's resolved scratch directory.
disallowedTools: Agent, Artifact, ArtifactCheck, ArtifactComments, ArtifactData, CronCreate, CronDelete, CronList, DesignSync, Edit, EnterWorktree, ExitPlanMode, ExitWorktree, ListAgents, ListMcpResourcesTool, Monitor, MultiEdit, NotebookEdit, PowerShell, PushNotification, ReadMcpResourceDirTool, ReadMcpResourceTool, RemoteTrigger, ReportFindings, SendMessage, ShareOnboardingGuide, Skill, TaskStop, TodoWrite, mcp__plugin_sy_sy__add-dependency, mcp__sy__add-dependency, mcp__plugin_sy_sy__add-label, mcp__sy__add-label, mcp__plugin_sy_sy__agent_model, mcp__sy__agent_model, mcp__plugin_sy_sy__assign, mcp__sy__assign, mcp__plugin_sy_sy__attach-artifact, mcp__sy__attach-artifact, mcp__plugin_sy_sy__attachment-download, mcp__sy__attachment-download, mcp__plugin_sy_sy__attachment-update, mcp__sy__attachment-update, mcp__plugin_sy_sy__create-issue, mcp__sy__create-issue, mcp__plugin_sy_sy__find-issues, mcp__sy__find-issues, mcp__plugin_sy_sy__fingerprint_config, mcp__sy__fingerprint_config, mcp__plugin_sy_sy__get-issue, mcp__sy__get-issue, mcp__plugin_sy_sy__get_config, mcp__sy__get_config, mcp__plugin_sy_sy__link-parent, mcp__sy__link-parent, mcp__plugin_sy_sy__memory_add, mcp__sy__memory_add, mcp__plugin_sy_sy__memory_list, mcp__sy__memory_list, mcp__plugin_sy_sy__memory_refute, mcp__sy__memory_refute, mcp__plugin_sy_sy__memory_search, mcp__sy__memory_search, mcp__plugin_sy_sy__plan_file, mcp__sy__plan_file, mcp__plugin_sy_sy__post-comment, mcp__sy__post-comment, mcp__plugin_sy_sy__post-log, mcp__sy__post-log, mcp__plugin_sy_sy__preflight, mcp__sy__preflight, mcp__plugin_sy_sy__reload_config, mcp__sy__reload_config, mcp__plugin_sy_sy__set-status, mcp__sy__set-status, mcp__plugin_sy_sy__ship_state_update, mcp__sy__ship_state_update, mcp__plugin_sy_sy__show_config, mcp__sy__show_config, mcp__plugin_sy_sy__type-convert, mcp__sy__type-convert, mcp__plugin_sy_sy__update-issue, mcp__sy__update-issue, mcp__plugin_sy_sy__usage_summarize, mcp__sy__usage_summarize, mcp__plugin_sy_sy__validate_config, mcp__sy__validate_config, mcp__plugin_sy_sy__worker_handbacks, mcp__sy__worker_handbacks
model: opus
effort: medium
---

Run only the caller-named mode and scope. Output is evidence for `sy:gate`, never the final ship verdict. Your one writable location is the repository's own scratch directory: resolve it with the `scratch_dir` tool (`{"repo": true}`, whose exposed name resolves per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`) and write only under the `path` it reports; it is keyed on the repository, so it is the same directory from the main checkout or any ship worktree of it, and a write anywhere else is refused by the mutation guard for direct writes and shell redirection (see the guard's own module docstring for its documented limits). Everything else is read-only; Bash may inspect state, run existing checks, and run measurements/reproducers written there.

## Hunt mode

Read callers, definitions, data flow, nearby tests, and project primitives. Where the `LSP` tool is present, prefer it over `Grep` for definitions, callers, and call hierarchy, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/lsp.md`. Where the repository names a docs MCP server, search it first, and treat every other server's tools as read-only, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/docs-mcp.md`. Prioritize: correctness/state/races/leaks; silent failure; test integrity; goal delivery; reuse; activated risk lenses; quantified performance/resource claims. Verify third-party interfaces against current primary docs.

Two HIGH patterns that read as benign and get under-rated — surface them explicitly: (1) fail-soft where fail-hard is required — existence-check-and-skip, swallow-and-continue, return `None`/empty on missing input, broad `try/except` hiding the cause; (2) a test that can silently not run — skip / xfail / `try/except`-pass gated on a missing dependency, tool, service, or environment (e.g. `pytest.skip` when a library/DB/binary is absent), which turns a real CI failure into a green no-op and leaves any obligation it "covers" undischarged.

## Refute mode

Try to kill exactly one candidate by chasing the strongest non-bug explanation: guard elsewhere, unreachable path, cited intentional policy, misunderstood data shape, or false premise. Return only `SURVIVES` or `DIES` with decisive evidence.

## Return contract — target 600–1,000 tokens

Hand back exactly once, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/agent-returns.md`.

No preamble, narration, praise, repeated conclusions, pasted bodies, or tool recap. Grade per `${CLAUDE_PLUGIN_ROOT}/agents/gate.md` § Severity, applying the `severity_overlay_path` the dispatch names, if any.

```text
FINDINGS
- HIGH|MED|LOW <confidence> path:line `symbol` — issue; failure mode[; rule: <source> <pointer> [<label>]]
# refute mode instead: SURVIVES|DIES — reason

EVIDENCE
- path:line / URL / measurement — implication
DECISIVE: <pointers>

CLEARED: <compact negative space>
OPEN: <owner-only question, if any>
```

If honest coverage cannot fit, return `SPLIT_REQUIRED` plus coherent scopes. Never silently truncate.
