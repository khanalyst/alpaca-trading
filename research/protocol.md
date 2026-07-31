# Research evidence protocol

Written before results are inspected, applied mechanically, and allowed to
return an answer nobody wants. Nothing in this protocol automatically promotes
an edge to live trading.

There are two verdict layers:

1. the current baseline-plus-one real-time experiment loop produces immutable
   `WORKED`, `FAILED`, or `INCONCLUSIVE` outcomes; and
2. the older multi-setting `forward-qualify` path produces `PROMOTE`, `REJECT`,
   `CONTINUE`, or `INSUFFICIENT_SAMPLE` as research qualification verdicts.

`PROMOTE` in the second layer means eligible for isolated local PAPER evidence,
not for an account or registry change.

At a few dozen round trips per variant, deciding after looking at the numbers
is p-hacking with extra steps. The only defence is a rule fixed in advance
that code applies mechanically — including on the fourth consecutive occasion
it says the sample is too small.


## Amendments

A rule fixed in advance can still be wrong, and the honest way to change one
is in public, before the evidence that would benefit from the change exists.
Every amendment is recorded here with its date and its reason. An amendment
made while a candidate is waiting on the criterion it changes is not an
amendment, it is a result being negotiated.

| Date | Amendment | Reason |
| --- | --- | --- |
| 2026-07-30 | **Criterion 10 added**: a Holm correction across every axis evaluated in one qualification run. **Criterion 5 reclassified** as a selection screen rather than an independent test. | The correction existed only for conditioning cells, so the path that actually qualifies an edge corrected nothing while this document claimed the corrected figure was the only quotable one. Five axes are registered and the count grows with the registry. No variant had forward evidence at the time, so nothing was pending on either criterion. |
| 2026-07-31 | **Real-time rotation outcomes documented separately.** Every strategy now keeps one baseline and at most one candidate; inadequacy is checked before performance and terminal outcomes are immutable. | The previous text said every setting ran continuously, which no longer matched the durable per-strategy rotation or the closed learning loop. |

---

## The verdicts

These are the `forward-qualify` research verdicts, not the terminal assignment
outcomes described in the next section.

| Verdict | Meaning |
| --- | --- |
| `PROMOTE` | Every criterion below holds. Eligible for a fresh isolated local paper portfolio; not for live capital. |
| `REJECT` | The whole axis underperforms, or the idea is structurally invalid. |
| `CONTINUE` | Neither. Keep collecting; the question is open. |
| `INSUFFICIENT_SAMPLE` | The sample cannot support any of the above. |

Every verdict names the single criterion that governed it. "Rejected" without
"because the whole axis's upper bound sat below the baseline" is not a
finding, it is an opinion with a timestamp.

## Real-time assignment outcomes

All seven strategies advance from the same cycle snapshot/timestamp. Each
strategy keeps its stable baseline and tests no more than one candidate at a
time. Strategies continue together while candidates rotate serially within
each strategy.

An assignment becomes terminal only after both configured collection floors
are met (three days and 100 comparable paired observations by default), or it
is explicitly rejected. The deterministic evaluator then checks evidence
adequacy before any performance claim:

- both arms contain decisions and at least 100 closed trades;
- no unresolved assignment-window opens or operational failures exist;
- one validated forward model and one experiment provenance apply per arm;
- full, 70/30 fit, and confirmation comparisons meet 100/70/30 pair floors,
  80% coverage, no duplicates, and at least 8 distinct six-hour episodes;
- two chronological segments can be formed.

Inadequate evidence is `INCONCLUSIVE`. Adequate evidence is then tested for
positive after-cost candidate PnL/expectancy, improvement over baseline,
drawdown no worse than baseline, positive and baseline-beating fit/confirmation
segments, intervals that establish the paired delta, and absence of revoked or
otherwise disqualifying variant/portfolio status.

- `WORKED` means all conservative gates passed.
- `FAILED` means adequate evidence failed a performance/disqualification gate,
  or the assignment itself was rejected.
- `INCONCLUSIVE` means adequacy or statistical confidence was not established.

Every outcome stores reasons and limitations. `WORKED` creates only an
`EDGE_CANDIDATE` with `authority: RESEARCH_ONLY` and
`promotion_allowed: false`. A separate LLM reviewer may explain the immutable
result and nominate one registered next setting; it cannot revise the verdict
or authorize execution.

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
4b. **Every window spans ≥ 8 distinct six-hour market episodes.** Trades
   inside one episode are close to one observation, and a single-cluster
   interval collapses to zero width — an absence of evidence that reads as
   certainty. See "Pairing and dependence".
5. **The paired fit-window delta interval is entirely positive.** Point
   estimates are not enough. **This is a selection screen, not a test.** The
   setting was chosen as the best of its axis on this same window, so the
   interval is inflated by that selection and cannot be quoted as evidence.
   It is here to stop a winner being carried forward on a point estimate;
   criterion 9 is what the claim rests on.
6. **Max drawdown ≤ the baseline's.** A better expectancy bought with a
   deeper hole is not an improvement.
7. **The setting survives the chronological 70/30 split.** One cutoff is
   derived from the baseline proposal calendar and applied to every arm.
   Selection occurs only before it; the later window cannot choose its winner.
8. **The confirmation window contains ≥ 30 resolved pairs**, with ≥ 80%
   coverage, no duplicate identities and the same registered dependence-aware
   bootstrap.
9. **The paired confirmation delta interval is entirely positive.** This is
   the criterion the claim rests on: the window is held out, it did not choose
   the winner, and it is the interval criterion 10 corrects.
10. **The confirmation result survives a Holm correction across every axis
   evaluated in the same qualification run.** Each axis is a separate chance
   to promote something, so a 5% test performed across five axes is not a 5%
   test. The family is every axis that produced a valid verdict in the run,
   including the axes that failed earlier — an axis that failed still consumed
   a chance, and dropping it would shrink the family exactly when a candidate
   needs the family small. `correct_axis_family()` records the family size, the
   adjusted figure and the axis list on every verdict, promoted or not, and
   `research.py forward-qualify` persists one `forward_axis_family` analysis
   per run so the count that governed the decision is auditable afterwards.
   A promotion that fails the correction becomes `CONTINUE`: the axis is not
   refuted, it has not cleared a bar that accounts for how many axes were
   asked the same question.

## Rejection — either

1. **≥ 3 settings including the baseline, and every alternative's CI upper bound is below the
   baseline's point estimate.** The whole axis has to be bad, not just the
   setting that happened to be tried first.
2. **Structurally invalid on inspection.** No sample required. The current
   implementation plan records the pre-registered falsifier and the reasoning
   as a finding row so the same idea does not return in three months.

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

Those immutable action records are the decision-ledger rows used by both the
terminal experiment outcome and the stricter forward-qualification path.

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

**Pairs are not the precision.** A clustered bootstrap's width is governed by
the number of independent episodes, not the number of trades inside them, so
every window additionally requires **≥ 8 distinct six-hour episodes**
(`MIN_BOOTSTRAP_CLUSTERS`). Without that floor the arithmetic inverts: pack a
hundred pairs into one afternoon and there is only one cluster to resample, so
every draw returns the same mean, the percentiles collapse onto the point
estimate, and a zero-width interval clears any "entirely positive" test. Ten
pairs spread over a month would report an honest interval and be refused while
the single afternoon promoted. The interval carries its own cluster count and a
collapsed one is rejected outright, never read as certainty.

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

Many hypotheses and conditioning cells against a limited sample create many
chances for something to look significant.

**The corrected figure is the only one any recommendation may quote.** The
uncorrected significance flag is deliberately not carried forward into any
output. Two corrections apply, at the two places a family exists, and both are
Holm–Bonferroni:


| Family | Applied by | Corrects |
| --- | --- | --- |
| Conditioning cells in one sweep | `correct_family()` | Several buckets of one axis tested against the same corpus |
| Axes in one qualification run | `correct_axis_family()` | Criterion 10. Several axes each given a chance to promote a winner |

Until 2026-07-30 only the first existed, which meant the sweep report was
corrected and the path that actually qualified an edge for paper was not. That
is the wrong way round: a conditioning cell produces a paragraph, an axis
produces a variant with its own paper account.

**Where the correction is *not* applied, and why.** Within an axis, the winner
is chosen as the best of its settings on the fit window. That selection is
handled by holding out the confirmation window rather than by correcting the
fit interval, because correcting a screen and then testing on held-out data
would charge the same multiplicity twice. Criterion 5 is therefore explicitly
labelled a screen.

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

## From collection to reviewed evidence

The current real-time path is baseline plus one candidate for each strategy,
not every setting at once. All seven strategy evaluators advance on every
available shared snapshot. The active momentum analyst is called at most once
per eligible decision cycle; non-active strategies use deterministic contracts
and no per-strategy LLM calls. Durable writes remain serialized.

Selections and adaptive values are first-class immutable identities. An
accepted selection waits behind the active assignment. Terminal assignment
evidence, outcomes, findings, review attempts, explanations, and any
research-only edge candidate remain append-only in schema 16.

The separate `research.py forward-qualify` path continues to apply the
multi-setting criteria above to complete prequalification decision-ledger
evidence. Schema migration 7 preserves legacy trades for audit but prevents
executed-trades-only populations from silently qualifying an edge. All arms
use one common evidence window; an operational failure invalidates it rather
than becoming missing data.

Forward qualification proves one strategy version and one declared axis,
verifies identical non-axis executable inputs, persists family correction, and
recomputes claims from embedded source evidence. A qualifying result can begin
a clean isolated local PAPER account only when flat.

PAPER success still does not edit the strategy registry. `research.py
t3-packet` builds an immutable SHA-256-addressed review bundle containing G2,
configuration/code/corpus provenance, held-out forward evidence, and the paper
result. Neither a packet nor any other research artifact automatically changes
the configured strategy, registry tier, account mode, risk, or capital.
