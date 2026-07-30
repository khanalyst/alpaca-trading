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
| `PROMOTE` | Every criterion below holds. Eligible for a fresh isolated local paper portfolio; not for live capital. |
| `REJECT` | The whole axis underperforms, or the idea is structurally invalid. |
| `CONTINUE` | Neither. Keep collecting; the question is open. |
| `INSUFFICIENT_SAMPLE` | The sample cannot support any of the above. |

Every verdict names the single criterion that governed it. "Rejected" without
"because the whole axis's upper bound sat below the baseline" is not a
finding, it is an opinion with a timestamp.

## Promotion — every criterion must hold

1. **At least three settings including the explicit baseline.** This is the
   rule that stops a good idea being killed by one badly chosen value.
2. **≥ 100 resolved proposal pairs** for the fit-selected setting and baseline.
3. **≥ 80% pair coverage** of the proposal union, with no duplicate proposal
   identities. Missing and unresolved proposals are reported, never dropped
   silently.
4. **The fit window contains ≥ 70 resolved pairs**, with ≥ 80% coverage, no
   duplicate identities and the registered six-hour paired cluster/block
   bootstrap. These are separate requirements from the full-sample floor.
5. **The paired fit-window delta interval is entirely positive.** Point
   estimates are not enough.
6. **Max drawdown ≤ the baseline's.** A better expectancy bought with a
   deeper hole is not an improvement.
7. **The setting survives the chronological 70/30 split.** One cutoff is
   derived from the baseline proposal calendar and applied to every arm.
   Selection occurs only before it; the later window cannot choose its winner.
8. **The confirmation window contains ≥ 30 resolved pairs**, with ≥ 80%
   coverage, no duplicate identities and the same registered dependence-aware
   bootstrap.
9. **The paired confirmation delta interval is entirely positive.**

## Rejection — either

1. **≥ 3 settings including the baseline, and every alternative's CI upper bound is below the
   baseline's point estimate.** The whole axis has to be bad, not just the
   setting that happened to be tried first.
2. **Structurally invalid on inspection.** No sample required. `findings.md`
   §6 rejected three hypotheses this way, and the reasoning is recorded as a
   finding row so the same idea does not return in three months.

Everything else is `CONTINUE`.

## Pairing and dependence

Every local shadow/paper proposal has a stable identity derived from symbol,
completed signal timestamp, direction and setup type. Variant names and wall
clock are deliberately excluded, so the same market opportunity can be
matched across parameter values after restarts.

The store records one immutable action row for every arm and proposal, not
only for proposals that opened a trade. An accepted action joins to its paper
trade and inherits the resolved R multiple; a veto is paired as an explicit
0R action. This is required for policy axes such as confidence floors,
exposure caps and discriminators: the hypothesis is about the value of trades
one arm admits while another suppresses. Treating that veto as a missing row
would make those axes incapable of demonstrating an edge.

Inference uses the return difference inside each exact pair. The bootstrap
keeps observations together in six-hour market clusters and samples
contiguous cluster blocks, preserving cross-symbol market episodes and
short-run serial dependence. The output always carries paired count, proposal
union count, coverage, left/right-only proposals, unresolved outcomes,
duplicate identities and examples of mismatches.

The minimums are fixed constants in `protocol.py`: 100 full pairs, 70 fit
pairs, 30 confirmation pairs and 80% coverage in every relevant population.
The natural 70/30 allocation of the 100-pair promotion floor therefore cannot
be satisfied by accumulating nearly all evidence on only one side of the
chronological cutoff.

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

## From edge to paper to reviewed evidence

Every preregistered parameter value runs continuously in its own persisted
`SHADOW` account. Every account advances on each available real-time snapshot,
including pause/day-stop/cadence/LLM-failure paths. The cycle's one recorded
LLM proposal set and confidences are reused by every arm; no extra LLM call is
made. The shipped budget is unlimited (`0`). If an operator sets a positive
budget, a durable least-observed-first scheduler skips only whole variants.
Axes are grouped by their
override path, so reward/risk, stop width, confidence, exposure and breakout
classification can never be pooled into one winner search.

Forward evidence starts only after every arm has the complete decision-ledger
watermark. Schema migration 7 preserves legacy trades for audit but revokes
qualifications based on the older executed-trades-only population, because
historical vetoes cannot be reconstructed without inventing data.
All arms use the same evidence window: the latest complete-ledger watermark
through the earliest qualification or paper-start boundary. Decisions after
that boundary cannot change the qualifying corpus. A recorded operational
failure inside the window invalidates the analysis rather than becoming an
unlabelled missing observation.

`research.py forward-qualify` applies this protocol to prequalification
real-time outcomes. It queries the exact decision-ledger rows, joins accepted
actions to their resulting trades, enforces homogeneous strategy
model/assumptions/provenance, embeds and hashes that source corpus,
proves the baseline and candidate definitions describe one strategy version
and exactly one declared axis, verifies all non-axis executable config is
identical, and recomputes the protocol before qualification. Generic analysis
insertion cannot create forward evidence. A winner becomes `PAPER_PENDING`
while any
shadow position is open, then starts a clean rebased `PAPER` account when flat. Shadow history
stays stored but is excluded from paper-only metrics.

Paper success does not edit the strategy registry. `research.py t3-packet`
builds an immutable SHA-256-addressed packet containing current G2,
configuration/code/corpus provenance, held-out forward evidence and the
postqualification paper result. It remains `DRAFT_REVIEW_REQUIRED` unless the
full checklist passes and a reviewer plus registry-change reference are
supplied. Live authority is always a deliberate reviewed code change.
