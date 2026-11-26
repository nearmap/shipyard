---
id: SY-007
severity: MED
---
# SY-007 Text earns its place

Applies to every added line of text: skills, references, agent briefs, dispatch prompts, plans, docs and PR bodies. Each passes the two cut tests in `skills/shared/references/context-economy.md`; cite that file, never restate the tests. Human prose (READMEs, `docs/`) is clear and never hard-wrapped to a column; machine-facing text (briefs, contracts, logs) is terse and structured.

A finding names one of: a fact the change states that already lives at a `path:line` its reader can reach (cite both copies); reasoning for a settled decision in machine-facing text; human prose hard-wrapped. "Could be shorter" with none of these is not a finding. Growth of agent-facing text is SY-002's.

Enforced by: `shipyard-standards` review mode; `scripts/validate.py` keeps the cut tests to one copy.
