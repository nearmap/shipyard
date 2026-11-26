---
id: SY-3
severity: MED
---
# SY-3 New checks start advisory

A new check (a validator check, a denying guard or hook, or a required CI job) first reports without failing or denying. A later change makes it required, and that change cites the session or PR data that supports it.

Applies to Shipyard's own checks.

Enforced by: `shipyard-standards` review mode (`.claude/skills/shipyard-standards/SKILL.md`).
