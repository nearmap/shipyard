---
name: trace
description: >-
  Depth trace of one load-bearing behaviour or data path for /sy:spec, /sy:spike, or a /sy:ship parent, read-only apart from its report file.
  Follow it end to end, expose breaking cases, and return decisive evidence pointers.
tools: Read, Grep, Glob, Bash, Write, WebFetch, WebSearch, mcp__plugin_sy_sy__scratch_dir, mcp__sy__scratch_dir, mcp__plugin_sy_sy__check_env, mcp__sy__check_env
model: opus
effort: high
---

Trace exactly one behaviour, call chain, schema path, or data flow. Source-read-only apart from the report file below. Follow entry points, callers, definitions, transforms, sources, sinks, configuration/order dependencies, and breaking cases. Verify third-party interfaces against current primary docs.

## Return contract — target 700–1,000 tokens

Hand back exactly once, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/agent-returns.md`: this report is expensive to regenerate, so persist it before returning per that file's § Persisting a report — `kind` `trace`, `scope` `<slug>` — and name its absolute path as `TRACE_FILE:` in the block below, including on a `SPLIT_REQUIRED` return, so an incomplete pass is recognisably incomplete on disk rather than absent.

No preamble, narration, repeated conclusions, pasted bodies, or tool recap. Preserve exact paths, symbols, URLs, and decisive spans.

```text
PATH: <concise end-to-end path; confidence high|medium|low>

EVIDENCE
- path:line `symbol` — role/implication
DECISIVE: path:start-end, path:start-end

BREAKS
- <input/config/order> — <failure/divergence>

OPEN
- <owner-only question, verbatim-ready>

TRACE_FILE: <absolute path>
```

If one path is still too broad, return `SPLIT_REQUIRED` plus coherent subpaths. Never silently truncate.
