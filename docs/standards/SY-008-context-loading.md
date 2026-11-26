---
id: SY-008
severity: MED
---
# SY-008 Context is loaded when it is used

Applies to skills and agent briefs.

- A skill's `SKILL.md` carries only what every invocation needs. A procedure only one mode or branch needs lives in a `references/` file read by path on that branch; one shared by two skills lives in `skills/shared/references/`.
- A broad read (many files, a CI log, a PR's threads) goes to a delegate that returns conclusions and `path:line` pointers, never the contents.
- Verbose command output (test suites, linters, type checks) goes to a log in the scratch directory; the reader reads back failures and summary lines only.

A finding names the added line that loads text its reader does not act on at that point.

Enforced by: `shipyard-standards` review mode.
