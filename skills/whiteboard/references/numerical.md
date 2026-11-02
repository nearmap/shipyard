# Numerical-science practice

Apply to any option that estimates, scores, smooths, classifies or detects something. The point is
to know what a method does to the data before trusting what it says about the world.

## Name the estimand

- What exactly is being estimated, for which unit (pixel, object, subject, event, region), at which
  time, and as of which date?
- What would the perfect answer be if the truth were known? A method that cannot say what it is
  estimating cannot be evaluated.
- Keep the observation, the model's reading of it, and the real state apart in the vocabulary.

## Bias, variance and the reference

- Is the estimator biased at the sample sizes we actually have? Short records, small groups and
  rare classes are where bias shows.
- Check any textbook reference value by simulating the actual code under the null. A formula for a
  slightly different estimator gives a confidently wrong reference.
- Report the spread, not only the centre. A difference smaller than the noise floor is not a finding.

## Simulate where the answer is known

- Generate data from the method's own assumed model, with known parameters, and check the method
  recovers them (parameter recovery). If it cannot recover its own world, it will not recover ours.
- Then break one assumption at a time (correlated errors, irregular timing, missing data, a class
  imbalance) and see how far the output moves. That shows which assumption matters.
- Simulation tests a mechanism. It never stands in for a real-data result.

## Uncertainty

- Give an interval for every headline number. Bootstrap by resampling whole clusters (subject,
  region, survey, period) when cases inside a cluster are correlated. Resampling rows when they are
  correlated gives intervals that are too narrow.
- Propagate uncertainty through a chain of steps, or say plainly that it was not propagated.
- A score called confidence or trust must come from the evidence, not from a hit rate against labels
  that do not exist.

## Parameters and sensitivity

- For each parameter: is it set by a physical rate, a stated reason, or fitting? Prefer the first two.
- Sweep each parameter across a plausible range and plot the trade-off it controls. A result that
  holds only at one setting is a fitted coincidence.
- Ask whether the parameters are identifiable from the data at all. Two parameters that trade off
  perfectly cannot both be learned.
- Would the setting hold on another region, period or dataset? Test on one held out.

## Selection effects and confounding

- Regression to the mean: cases picked because they look extreme will look less extreme next time
  with no change at all.
- Selection: examples chosen because they are easy to find (one region, one period, one source) do
  not represent the population. Say where they came from.
- Confounding: a difference between groups can come from a third factor (time, place, sensor, season).
  Compare matched controls before attributing an effect.
- Many comparisons: test enough slices and one will look significant by chance. Say how many were looked at.

## Sample size

- Before a probe, estimate whether the sample can detect the effect that would matter. If not, the
  probe can only rule out large effects, and the board says so.
- Rare failures (1 in 1,000 or rarer) need a full-population count, not a sample.

## Stability and determinism

- Same input, same output, on every run. Common sources of drift: thread counts, batch sizes, the
  order of sets and dictionaries, floating-point sums across groups in parallel, unseeded randomness.
  Name which apply to each option.
- Watch for loss of precision: subtracting near-equal numbers, summing many small values into a
  large one, low-precision types, probabilities that underflow (work in log space).
- Check behaviour at the edges: empty input, a single reading, all readings identical, extreme values.

## Testing algorithmic code

- The strongest test is an invariant checked on many random inputs from an unseeded generator, as
  long as the invariant is not sensitive to floating-point error. A fixed seed and a few examples
  do not prove an invariant.
- A runtime assert is the only true guarantee that an invariant held in production.
