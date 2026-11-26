---
id: SY-003
severity: MED
---
# SY-003 New checks start advisory

A new check (a validator check, a denying guard or hook, or a required CI job) first reports without failing or denying. A later change makes it required, and that change cites the session or PR data that supports it.

Applies to Shipyard's own checks. Out of scope: `scripts/validate.py` phrase pins, which fail on text the same change writes.

Enforced by: `shipyard-standards` review mode (`.claude/skills/shipyard-standards/SKILL.md`).
