---
id: SY-005
severity: HIGH
---
# SY-005 Nothing private in a public repo

Nothing tracked in this repository, and nothing posted to its PRs or commits, names an internal team, tool, product or repository, a private individual, a spend or usage figure, or a path on someone's machine.

Allowed: the public repository and organisation URL, authorship and licence fields, public authors and handles cited as sources, and runtime locations written for any user (`~/.claude/...`). Examples and fixtures use generic names (`example.com`).

Enforced by: `shipyard-standards` review mode over added lines (`.claude/skills/shipyard-standards/SKILL.md`). Nothing checks PR bodies or commit messages automatically.
