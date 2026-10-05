# Splitting wide refactors into expand, migrate-in-batches, contract

`/sy:plan` does not decompose a wide mechanical change (a rename, a retyped shared symbol) into an expand ticket, batched migration tickets, and a contract ticket.

## Why

Migrating in batches is slower, and on a repository with many active contributors each batch is another window for merge conflicts. One PR that makes the whole change is almost always the better choice. Expand/contract stays available as verification evidence for a migration whose old and new versions must coexist at runtime; it is just not a ticket shape.

## Prior requests

- AM-1607: proposed from the mattpocock/skills `to-tickets` skill.
