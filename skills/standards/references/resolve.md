# Resolve mode

## Authority

Resolve in order:

1. the repository's own standards skill, named by `skills.standards` (per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`) — a value set but uninvokable fails loud rather than falling to tier 2;
2. repository docs/rules such as `CLAUDE.md`, `CONTRIBUTING.md`, `AUTHORING.md`, `.claude/rules/`, standards directories, language guides, and documents explicitly referenced by them;
3. `fallback-risk.md` only when no applicable repo authority exists.

Do not blend contradictions into a compromise; name and follow the higher authority.

## Scope reading

Read a small cohesive rule section directly. For large standards sets, map first and load only sections relevant to the change. Authority coverage matters; copying the handbook does not.

## Compact return contract

```text
AUTHORITY
- <skill or exact file:section>

CONTRACT
- <task-relevant rule> — <source pointer>

PRIMITIVES
- <helper/library/pattern/command to reuse>

LENSES
- <only surface-justified risk lens>

CONFLICTS/UNKNOWNS
- <owner decision; or none>
```

Do not paste generic style into every Task. `/sy:spec` records authority and unusual task-specific constraints; `/sy:ship` and `sy:slice` receive the relevant compact contract.
