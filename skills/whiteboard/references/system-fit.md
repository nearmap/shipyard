# Judging system and product fit

Use when an option changes how something is built, run or delivered, not only which algorithm it uses. Start from what already exists in the repository (owners, modules, deployment, tests, interfaces, the repo's own standards). Never invent a file, interface or behaviour. Read it.

## Requirements to pin down in the interview

Mark each one as known, assumed or unknown. An unknown requirement that decides between options becomes a question for the person who asked, not a guess.

- What the consumer needs, and what they will do with it.
- Correctness: what a wrong answer costs, and how rare a failure has to be to be acceptable.
- Scale: full population, not the sample. Rows, files, bytes, compute.
- Latency and freshness, if anyone waits for the result.
- Cost to run, and cost to recompute everything if it is wrong.
- Who maintains it after it ships.

## Reuse before building

Before any new component, take the first answer that fully works:

1. Does it need to exist at all? Name what is skipped in one line.
2. Does the codebase already have it (a helper, a pattern, a pipeline step)? Use it the way the surrounding code does.
3. Does the platform or standard library do it?
4. Does an installed dependency do it? Never add a dependency for a few lines.
5. Otherwise, the smallest new piece that works, with its check.

The smallest option is preferred only once everything it must reach (callers, consumers, tests, data contracts) is known.

## Size it before designing

Rough numbers at full scale, before the first data-flow drawing; they go on the board's arrow labels.

- Data volume: rows, files, bytes, now and per day.
- Request rate, and its peak.
- Latency budget, if anyone waits for the result.
- Storage and compute cost at full scale, and the cost of recomputing everything once.
- Growth: what the numbers are in a year, and which one breaks the design first.

## Questions for each design option

- **Boundaries.** Where does it sit? What is the thinnest interface it needs? What does it couple to?
- **Data contract.** What does it read and write? Which output becomes fixed once consumers depend on it?
- **Time and provenance.** Event time vs reading time vs processing time. Can it answer "as of" a past date? What happens to late or corrected inputs?
- **Failure.** Does a bad input stop it loudly, or flow through as plausible bad data? Is one failure contained, or does it take down the whole run?
- **Reruns.** Is it safe to rerun after a crash halfway? Is the output a pure function of the inputs?
- **Determinism.** Thread counts, batching, ordering of sets and dictionaries, floating-point sums across groups: which of these could change the output between runs?
- **State.** Does it add a database, cache or service? Writing code is usually cheaper than running a stateful system.
- **Change.** How is it rolled out, compared with the old behaviour, and rolled back?

## Operability

- How do we know it works in production: the metrics, the alerts, and how drift in the inputs or the output shows up.
- Who owns it and runs it, and what it costs to run.
- Security, privacy, retention and licensing of the data it reads and writes.

## Reversal cost

Every decision is cheap or costly to undo. A cheap one (a parameter, an internal function, a probe script) is settled fast and revisited when it bites. A costly one gets both lenses and a probe or a debate before it settles: a published schema, a stored format, a public API and a new service are costly, because something outside the change comes to depend on them.

## Data alignment checks (only when relevant)

- Units, scales, time zones and identifiers that differ between sources, the conversions between them, and the rule for a value that falls on a boundary.
- Misalignment between sources (in time, keys or granularity), and how it moves the result.
- Irregular sampling, gaps, censoring at the start or end of a record.
- Correlated inputs that look independent (shared source, shared collection run, reused upstream data).

## Product checks

- Does the output answer the consumer's question in their terms?
- Is it stable, so the same question gives the same answer unless something real changed?
- Can it be explained in one sentence?
- Mock the final answer with placeholder values and ask the consumer whether that is what they need. Do it in the interview, before measuring anything.
