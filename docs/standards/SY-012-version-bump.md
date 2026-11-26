---
id: SY-012
severity: MED
---
# SY-012 Consumer-visible changes bump the version

A change under `skills/`, `agents/`, `hooks/`, `sy_tools/`, `scripts/` or `docs/` changes `version` in `.claude-plugin/plugin.json`. `claude plugin update` gates on that string, not the git SHA: without a bump, installed copies stay on stale content. After merging, tag with `claude plugin tag --push` so the tag and `plugin.json` agree.

A finding names a range that changes one of those paths and leaves `version` unchanged.

Enforced by: `shipyard-standards` review mode (`.claude/skills/shipyard-standards/SKILL.md`).
