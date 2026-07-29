# Promotion protocol

Written before results are inspected, applied by `protocol.py`, and allowed
to return an answer nobody wants.

At a few dozen round trips per variant, deciding after looking at the numbers
is p-hacking with extra steps. The only defence is a rule fixed in advance
that code applies mechanically — including on the fourth consecutive occasion
it says the sample is too small.

---

## The verdicts

| Verdict | Meaning |
| --- | --- |
| `PROMOTE` | Every criterion below holds. Eligible for demo trading. |
| `REJECT` | The whole axis underperforms, or the idea is structurally invalid. |
| `CONTINUE` | Neither. Keep collecting; the question is open. |
| `INSUFFICIENT_SAMPLE` | The sample cannot support any of the above. |

Every verdict names the single criterion that governed it. "Rejected" without
"because the whole axis's upper bound sat below the baseline" is not a
finding, it is an opinion with a timestamp.

## Promotion — all five must hold

1. **≥ 100 matched round trips** on the best setting.
2. **≥ 3 settings tested along the axis.** This is the rule that stops a good
   idea being killed by one badly chosen parameter value.
3. **Expectancy CI lower bound > the baseline's point estimate.** Not the
   point estimates compared — the interval must clear the baseline outright.
4. **Max drawdown ≤ the baseline's.** A better expectancy bought with a
   deeper hole is not an improvement.
5. **Survives the out-of-sample split.** Fit on the first 70% of the corpus
   by time, confirm on the last 30%.

## Rejection — either

1. **≥ 3 settings tested, and every setting's CI upper bound is below the
   baseline's point estimate.** The whole axis has to be bad, not just the
   setting that happened to be tried first.
2. **Structurally invalid on inspection.** No sample required. `findings.md`
   §6 rejected three hypotheses this way, and the reasoning is recorded as a
   finding row so the same idea does not return in three months.

Everything else is `CONTINUE`.

## The out-of-sample split

**By time, never at random.** A random split lets the same market episode
appear on both sides, so a variant fitted to one afternoon's regime confirms
on that same afternoon and looks robust.

**"Survives" is a deliberately weak bar**: the confirm window's interval must
not lie entirely below the fit window's point estimate. That is, the confirm
window must not be evidence *against* the fit window's claim. A strict bar
would reject everything at these sample sizes, and a bar nothing can pass
carries no information.

**The regime profile of both windows is always reported alongside the
result.** From the risk register: a corpus spanning a volatility regime
change turns the split into a regime test rather than a robustness test, and
those have opposite meanings — one says the variant generalises, the other
says the market changed. The pass/fail alone cannot distinguish them, so the
median realised-vol ratio of each window is quoted, and a spread beyond 1.5×
between the windows marks the comparison as not comparable.

## Multiple comparisons

Six hypotheses, several conditioning cells each, against a few hundred round
trips. Something will look significant.

`correct_family()` applies a Holm–Bonferroni correction across each batch,
and **the corrected figure is the only one any recommendation may quote.**
The uncorrected significance flag is deliberately not carried forward into
the output.

The p-values are approximated from each bucket's bootstrap interval. That is
coarse, and it is the honest resolution available at this sample size — it is
used to rank and to correct, and never quoted as a p-value in its own right.

## What `INSUFFICIENT_SAMPLE` means, and what it does not

It means the question is **open**. It does not mean the hypothesis failed.

This distinction is the one most likely to erode in practice. Four
consecutive `INSUFFICIENT_SAMPLE` verdicts feel like a broken harness, and
the natural response is to relax the threshold "just to see" — which is the
exact moment the threshold exists for.

Two things make that pressure survivable, and both are already built:

- `research.py sweep` **forecasts achievable n before running** and refuses a
  grid it cannot power, so the verdict is stated once rather than once per
  grid point.
- **Conditioning axes are preferred over parameter axes**, because they
  partition trades that already exist instead of dividing an already-small
  sample further.

A null result is a row in the findings store with the same weight as a
positive one. A programme that records only what worked records only noise.
