# Challenge checklist

Run it against every surviving option at each checkpoint, and against Claude's own claims before
they reach the board. Report what failed. Do not wait for the user to ask. Skip a question
only when it plainly does not apply.

Every failure reported needs a concrete case: this input or situation leads to this wrong result.
No case, no finding.

## Data

- Is this real data from the actual source, at the version or run the claim names?
- Is the sample biased by place, time, segment, device or source? Where did the examples come from?
- Could the thing that matters be rare (1 in 1,000 or rarer)? Has a full-population count been run
  for it, or only a sample?
- Do labels exist? If not, what stands in for truth, and what can that stand-in not tell us?
- Could the evaluation leak: near-duplicates or neighbours in time across the split, the same subject
  on both sides of it, the answer encoded in an input?
- Was the result computed on synthetic data, and is that said everywhere it is quoted?

## Method

- What does the method assume about how the data was made? Which plot or count would show the
  assumption is false? Has anyone looked?
- Is each parameter justified by a known rate or a stated reason, or tuned to this sample?
  Would it hold on another segment, period or dataset?
- Does it give the same output on every run with the same input?
- Does it beat the simplest baseline, measured in both directions?
- Does it win on average but lose on a slice that matters?
- Does it need rules bolted on afterwards to behave?
- Do any of its outputs contradict each other?

## System

- What does it cost, in compute, storage and time, at full scale rather than on the sample?
- When it goes wrong, does it fail loudly or write plausible bad data quietly?
- Can it rerun safely after a crash halfway through?
- Which data interface becomes fixed once it ships?
- What new dependency, service or state does it add, and who maintains it?

## Product

- Does it answer the question the consumer actually asked, or the one we find interesting?
- What will the consumer do with the output? Does the output support that action?
- Is the output stable over time, so an answer does not change without a real reason?
- Can it be explained to the consumer in one sentence?
- One output or several? Does each extra field earn its place?

## Decision

- What is the strongest argument against the leading option? Who would make it?
- Pre-mortem: it is six months later and this failed. What is the most likely reason?
- What result would change our mind? Is there a probe that could produce it?
- How hard is it to reverse once shipped?
