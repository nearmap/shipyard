# Review mode

When `skills.standards` names the repository's own standards skill (per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md`), invoke its review mode and return its cited findings — skipping the invocation only when the caller states that skill already ran for this scope. Do not duplicate the pass.

Otherwise resolve authoritative repo docs and review only applicable rules. Every standards finding requires exact code pointer, exact rule/section pointer, severity, and concrete fix. Preference is not policy.

## Compact return contract

```text
FINDINGS
- HIGH|MED|LOW code/path:line — issue; rule: <source> <rule/path:section> [<label>] — fix

CLEARED: <compact negative space>
AUTHORITY: <skill/docs/fallback>
BEHAVIOURAL_LENSES: <items gate/hunt must investigate as correctness, not conformance>
```

`HIGH|MED|LOW` stays the generic grade. When the repository's own standards skill ran, `[<label>]` is that skill's label for the finding, verbatim. When `skills.standards` is null, take it only from the rule entry's metadata per `${CLAUDE_PLUGIN_ROOT}/docs/severity-overlay.md` § Rules rule (f), never from a doc body and never a class label; otherwise omit it.

Load `fallback-risk.md` only if the repository genuinely has no applicable authority or when a risk-lens catalogue is needed.
