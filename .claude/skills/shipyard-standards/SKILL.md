---
name: shipyard-standards
description: >-
  Shipyard's own engineering standards. Resolve the rules that apply to a scope, or review a commit
  range against them with cited, graded findings. The repository's `skills.standards` skill.
argument-hint: "[resolve|review] <scope>"
---

# Shipyard standards

The rules are the parts in `docs/standards/`, one file per rule. Each part's frontmatter carries its `id` and `severity`; the body is the rule. Cite a part; never restate it.

## Load

Read every `docs/standards/*.md`. Return `blocked`, naming the file, when a part has no `id`, has no `severity` of `HIGH`, `MED` or `LOW`, or repeats another part's `id`.

## Modes

- `resolve <scope>`: read `.claude/skills/shipyard-standards/references/resolve.md`.
- `review <base>..<head> [paths]`: read `.claude/skills/shipyard-standards/references/review.md`.
