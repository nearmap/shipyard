# Vision and goals

**One foundation, many paths, one shape for outputs.**

## Vision

Agents now write much of our code, and the share grows every month. This frees each of us for the work that matters most: deciding what to build and agreeing on it as a team. Every engineer now leads a team of agents. Shipyard helps each person lead that team well: scope the work, delegate it, and judge the result. When every ship produces consistent outputs, each PR is quick to trust, and review becomes a short, useful conversation.

**Fractal planning.** Large work is planned in layers. An objective becomes epics, an epic becomes tasks, and some tasks start as a spike or an MVP whose findings become new tasks. At the bottom, each task gets a spec that can be shipped. This is the skill that grows a senior into a principal, and a principal into a director. With a team of agents to lead, every engineer can practise it from their first year, so careers grow faster.

## Goals

Each goal has an ID. `/sy:retro` scores a person's sessions against them, one section per goal, and the interview asks why each pattern exists.

### Product lens: what the team gets

- **P1.** Help each engineer get good at fractal planning: split large work into epics, then tasks, then PR-sized pieces.
- **P2.** Make Shipyard useful to every person on the team, not only to its author.
- **P3.** Let each person keep their own way of working. Give every path the same outputs: spec, PR and escalation.
- **P4.** Ask people only for the decisions that need them: value judgements and real escalations. Agents answer factual questions.
- **P5.** Get from an agreed spec to a mergeable PR with as few human steps as possible.
- **P6.** Make review cheap: terse comments for agents, a short summary for people.

### Implementation lens: how Shipyard gets there

- **T1.** Agree decisions in the spec, before code exists, where review costs least.
- **T2.** Base each change to Shipyard on evidence from the whole team, not on the habits of one person.
- **T3.** Keep the foundation small: fewer words for agents to read, state kept in code, and defaults that each repo can change.

## How retro measures each goal

| Goal | Measured from |
| --- | --- |
| P1 | planning layer of each session; planning skill runs; merged PRs that had a plan |
| P2 | Shipyard use level; how often sessions reach their goal, with and without Shipyard |
| P3 | approaches and work types; which outputs each path produced |
| P4 | each human turn classed as value judgement, factual, approval, steering or rescue; questions and interrupts by skill |
| P5 | sessions, human turns, hours and failed CI commits per merged PR |
| P6 | review comments and rounds per merged PR; which standards rule each comment enforces |
| T1 | PRs reworked after review; sessions whose friction was a wrong approach or review rework |
| T2 | the retro itself: every discovery file adds one person's evidence |
| T3 | cost by token class, cache rewrites after idle gaps, subagent share, cost by skill and agent, peak context |
