# A one-way or two-way door call at merge

The merge handoff does not ask the author to classify the change as reversible or not, or to estimate its blast radius.

## Why

Shipyard's users roll forward rather than revert: the work is mostly batch processing and algorithm development rather than live services, so a fix-forward PR is nearly always the recovery path. A reversibility label would be boilerplate on almost every merge. Changes that genuinely cannot be undone (migrations, published data) are already covered by risk lenses and their verification obligations at spec time.

## Prior requests

- AM-1607: proposed from the mattpocock/skills `pr` skill's Merge Danger section.
