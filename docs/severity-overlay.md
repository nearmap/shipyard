# Severity overlay

## Purpose

An overlay tunes how `/sy:ship` grades findings in one repository. It maps the labels your own review skill or standards doc emits onto Shipyard's four grades, and pins defect classes your team has learned to treat as more or less serious than the generic scale would. `sy:gate`, `sy:hunt` and `repo-review` grade with it, and `sy:gate-triage` uses it when it decides whether a finding is `breaking`. It cannot make fail-soft handling or a check that can silently not run anything but HIGH, and it changes nothing about which reviewers run, at what model, or over what scope. The generic scale it adjusts is [`agents/gate.md` § Severity](../agents/gate.md#severity).

## Enabling it

Commit the file at exactly `.shipyard/severity-overlay.yaml`, beside `.shipyard/config.json`. There is no config key and no other location: an overlay anywhere else is never read, and if no file is there, the generic scale applies.

`/sy:ship` reads the version on the target branch when its START phase runs, never the copy in a working tree, so a pull request's edits to the overlay take effect only on ships that start after it merges. A file that is present but malformed makes START return `blocked`, naming the path and the rule below that failed.

If you want the overlay's content shown in other docs as well, link to the file from them and never copy it.

## File structure

The file is YAML: one mapping with exactly these three top-level keys and no others, so a misspelt key fails the check rather than being silently ignored.

- `scale_mapping`: a list with one entry per label, each a mapping with exactly these keys:
  - `source` names a review skill or standards doc.
  - `label` is a severity label that source emits.
  - `grade` is one of `HIGH`, `MED`, `LOW` or `NIT`.
- `pinned_patterns`: a list, each entry a mapping with exactly these keys:
  - `grade` is one of `HIGH`, `MED`, `LOW` or `NIT`.
  - `pattern` names the defect class in one line.
  - `recognise_by` gives a concrete tell: a symbol, an import, a call shape, or a doc claim.
- `notes`: a string for rationale and links that cover the whole file, which may be empty (`notes: ""`).

Every entry value is a non-empty string, and either list may be empty (`[]`). Quote a value that YAML would otherwise read as something else, such as `yes`, `1.0`, or anything containing `: `. A YAML comment is the natural place for one entry's rationale: the check ignores comments, and the agents read them as context.

## Rules

Every overlay obeys these, and the agents enforce them when they grade:

- (a) Fail-soft handling, and a check that can silently not run, stay HIGH whatever the overlay says.
- (b) A pin at MED or above makes the finding `breaking`, so it is never deferred by the terminal pass.
- (c) A pin may lower a finding to LOW or NIT, except under rule (a).
- (d) When a finding matches both a `scale_mapping` entry and a pin, the pin wins.
- (e) When it matches two pins, the higher grade wins.

## Worked example

```yaml
scale_mapping:
  - source: code-review
    label: must-fix
    grade: MED
  - source: code-review
    label: suggestion
    grade: LOW

pinned_patterns:
  # Unbounded retries have twice turned a downstream outage into a queue backlog; MED keeps them from being deferred.
  - grade: MED
    pattern: Unbounded retry against a network service
    recognise_by: "a `while True:` loop around an HTTP or RPC call with no attempt cap"
  - grade: LOW
    pattern: Missing docstring on a private helper
    recognise_by: "an `_`-prefixed function or method with no docstring"

notes: "Labels are the ones the repository's `code-review` skill emits; revisit the pins after each incident review."
```

One finding traced through it: `code-review` labels a missing docstring on `_parse_row` as `must-fix`. The mapping entry alone would grade it MED, but the LOW pin matches too and the pin wins (rule d), so `sy:gate` reports it under LOW. Nothing about it is fail-soft or a silenced check, so rule (a) does not apply and the lowering stands (rule c). Triage sets IMPACT `non-breaking`: it changes no behaviour and misleads no reader. A LOW, `non-breaking` accepted finding is deferrable, so on a terminal pass it is posted to the pull request as a deferred finding rather than fixed. Had the same label landed on an unbounded retry loop instead, the MED pin would make it `breaking` (rule b) and it would be fixed.

## What START checks

Run against the target branch's copy, with `pixi run --manifest-path <plugin root>/pyproject.toml overlay check <absolute path>`, which runs from the plugin root:

1. The file exists; a missing file exits 2. START tests existence with `git ls-tree` before running the check, so any non-zero exit from the check on START's copy returns `blocked`.
2. The file parses as YAML into a mapping with exactly the keys `scale_mapping`, `pinned_patterns` and `notes`, where the first two are lists and `notes` is a string.
3. Every `scale_mapping` entry is a mapping with exactly the keys `source`, `label` and `grade`, every `pinned_patterns` entry one with exactly `grade`, `pattern` and `recognise_by`, and each value is a non-empty string.
4. Every `grade` is `HIGH`, `MED`, `LOW` or `NIT`.

A failure exits 1 and prints one line per failed rule. Nothing else is checked: comments are ignored, and whether an entry's content is sensible is left to the agents and to review of the file itself.
