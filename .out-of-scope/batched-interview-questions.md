# Asking several questions in one turn

Every Shipyard skill that interviews the user asks one question per turn, even where `AskUserQuestion` could carry up to four.

## Why

Several questions at once overload the person answering, and no real conversation works that way: the answer to the first usually reshapes the second. Batching saves turns at the cost of worse answers. `skills/shared/references/user-interaction.md` § Question is the rule.

## Prior requests

- AM-1607: proposed from the mattpocock/skills `grilling` skill (rounds of every question whose prerequisites are settled).
