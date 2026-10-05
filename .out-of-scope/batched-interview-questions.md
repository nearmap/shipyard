# Asking a list of questions in prose

No Shipyard skill asks the user several questions as a prose list and expects a matching list of answers back.

## Why

A prose list asks the person to hold every question at once and compose a reply that maps back onto it, and a skipped or merged answer goes unnoticed. Batching belongs in `AskUserQuestion`: up to four questions in one call, each with its own options and a free-text "Other", so every answer arrives attached to its question. `skills/shared/references/user-interaction.md` § Question is the rule.

## Prior requests

- AM-1607: proposed from the mattpocock/skills `grilling` skill, which asks numbered rounds of questions in prose with a recommended answer under each.
