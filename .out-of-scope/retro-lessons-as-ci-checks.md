# Turning retrospective lessons into new CI checks

The ship retrospective does not propose a new lint rule, pre-commit hook, or CI job for each mechanical mistake it finds.

## Why

One check per lesson bloats a repository's CI tooling until the checks cost more to maintain than the mistakes they catch. The retrospective proposes a standards-doc edit when a run surfaces a new team decision, and durable tool-level lessons go to cross-session memory.

## Prior requests

- AM-1607: proposed from the mattpocock/skills `retro` skill.
