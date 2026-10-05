# Setting an agent's effort per repository

`models.agents.<name>.effort` records intent and enforces floors; it never changes the effort an agent runs at.

## Why

The `Agent` tool takes no effort parameter. A subagent's effort comes only from its definition frontmatter, which Claude Code parses at plugin load, before any substitution, and an `effort: ${user_config.KEY}` value is rejected at load. Frontmatter ships with the plugin, so effort cannot vary per repository. `docs/configuration.md` § Effort is not a runtime knob has the evidence.
