# The decision page

`page_render` turns `notes.md` into `page.html` beside it, one collapsible section per `## ` heading. Raw HTML in `notes.md` shows as text.

## Opening the page

Tell the user once, at Open, with the `url`, `port` and `forward` that `page_serve` returned:

> The decision page is at `<url>`. Open it in a browser. It refreshes itself within a second of each of my updates. Without the server, open `file://<page path>`. As a last fallback, open `notes.md` in VS Code's Markdown preview (Ctrl/Cmd+Shift+V): same content, without the collapsing sections.
>
> If Claude runs on a remote machine, forward the port first. One forward covers both the page and the board (same port):
>
> - On your own machine, in a terminal you leave open, run `<forward>` with the host swapped for the name you normally type after `ssh`. The host in the line is the remote's own hostname, which can be a container name your machine cannot reach. `-N` prints nothing while it works; Ctrl+C stops it.
> - Or, in VS Code Remote-SSH, open the Ports tab, choose Forward a Port, and enter `<port>`.
> - Or add `LocalForward <port> 127.0.0.1:<port>` to that host's entry in your `~/.ssh/config`, so every ssh session forwards it.
> - If `127.0.0.1` does not open on your machine but the port is forwarded, use `localhost` in the URL instead (same path). Something else on your machine holds that port on IPv4, so the forward listens on IPv6 only. Saves still work: the server accepts both names.

When the page server stops, the open page shows a banner: call `page_serve` again.

## The hosted page

Only when the user picked it on the Hosted choice point.

- First publish: the Artifact tool with `page.html` and these capabilities:

```json
{"db": {"rules": [
  {"path": "", "read": "view", "write": "owner"},
  {"path": "comments", "read": "view", "write": "owner"},
  {"path": "comments/{self}", "read": "view", "write": "interact"}
]}, "user": {"scopes": ["profile"]}}
```

- Record the URL in the `Hosted page:` line of `notes.md`.
- Tell the user to share it at Contributor level or above with anyone who should comment.
- Run one sync right after the first publish. It checks that the capabilities took.
- Later publishes omit `capabilities`.
- Probe figures show only on the local page.

Each colleague's comment is one document at `comments/<their id>/items/<id>`, with a marker document `comments/<their id>`.

## Syncing comments

1. If `<folder>/.comments-in` is left from an aborted sync, remove that exact path with Bash `rm -rf`.
2. ArtifactData `list` of collection `comments`, `out_dir` `<folder>/.comments-in`, `query.limit` 1000.
3. For each marker file `comments/<id>.json` it wrote, ArtifactData `list` of collection `comments/<id>/items` into the same `out_dir`. Follow `next_cursor` until it is empty.
4. `page_comments_save`. It writes `comments.json`, removes the staging folder and returns `pending`.
5. Answer the pending comments per SKILL.md § Colleague comments, adding each row to the `## Comments` table.

## Session output

| File | Written by | Shape |
|---|---|---|
| `decisions.json` | `page_render` | `{"rev": "<16 hex>", "tree": [{"<header>": "<cell text>"}], "decision": ["<item text>"]}`: one object per Decision tree row, one string per top-level Decision item |
| `comments.json` | `page_comments_save` | `[{"section", "decision", "option", "text", "at", "rev", "id", "author"}]`, sorted by `(at, author, id)` |

## `notes.md` template

Sections in page order: what the user reads first comes first. Keep each section's heading as written, so links into the page (`page.html#decision-tree`) keep working.

```markdown
# <problem name>

Board: <folder path>
Hosted page: <url | none>
Kind of work: <root cause | method choice | system design | feasibility | measurement design>
Mode: <page first | page and board | board only | page only>. Start: <explore | straight to the options | decide now>

## Where we are
- Leading option: <name, or "none yet">
- Strongest open objection: <one line>
- Next step: <one line>

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

| Option | Intuition | Status | Rests on (assumes) | Would break if | Cost | For | Against | Ruled out because (tier); reopen if |
|---|---|---|---|---|---|---|---|---|
| <name> | <one sentence> | idea / alive / parked / ruled out | <assumptions about state and observations; hypothesis ids> | <the plot or count that would show it> | <build and run, rough> | <strongest point> | <strongest point> | <one line> |

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
- Result: <one line, both directions>. Matched prediction: yes / no.

| Measure | Estimate | Interval | n |
|---|---|---|---|
| <what, by slice> | ... | ... | ... |

![<what the figure shows>](probes/p<n>.png)
- If no: <formulation, data, model assumption or evaluation design, and what changed>

## Design
- Sizing: <data volume, request rate, latency budget, storage and compute cost at full scale, growth>
- Board sections: <the sections drawn, or "page only">
- <in page-only mode: the data model, data flow and system design as text and tables>

## Disagreements
- <topic>: <user's position> vs <Claude's position>. Settled by: <probe | user's call | Claude's call>, <date>.

## Comments
| Comment | Where | Gist | Disposition |
|---|---|---|---|
| `<author>/<id>`, exactly as in comments.json | <section, decision or option> | <one line> | adopted: <what changed> / put to the user as D<n> / noted |

Ids only, no colleague names. `page_comments_save` reads the first column.

## Decision
- Decision: <what>. Why: <reasons, linked to probes and evidence>.
- Done criterion: <how the decision meets the interview's Done when, or what it leaves short>
- Rejected: <option, reason>
- Pre-mortem: <each reason it could fail in six months, and the residual risk or probe it became>
- Residual risks: <list>. Revisit if: <result or event>.
- Delivery shape: <smallest end-to-end version; milestones; acceptance checks from the done criterion>
- Agreed by the user: <their words, date>. Dissent on record: <if any>.
- Hand-off: <none | /sy:spike | /sy:plan | /sy:spec | /sy:mvp>

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
