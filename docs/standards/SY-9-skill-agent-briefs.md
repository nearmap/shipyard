---
id: SY-9
severity: MED
---
# SY-9 Skills and agents are written to be followed

Applies to `skills/**` and `agents/**`.

- Each instruction names its trigger and its action. "Consider", "if needed" or "where appropriate" with no stated test is a finding.
- A brief to a delegate states the goal, the scope, what is already ruled out, and the return form it expects.
- A dispatch resolves model and effort as `skills/shared/references/model-dispatch.md` says, never from a literal in the text.
- An agent does one job, and its tools are the fewest that job needs.

Enforced by: `shipyard-standards` review mode. `scripts/validate.py` already enforces agent frontmatter, model floors, tool deny-lists, return contracts and `${CLAUDE_PLUGIN_ROOT}` paths (`check_agent_*`, `check_no_home_paths`); cite those checks, never restate them.
