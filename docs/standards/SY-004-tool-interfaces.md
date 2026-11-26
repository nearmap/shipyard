---
id: SY-004
severity: MED
---
# SY-004 Tool parameters have one shape

A new or changed `sy` tool parameter has one type: no `str | list[str]`, and no value that is sometimes a name and sometimes an id. A parameter that takes an agent name accepts both the bare and the `sy:`-prefixed form. Input the tool acts on is structured fields, never prose it parses. Fix the interface before writing prose about how to call it.

Applies to new and changed parameters. An existing parameter conforms when a change touches it.

Enforced by: `shipyard-standards` review mode (`.claude/skills/shipyard-standards/SKILL.md`).
