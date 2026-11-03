# The decision page

`page_render` turns `notes.md` into `page.html` beside it, one collapsible section per `## ` heading. Raw HTML in `notes.md` shows as text.

## Opening the page

Tell the user once, at Open, with the `page` path `page_render` returned:

> The decision page is `<path>/page.html`. Open it in a browser (`file://<path>/page.html`); it refreshes itself within a second of each of my updates. On a remote machine, open it from VS Code with the Live Preview extension (`ms-vscode.live-server`), which serves it over the forwarded port. If neither works, open `notes.md` in VS Code's Markdown preview (Ctrl/Cmd+Shift+V): same content, without the collapsing sections.

## `notes.md` template

Sections in page order: what the user reads first comes first. Keep each section's heading as written, so links into the page (`page.html#decision-tree`) keep working.

```markdown
# <problem name>

Board: <folder path>
Kind of work: <root cause | method choice | system design | feasibility | measurement design>
Mode: <page first | page and board | board only | page only>. Start: <explore | straight to the options | decide now>

## Where we are
Leading option: <name, or "none yet">
Strongest open objection: <one line>
Next step: <one line>

## Aim and interview
- Aim v1 (<date>): <one sentence>. Consumer: <who>. They will use it to: <action>.
- Aim v2 (<date>): <one sentence>. Changed because: <reason>.
- In scope: <list>. Out of scope: <list>
- Requirements: <list, each known / assumed / unknown>
- Hard constraints: <list>
- Resources: people <who, how much time>; time <deadline>; compute <what>; data <what, access>; budget <amount>
- Estimate: <what, at which unit>
- Truth: <source, or the stand-in and what it cannot tell us>
- Done when: <the criterion the Decision and the hand-off brief are judged against>
- Success: <result that ends the work positively>. Stop: <result that ends it negatively>
- Delivery: <what is handed over, to whom, in what form>
- Confirmed by the user: <their words, date>

## Decision tree
| Id | Decision | Depends on | Reversal cost | Status | Recommended | Settled as (by whom, date) |
|---|---|---|---|---|---|---|
| D1 | ... | - | cheap / costly: <why> | open / waiting on lookup / settled | ... | |

Frontier: <open decisions whose dependencies are all settled>

## Options
- Idea round: <date>, debate outcome in one line.

### <option> (idea | alive | parked | ruled out)
- Intuition: <one sentence>. Rests on: <hypothesis ids>
- Assumes: <about the state, the observations, the system>
- Would break if: <the plot or count that would show it>
- Cost: <build and run, rough>
- For: <strongest point>. Against: <strongest point>.
- Ruled out because: <one line, tier>. Reopen if: <condition>.

## Theory, hypotheses and assumptions
- Known: <mechanism or result, and its source>
- Plausible approaches: <families the theory points to>
- Expected failure modes: <approach: where theory says it breaks>

| Id | Hypothesis | Test | Disproved if | Status |
|---|---|---|---|---|
| H1 | ... | ... | ... | open / supported / disproved / strained by early look |

| Assumption | Evidence | Who checks it | If wrong |
|---|---|---|---|
| ... | checked / documented / inferred / none | <Claude, the user, a probe> | <what breaks> |

## What we know
| Claim | Tier | Source |
|---|---|---|
| ... | checked / documented / inferred / proposed / unknown (+ ladder rung for a causal claim) | path:line, run id, doc |

## Probes
### P<n> <name> (planned | running | done)
- Tests: <hypothesis ids>. Baseline: <simplest comparison>.
- Prediction (before running): <who wins and why>
- Data: <real source, ids, sample and population>
- Result: <both directions, with uncertainty, by slice>. Matched prediction: yes / no.
- If no: <formulation, data, model assumption or evaluation design, and what changed>

## Design
- Sizing: <data volume, request rate, latency budget, storage and compute cost at full scale, growth>
- Board sections: <the sections drawn, or "page only">
- <in page-only mode: the data model, data flow and system design as text and tables>

## Disagreements
- <topic>: <user's position> vs <Claude's position>. Settled by: <probe | user's call | Claude's call>, <date>.

## Decision
- Decision: <what>. Why: <reasons, linked to probes and evidence>.
- Done criterion: <how the decision meets the interview's Done when, or what it leaves short>
- Rejected: <option, reason>
- Pre-mortem: <each reason it could fail in six months, and the residual risk or probe it became>
- Residual risks: <list>. Revisit if: <result or event>.
- Delivery shape: <smallest end-to-end version; milestones; acceptance checks from the done criterion>
- Agreed by the user: <their words, date>. Dissent on record: <if any>.
- Hand-off: <none | /sy:spike | /sy:plan | /sy:spec>

## Coverage and glossary
| Area | Status | Notes |
|---|---|---|
| repo docs and rules | read / excluded | <which, or why excluded> |
| earlier work and memory | read / excluded | ... |
| code path | read / excluded | ... |
| data sources | read / excluded | <ids, sample size, population counts> |
| what the askers said | read / excluded | <where> |

- <term>: <the one meaning used in this record>
- `<data source id>`: <what it is, environment, version or date>

## Log
- <date>: <what changed and why>
```
