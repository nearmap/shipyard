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

## `resolve <scope>`

Return the block in `skills/standards/references/resolve.md` § Compact return contract:

- `AUTHORITY`: `shipyard-standards`.
- `CONTRACT`: one line per rule that applies to the scope, as `<id> [<severity>] <the rule in one line> — docs/standards/<file>`. When the scope touches text SY-6 covers, add `SY-6 [LOW] design invariant the plan carries: "SY-6: gate scope on skill text" — docs/standards/SY-6-skill-text-bar.md`.
- `SOURCES`: `docs/standards/`, `.claude/skills/shipyard-standards/`, `.shipyard/severity-overlay.yaml`.

## `review <base>..<head> [paths]`

Read `git diff <base>..<head>`, limited to `paths` when given. Check added lines against every part except SY-2, SY-6 and SY-11: SY-2 needs the PR body, which this pass cannot see; SY-6 is an acceptance bar, not a rule text can break; a test enforces SY-11.

Return the block in `skills/standards/references/review.md` § Compact return contract. Each finding's grade equals its part's `severity` and carries `rule: shipyard-standards <id> [<severity>]`. Set `BEHAVIOURAL_LENSES` to name SY-6 when the range touches text it covers.

Never raise SY-6, and never raise a finding whose only fix is rewording text that is already true.
