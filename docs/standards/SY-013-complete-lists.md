---
id: SY-013
severity: MED
---
# SY-013 A list of every command or agent stays complete

`CONTRIBUTING.md` step 5 is the canonical list of user commands, and `agents/` is the canonical set of agents. Any other text that lists every command or every agent (README, `docs/`, `install.sh`, `agent-guide.md`, and the scope lines in `skills/shared/references/*.md`) names each one. A change that adds, renames or removes a command or agent updates every such list in the same PR.

A finding names the list and the missing or stale entry. A list that names a subset and says so, such as "skills that write the tracker", is checked against its own criterion, not the full set.

Enforced by: `shipyard-standards` review mode (`.claude/skills/shipyard-standards/SKILL.md`).
