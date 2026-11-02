# Board shape

## Lanes and note kinds

The board is what `board_write` renders: lanes (frames) left to right, notes inside them, links between notes. Start every board with these lanes, adding one only when the problem needs it:

| Lane id | Name | Holds |
|---|---|---|
| `now` | Where we are | one `status` note: leading option, strongest open objection, next step |
| `frontier` | Open questions | one `decision` note per open decision (`D1 ...`), linked to the decisions it depends on |
| `alive` | Alive | live options, ranked top to bottom, with their `objection` notes beside them |
| `parked` | Parked | options set aside for a stated reason, and ideas for other problems |
| `out` | Ruled out | options with their one-line reason, tier and what would reopen them |
| `probes` | Probes | one `probe` note per probe: prediction, then result |
| `decided` | Decided | settled decisions, and at convergence the Decision |

Kinds set a note's colour: `status`, `option`, `decision`, `hypothesis`, `probe`, `objection`. A link is an arrow: `D1 -> D2` when D2 depends on D1, `objection -> option` when an objection targets an option.

Keep notes short: a title line, then at most three short lines. The detail is in `notes.md`; the board is the map of it. A note's id never changes once written (`opt-<slug>`, `D<n>`, `P<n>`, `H<n>`), because the id is how a user's move is traced back.

The spec lists every lane and note Claude wants as it stands, so it is rebuilt from `notes.md` each write. A spec that drops a note leaves it on the board untouched; moving it to `out` is how an option leaves.

## Opening the board

Tell the user once, at the first write, with the path `board_write` returned:

> The board is `<path>/board.excalidraw`. Open it in VS Code with the Excalidraw extension (`pomdtr.excalidraw-editor`), which edits the file in place, or at excalidraw.com through Open, where a Chromium browser saves back to the same file. Drag notes between lanes, edit their text, add notes or arrows; I read your changes at the start of every turn.

excalidraw.com keeps the scene in the browser unless the user starts a live collaboration or a share link; say so when the problem is sensitive. Any other Excalidraw editor works the same way.

## `notes.md` template

The full record. The board is built from it, and a new session resumes from it.

```markdown
# <problem name>

Board: <path to board.excalidraw>
Kind of work: <root cause | method choice | system design | feasibility | measurement design>

## Coverage
| Area | Status | Notes |
|---|---|---|
| repo docs and rules | read / excluded | <which, or why excluded> |
| earlier work and memory | read / excluded | ... |
| code path | read / excluded | ... |
| data sources | read / excluded | <ids, sample size, population counts> |
| what the askers said | read / excluded | <where> |

## Where we are
Leading option: <name, or "none yet">
Strongest open objection: <one line>
Next step: <one line>

## Aim
- v1 (<date>): <one sentence>. Consumer: <who>. They will use it to: <action>.
- v2 (<date>): <one sentence>. Changed because: <reason>.

## Frame
- Estimate: <what, at which unit>
- Truth: <source, or the stand-in and what it cannot tell us>
- Hard constraints: <list>
- Non-goals: <list>
- Success: <result that ends the work positively>
- Stop: <result that ends it negatively>

## Glossary
- <term>: <the one meaning used on this board>
- `<data source id>`: <what it is, environment, version or date>

## Theory and prior
- Known: <mechanism or result, and its source>
- Assumptions: <what each candidate takes as given>
- Plausible approaches: <families the theory points to>
- Expected failure modes: <approach: where theory says it breaks>

## Hypotheses
| Id | Hypothesis | Test | Disproved if | Status |
|---|---|---|---|---|
| H1 | ... | ... | ... | open / supported / disproved / strained by early look |

## Decision tree
| Id | Decision | Depends on | Status | Recommended | Settled as (by whom, date) |
|---|---|---|---|---|---|
| D1 | ... | - | open / waiting on lookup / settled | ... | |

Frontier: <open decisions whose dependencies are all settled>

## What we know
| Claim | Tier | Source |
|---|---|---|
| ... | checked / documented / inferred / proposed / unknown (+ ladder rung for a causal claim) | path:line, run id, doc |

## Idea round
- Debate: <date>, outcome in one line.
| Idea | Verdict | Reason | Tier | Reopen if |
|---|---|---|---|---|

## Options
### <option> (alive | parked | ruled out)
- Intuition: <one sentence>
- Assumes: <about the state, the observations, the system>
- Would break if: <the plot or count that would show it>
- Cost: <build and run, rough>
- For: <strongest point>. Against: <strongest point>.
- Ruled out because: <one line, tier>. Reopen if: <condition>.

## Probes
### P<n> <name> (planned | running | done)
- Tests: <hypothesis ids>. Baseline: <simplest comparison>.
- Prediction (before running): <who wins and why>
- Data: <real source, ids, sample and population>
- Result: <both directions, with uncertainty, by slice>. Matched prediction: yes / no.
- If no: <formulation, data, model assumption or evaluation design, and what changed>

## Disagreements
- <topic>: <user's position> vs <Claude's position>. Settled by: <probe | user's call | Claude's call>, <date>.

## Parked
- <ideas for other problems>

## Decision
- Decision: <what>. Why: <reasons, linked to probes and evidence>.
- Rejected: <option, reason>
- Residual risks: <list>. Revisit if: <result or event>.
- Agreed by the user: <their words, date>. Dissent on record: <if any>.
- Hand-off: <none | /sy:spike | /sy:plan | /sy:spec>

## Log
- <date>: <what changed and why>
```
