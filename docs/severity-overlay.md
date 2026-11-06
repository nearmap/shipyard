# Severity overlay

## Purpose

An overlay tunes how `/sy:ship` grades findings in one repository. It maps the labels your own review skill or standards doc emits onto Shipyard's four grades, and pins defect classes your team has learned to treat as more or less serious than the generic scale would. `sy:gate`, `sy:hunt` and `repo-review` grade with it, and `sy:gate-triage` uses it when it decides whether a finding is `breaking`. It cannot make fail-soft handling or a check that can silently not run anything but HIGH, and it changes nothing about which reviewers run, at what model, or over what scope. The generic scale it adjusts is [`agents/gate.md` § Severity](../agents/gate.md#severity).

## Enabling it

Commit the file at exactly `.shipyard/severity-overlay.md`, beside `.shipyard/config.json`. There is no config key and no other location: an overlay anywhere else is never read, and if no file is there, the generic scale applies.

`/sy:ship` reads the version on the target branch when its START phase runs, never the copy in a working tree, so a pull request's edits to the overlay take effect only on ships that start after it merges. A file that is present but malformed makes START return `blocked`, naming the path and the rule below that failed.

If you want the overlay's content shown in other docs as well, link to the file from them and never copy it.

## File structure

The file is Markdown, and these three `##` sections are required, in this order. Any other heading or prose is allowed, ignored by the structure check, and read by the agents as context.

- `## Scale mapping`: one Markdown table with exactly the columns `Source | Label | Grade`.
  - `Source` names a review skill or standards doc.
  - `Label` is a severity label that source emits.
  - `Grade` is one of `HIGH`, `MED`, `LOW` or `NIT`.
  - One row per label. The table may be empty, with a header row only.
- `## Pinned patterns`: one Markdown table with exactly the columns `Grade | Pattern | Recognise by`.
  - `Grade` is one of `HIGH`, `MED`, `LOW` or `NIT`.
  - `Pattern` names the defect class in one line.
  - `Recognise by` gives a concrete tell: a symbol, an import, a call shape, or a doc claim.
  - The table may be empty.
- `## Notes`: free prose, which may be empty. Put the rationale and links here, never in the tables.

## Rules

Every overlay obeys these, and the agents enforce them when they grade:

- (a) Fail-soft handling, and a check that can silently not run, stay HIGH whatever the overlay says.
- (b) A pin at MED or above makes the finding `breaking`, so it is never deferred by the terminal pass.
- (c) A pin may lower a finding to LOW or NIT, except under rule (a).
- (d) When a finding matches both a mapping row and a pin, the pin wins.
- (e) When it matches two pins, the higher grade wins.

## Worked example

```markdown
# Severity overlay

## Scale mapping

| Source | Label | Grade |
|---|---|---|
| `code-review` | `must-fix` | MED |
| `code-review` | `suggestion` | LOW |

## Pinned patterns

| Grade | Pattern | Recognise by |
|---|---|---|
| MED | Unbounded retry against a network service | a `while True:` loop around an HTTP or RPC call with no attempt cap |
| LOW | Missing docstring on a private helper | an `_`-prefixed function or method with no docstring |

## Notes

Unbounded retries have twice turned a downstream outage into a queue backlog; the MED pin keeps them from being deferred.
```

One finding traced through it: `code-review` labels a missing docstring on `_parse_row` as `must-fix`. The mapping row alone would grade it MED, but the LOW pin matches too and the pin wins (rule d), so `sy:gate` reports it under LOW. Nothing about it is fail-soft or a silenced check, so rule (a) does not apply and the lowering stands (rule c). Triage sets IMPACT `non-breaking`: it changes no behaviour and misleads no reader. A LOW, `non-breaking` accepted finding is deferrable, so on a terminal pass it is posted to the pull request as a deferred finding rather than fixed. Had the same label landed on an unbounded retry loop instead, the MED pin would make it `breaking` (rule b) and it would be fixed.

## What START checks

Run against the target branch's copy, with `PYTHONPATH=<plugin root> python -m sy_tools.overlay check <path>`:

1. The file exists. A missing file exits 2, which START reads as "no overlay", never as an error.
2. `## Scale mapping`, `## Pinned patterns` and `## Notes` are each present once, in that order.
3. `## Scale mapping` holds one table whose header is exactly `Source | Label | Grade`, and `## Pinned patterns` holds one table whose header is exactly `Grade | Pattern | Recognise by`.
4. Every `Grade` cell is `HIGH`, `MED`, `LOW` or `NIT`.

A failure exits 1 and prints one line per failed rule. Nothing else is checked: headings and lines inside fenced code blocks are ignored, and whether a row's content is sensible is left to the agents and to review of the file itself.
