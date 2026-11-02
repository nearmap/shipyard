# Judging whether a method fits the problem

Every method is a story about how the data was made. A method fits when its story matches the real one. Judge in this order, because the early steps are cheap and rule out most candidates.

## 1. Write down how the data is actually made

Before looking at any method, describe two processes in plain words.

- **The real thing (the state).** Is it a category, an ordered category, a count, a continuous value, a shape, an event? How does it change: never, slowly, suddenly, at what rate? Can it be several things at once?
- **How we observe it (the observations).** Which instrument, system or model produces the readings? How noisy are they? Are the errors independent, or shared across readings (same source, same collection run, same time window, reused inputs)? Are readings taken at regular or irregular intervals? What is missing, and is it missing at random? Is there any ground truth?

Keep the observation separate from the thing observed. A reading is not the truth, and the time of a reading is not the time of an event.

## 2. Read each candidate's assumptions against that

For each candidate give: the intuition in one sentence, what it assumes about the state and the observations, and where those assumptions clash with step 1. Consider at least:

- no change to today's behaviour;
- the simplest rule or heuristic;
- a classical statistical or numerical method;
- a probabilistic model of the state and the observations;
- a learned model;
- reusing something that already exists.

A learned model needs labels. With no labels it is out, unless the labels can be created.

Most candidates fail here in one line. That is the point of the step.

## 3. Name the plot that would break each assumption

For every assumption the leading options rest on, name the cheapest plot or count that would show it is false, then look. Typical ones:

- distribution of gaps between readings (does irregular timing matter?);
- disagreement between neighbouring readings against the gap between them (flat at short gaps means independent noise; lower at short gaps means shared errors);
- results split by source, segment or period (hidden bias);
- how often the input is split between classes (are the classes really exclusive?);
- residuals against time, order or an input the model ignores (structure the model missed).

Check a textbook formula against a simulation of the actual code before relying on it. A formula for a slightly different estimator gives a confidently wrong reference.

## 4. Check the constraints

- Labels: needed, available?
- Determinism: same input, same output, on every run?
- Time: does it need the future to answer about the past? Can it answer "as of date X"?
- Scale: cost per subject times the full population.
- Parameters: each one justified by a rate or a reason, and stable across datasets?
- Explanation: can the consumer be told why an output is what it is?

## 5. Beat the simplest baseline, in both directions

Pick the two quantities that pull against each other (false alarms vs missed changes, precision vs recall, stability vs speed of response) and measure both against the simplest baseline. A method earns its complexity only if it moves the trade-off, not just one side of it.

## Metrics by output type

Pick the metric from the type of output, then state its assumptions.

- **Category:** confusion matrix, per-class precision and recall.
- **Several labels at once:** per-label precision, recall and F1, plus coverage.
- **Ordered category:** weighted kappa or rank error, and say why the spacing between levels is treated the way it is.
- **Probability:** log loss or Brier score, calibration and sharpness, reported separately from any decision threshold.
- **Continuous value:** MAE, RMSE, quantile errors.
- **Overlap (a span, a set, an extent):** IoU or Dice, boundary error, with alignment between sources controlled.
- **Event or change:** event precision and recall at a stated time tolerance, plus detection delay.
- **Ranking:** average precision or NDCG, only when ranking is the real task.

Give uncertainty with a bootstrap that resamples whole clusters (subject, group, batch) where cases are correlated. Split by group or time where near-duplicates would leak.

## With no ground truth

Say plainly what cannot be claimed. Agreement between methods, stability over time and posterior checks are diagnostics. They show consistency, not accuracy. Useful stand-ins:

- held-out sources or periods the method never saw;
- pairs that should agree (same subject, same moment, different source) as a noise floor;
- blind review of real examples by someone who does not know which method produced which;
- known-change cases, from an independent source, to test response.

## Evidence ladder for causal claims

Every claim that X causes Y says which rung it has reached. Lower rungs are leads, not conclusions.

1. **Pattern seen:** visible in a few examples.
2. **Association:** X and Y move together across the data.
3. **Controlled association:** they still move together with the obvious confounders held fixed (time, segment, source, matched controls).
4. **Intervention:** changing X alone, with everything else fixed, changes Y.
5. **Replicated intervention:** the intervention holds on new data (another segment, period or run).

When two causes always change together in the data (one input source always handled by one version of the code), no amount of rung 2 or 3 evidence separates them. Name the intervention that would.

One case, or one pair of runs, is an observation. Generalise only after contrasting cases.

## Signs of a bad fit

- Results swing on one parameter.
- It needs rules bolted on afterwards.
- It wins on average and loses on a slice that matters.
- Its outputs contradict each other.
- Its confidence is always maximal.
- Its parameters have to be refitted for each new dataset.
