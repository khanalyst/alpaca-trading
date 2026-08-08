# Research evidence protocol

Written before results are inspected, applied mechanically, and allowed to
return an answer nobody wants. Nothing in this protocol automatically promotes
an edge to live trading.

There are two verdict layers:

1. the current baseline-plus-bounded-batch real-time experiment loop produces immutable
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
| 2026-07-31 | **Real-time rotation outcomes documented separately.** The initial durable design paired a stable baseline with a serial candidate assignment; inadequacy was checked before performance and terminal outcomes became immutable. | The previous text said every setting ran continuously. The 2026-08-06 amendment below superseded serial assignment with bounded batches. |
| 2026-07-31 | **Forward qualification now reconstructs every setting from all eligible completed assignment attempts and its own contemporaneous baseline. Axis correction uses a calibrated paired six-hour-cluster sign-flip p-value.** | A common latest-ledger watermark erased earlier serial settings, and interval geometry is not a p-value. Existing v2 analyses remain readable but cannot authorize a new qualification. |
| 2026-08-06 | **Real-time rotation now runs one shared baseline with a bounded pre-registered candidate batch per strategy (four shipped, hard cap eight); each assignment still tests one setting.** | Priority 1 increases discovery throughput without mixing adaptive selections into a static family or changing the downstream family-correction and held-out gates. |

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

The four realtime strategies advance from the same cycle snapshot/timestamp.
Each keeps its stable baseline and tests a bounded batch of pre-registered
candidates in parallel; every individual assignment still tests no more than
one candidate setting. The active analyst's adaptive selections remain
isolated from the static batch.
The registered `funding-carry`, `funding-unwind`, and `trend-multiday` models
are offline-only; their candidates do not occupy realtime lanes.

The configured `ls_ratio_fade.tuned_70_30_ext_1_5_stop_1_target_3` contract is
not part of that adaptive one-axis candidate set. It is a pinned isolated paper
arm with its own account; the coupled 70/30, 1.5-ATR, 1-ATR, 3R identity is
never decomposed into selector assignments.

An assignment becomes terminal only after both configured collection floors
are met (ten elapsed days and 100 comparable paired observations by default), or it
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

Inference eligibility is fail-closed before any of those statistics run. Each
row must match the canonical `StrategyContract` variant and semantic hash; old,
missing, or mismatched contract provenance is retained but quarantined. A
closed outcome must also have funding status `verified_realized` or
`verified_no_settlement_due`. Legacy, partial, forecast-only, or otherwise
unverified funding never becomes a zero-cost sample.

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
7. **The setting survives the chronological 70/30 split.** Each serial setting
   uses the proposal calendar of its own assignment-matched baseline. Selection
   compares paired fit-window deltas only; the later window cannot choose its
   winner.
8. **The confirmation window contains ≥ 30 resolved pairs**, with ≥ 80%
   coverage, no duplicate identities and the same registered dependence-aware
   bootstrap.
9. **The paired confirmation delta interval is entirely positive.** The held-out
   pairs also receive a one-sided cluster sign-flip randomization test, with all
   trades in one six-hour episode flipped together. This test requires cluster
   aggregate delta signs to be exchangeable under a null distribution symmetric
   about zero; it is not assumption-free, and that assumption label is persisted
   and validated. Small cluster families enumerate every sign assignment exactly
   conditional on that null assumption; larger ones use a fixed-seed Monte Carlo
   estimate whose method, seed and resample count are persisted.
10. **The confirmation result survives a Holm correction across every axis
   evaluated in the same qualification run.** Each axis is a separate chance
   to promote something, so a 5% test performed across five axes is not a 5%
   test. The family is every axis that produced a valid verdict in the run,
   including the axes that failed earlier — an axis that failed still consumed
   a chance, and dropping it would shrink the family exactly when a candidate
   needs the family small. `correct_axis_family()` records the family size, the
   calibrated raw p-value, adjusted figure and axis list on every verdict,
   promoted or not, and
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
Forward qualification groups them by immutable completed assignment. Every
eligible current-code/current-model/current-config attempt is retained, with
its assignment boundaries, retry lineage, provenance hashes, candidate actions
and that attempt's own baseline actions. Baseline rows from another assignment
are never pooled into a candidate's comparison.

Inference uses the return difference inside each exact pair. The bootstrap
keeps observations together in six-hour market clusters and samples
contiguous cluster blocks, preserving cross-symbol market episodes and
short-run serial dependence. The output always carries paired count, proposal
union count, coverage, left/right-only proposals, unresolved outcomes,
duplicate identities and examples of mismatches.

The family-wise test uses the same exact pairs and six-hour clusters but a
different operation: it flips whole-cluster delta signs under the null. It is
valid only when cluster aggregate delta signs are exchangeable under a null
distribution symmetric about zero; the evidence records this assumption as
`cluster_delta_sign_exchangeability_under_symmetric_null`. Exact enumeration
is labeled exact only for enumeration of the sign assignments conditional on
that assumption. Fixed-seed Monte Carlo output is labeled as an estimate and
records its resample count and seed.

A pair both arms vetoed stays in the estimate. Its delta is exactly zero,
which is the correct contribution: a policy is run over every opportunity, so
a variant that gains 1R on 1% of them is worth 0.01R per opportunity, not 1R.
Dropping those zeros would change the estimand to "conditional on at least one
arm acting", inflate a rarely-active variant's effect, and discard the
uncertainty those clusters carry.

**A window with no informative pair is inadequate, not negative.** When every
pair is a concordant veto the interval collapses onto exactly zero, and a
nonpositive delta is otherwise read as a performance failure - which would
book a strategy that never fired as one that was tested and lost. Windows
therefore report `informative_pairs`, and a window with none is refused at
the adequacy gate. This is a floor of one: it targets the empty case only and
is not a second sample requirement on mixed evidence.

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

Holm correction accepts only a supplied calibrated raw p-value. The forward
axis path supplies the paired cluster sign-flip value above. Generic callers
that only have a confidence interval fail closed at p=1; interval width or
distance from zero is never converted into a pseudo-p-value.

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

## Recorded-data discovery boundary

The recorder event plane is `event-plane.v1` at
`runtime/research/market_events.db`; it is separate from and does not increment
`forward_feed_version: 8`. Ingestion preserves receipt/availability, source,
feed, schema, revision, and quality metadata, uses strict event and availability
as-of filtering, archives raw CSV inputs, and quarantines malformed or legacy
rows. Confirmed `execution_bar_1m` bars are joined after the signal feature
cutoff with a later bounded outcome cutoff for bars and funding; direction is
evidence-derived, and normalization is evaluated on the persisted episode path.
Bars must be contiguous. Direct timeout requires full horizon coverage, while
partial bars remain `no_data`. Nightly ingestion runs before discovery.

Discovery is a bounded research-only screen: typed IR, generated verified
evaluator, fixed mechanism-aligned exit profile, deterministic
counterfactuals, a small fit-only world model, and exact source-event digest.
Its candidate/analysis records are append-only and cannot authorize registry,
configuration, tier, or order changes. `IDLE`, `NO_DATA`, and `NO_STATE_DATA`
remain non-authorizing outcomes. `COMPLETE` is still research-only: scalar or
mixed scalar/non-episode rows cannot complete a counterfactual, and no
discovery result grants registry/configuration/demo/live authority.

The protocol and shortlist share `research/evidence_primitives.py` for
canonical opportunity identity, duplicate-safe indexing, chronological split,
and pair/union coverage. Their lane policies remain separate, but duplicate
rows are never resurrected by either path. Price-cache ranges use an
end-exclusive `end_ms`; a bar exactly at the endpoint is outside the window.

## From collection to reviewed evidence

The current realtime path is one shared baseline plus a bounded candidate batch
for each realtime strategy, not every setting at once. Four strategy evaluators
advance on every available shared snapshot using deterministic contracts and no
per-strategy LLM calls. The three long-horizon registered models are offline-
only. The shipped `shadow_only` runtime creates no order path or `:llm` scope.
Analyst mode may retain its genuine choices in a separate, non-comparable scope; those rows
are planner history rather than qualification evidence. Packet computation may
run concurrently, while durable writes remain serialized.

The model decision throttle is elapsed-time based: with
`cycle.decision_interval_seconds: 300`, the engine waits for at least 95% of
the interval since the prior decision. The 60-second housekeeping/safety loop
continues independently.

Selections and adaptive values are first-class immutable identities. An
accepted selection waits behind the active assignment. Terminal assignment
evidence, outcomes, findings, review attempts, explanations, and any
research-only edge candidate remain append-only in schema 17.

The separate `research.py forward-qualify` path continues to apply the
multi-setting criteria above to complete prequalification decision-ledger
evidence. Schema migration 7 preserves legacy trades for audit but prevents
executed-trades-only populations from silently qualifying an edge. Each
setting uses all eligible completed assignment windows and the baseline from
those same windows; an operational failure invalidates its assignment evidence
rather than becoming missing data.

Evidence-package verification recursively verifies every `parent_evidence_ids`
package and rejects a missing, tampered, cyclic, or over-depth parent. A child
cannot become authoritative merely because its parent digest is present.

Staged family registration is atomic: the root and all bounded initial
configurations publish together, and a validation failure leaves no partial
family. The staged maker-first boundary remains demo-only measurement; it is
not a live-order or promotion path.

The proposal-fidelity replay is narrower than risk or execution validation: it compares the
full canonical pre-risk proposal identity (cycle, symbol, direction, setup
identity/type, signal timestamp, strategy version, and baseline variant)
symmetrically with replay keys. It requires a non-vacuous exact match and
fails closed on malformed, duplicate, missing, or extra identities. Outcome
resolution gaps remain diagnostics rather than proposal mismatches. A failed,
stale, or vacuous result blocks treating journal-derived evidence as authoritative.

Forward qualification proves one strategy version and one declared axis,
verifies identical non-axis executable inputs, persists family correction, and
recomputes claims from embedded source evidence. A qualifying result can begin
a clean isolated local PAPER account only when flat.

PAPER success still does not edit the strategy registry. `research.py
`t3-packet` builds an immutable SHA-256-addressed review bundle containing proposal fidelity,
configuration/code/corpus provenance, held-out forward evidence, and the paper
result. Neither a packet nor any other research artifact automatically changes
the configured strategy, registry tier, account mode, risk, or capital.
