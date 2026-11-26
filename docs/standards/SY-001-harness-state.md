---
id: SY-001
severity: MED
---
# SY-001 Harness state is code's job

State the harness owns (SHAs, statuses, counts, round numbers, phases, tool arguments) is written and checked by code. It is never kept in prose for a model to track, and never derived by parsing prose or model output. Where a tool would parse prose, change its interface to take structured fields instead, as `post-comment` does with `human` and `agent_detail` (`sy_tools/server.py`).

Applies to new and changed code and text. An existing parser stays until a task replaces it; a change must not add one. Out of scope: `scripts/validate.py` phrase pins, which check text rather than hold state.

Enforced by: `shipyard-standards` review mode (`.claude/skills/shipyard-standards/SKILL.md`).
