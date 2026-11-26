---
id: SY-006
severity: LOW
---
# SY-006 Acceptance bar for findings on skill text

Applies to review findings on skill, agent and doc text: `skills/**`, `agents/**`, `docs/**`, `*.md`, `.claude/skills/**`, and MCP tool docstrings and `Field(description=...)`. Accept such a finding only when following the text loses data or fails in a normal setup (a documented install and configuration: `docs/installation.md`, `docs/configuration.md`). Reject every other one, citing SY-006.

Still accepted: anything `docs/severity-overlay.md` § Rules (a) keeps HIGH; a shipped claim that leads a reader to the wrong action in a normal setup; any finding citing another rule in `docs/standards/` or the § Net-new agent-facing text outcome (`skills/ship/references/immutable-gate.md`).

SY-006 bars only findings that cite no rule. Text never violates it, and the standards review never raises it. Applied through the plan invariant `shipyard-standards` resolve mode emits.
