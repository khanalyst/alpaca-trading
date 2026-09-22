# Steps 5, 6 and 2: exit geometry, signal quality, and costs

Completes the remediation list in `FINDINGS.md`. Same corpus throughout:
177,504 one-minute bars, the configured 24-ETF universe, 19 sessions
(2026-08-25 to 2026-09-21).

**This document overturns a conclusion I reached earlier in this work.** The
audit predicted that the twelve-family grammar carried no control-adjusted
signal. Measured properly, it carries a large and highly structured one. The
sign of that signal is the opposite of what nine of the twelve families assume.

---

## Step 5, exit geometry: a real defect worth about half a basis point

65% of trades exit on time and the median excursion is a quarter of the target,
so the bracket is plainly mis-sized. The question is what fixing it is worth.

Swept the repository's own ladders from `research/path_telemetry.py`
(`TARGET_HOLD_TARGET_LADDER` plus 1.5 so the shipped reversion geometry is on
the grid, and `TARGET_HOLD_HOLD_LADDER`): 81 geometries for each of 24 arms,
1,944 combinations.

| selection method | mean gross |
| --- | --- |
| shipped geometry, in-sample | -0.07 bps |
| best of 81 per arm, **in-sample** | +2.68 bps |
| best of 81 per arm, **held-out** | +0.48 bps |
| shipped geometry, held-out | +0.07 bps |
| **honest gain from re-picking** | **+0.41 bps** |

Picked on the first 10 sessions, graded on the last 9. **85% of the apparent
improvement was overfitting**, and the pick helped on only 14 of 24 arms.

A predeclared geometry, which cannot overfit, does slightly better than a
searched one and still does not reach break-even:

| geometry | mean held-out gross |
| --- | --- |
| `target_r=1.25, hold=45` (best predeclared) | **+0.51 bps** |
| `target_r=1.5, hold=45` | +0.40 bps |
| shipped | +0.07 bps |
| `target_r=0.25, hold=180` (worst) | -1.53 bps |

**Decision: do not add exit-side variant axes to the cohort.** The whole
dimension is worth about +0.44 bps against a realistic 1.2 bps round trip.
Twelve new arms chasing half a basis point would inflate the false-discovery
denominator for every other hypothesis. The defect is real; it is not where
the money is. This is a measured negative result, not a skipped step.

The searched picks also cluster at ladder extremes (`hold=1`, `hold=390`,
`target_r=10.0`), which is the usual signature of fitting noise. `hold=1` is
not a strategy; it is the one-bar forward return.

---

## Step 6, signal quality: the decisive measurement

`research/signal_quality.py` measures conditional forward return minus a
**clock-matched control**: the same instrument, at the same session minute, on
every other session. That removes the intraday time-of-day confound that makes
raw forward returns uninterpretable. It was run here as the primary decision
instrument for the first time.

Residual clock gap between candidate and control: **0.025 session-minutes**.
The control is doing its job.

Control-adjusted delta, basis points:

| family | 5m | 15m | 30m | 60m | 120m |
| --- | --- | --- | --- | --- | --- |
| **vwap_reversion** | +1.04 | +3.39 | +5.78 | **+9.00** | **+11.15** |
| **vwap_reversion** (variant) | +2.75 | +6.02 | +6.35 | +8.48 | +10.79 |
| **opening_range_fade** | +0.20 | +6.50 | +8.65 | **+16.81** | **+25.17** |
| **opening_range_fade** (variant) | +1.14 | +13.66 | +15.08 | +24.36 | +28.06 |
| mean_reversion | +0.45 | +0.97 | -0.21 | -0.09 | +0.44 |
| volume_breakout | -2.00 | -2.42 | -1.48 | +1.67 | -2.07 |
| trend_pullback | -0.84 | -0.36 | -2.07 | -5.12 | -4.90 |
| cross_sectional_residual | -1.67 | -2.52 | -3.34 | -5.06 | -6.01 |
| volatility_breakout | -3.61 | -4.44 | -5.09 | -6.52 | -4.77 |
| range_expansion | -2.31 | -2.64 | -3.67 | -5.13 | -8.72 |
| opening_range_breakout | -0.58 | -4.12 | -4.49 | -7.25 | -6.75 |
| momentum_continuation | -1.75 | -3.96 | -5.13 | **-7.76** | **-8.77** |
| opening_drive | +0.45 | -4.79 | -7.06 | **-10.49** | -9.10 |
| **vwap_trend** | -2.45 | -5.85 | -9.89 | **-13.34** | **-14.45** |

The predeclared kill criterion from `FINDINGS.md` was: no arm above +3 bps with
|t| > 2 at any horizon means the grammar is dead. It returns **11 hits**. The
criterion is not met. The grammar is not dead.

### The structure is the finding

| | positive at 60m |
| --- | --- |
| reversion families (repo's own `REVERSION_FAMILIES`) | **5 of 6** |
| continuation families | 2 of 18 (16 of 18 negative as predicted) |

**21 of 24 arms fall on the side their family class predicts.** Magnitude grows
monotonically with horizon in both directions.

The cleanest internal check is the mirror pair. `vwap_reversion` and
`vwap_trend` read the same session-VWAP deviation with opposite sign:

| horizon | vwap_reversion | vwap_trend |
| --- | --- | --- |
| 30m | +5.78 | -9.89 |
| 60m | +9.00 | -13.34 |
| 120m | +11.15 | -14.45 |

One signal, two signs, opposite outcomes of similar magnitude. That is not a
multiple-testing artefact.

**Plain reading: over this window, intraday deviation on these ETFs reverts.
Nine of the twelve families are built on the opposite assumption, and they lose
in proportion to how strongly they express it.** `vwap_trend`, the purest
continuation expression, is the single worst arm in the catalogue.

### What this does not establish

- **The t-statistics are event-level, not session-clustered.** The module
  reported `session_clusters` but did not use it in the standard error.
  ~~On the same arms, the session-clustered t computed in Q2 ran roughly one
  third of the event-level value. Treat every t above as about 3x overstated:
  `vwap_reversion` at +4.67 is nearer +1.6 clustered, which is **not**
  significant on its own.~~ **Corrected 22 September: this estimate was wrong.**
  It was extrapolated from Q2's *raw* forward returns, which share each day's
  market shock. The signed, control-adjusted delta largely does not, because
  long and short signals in one session cancel that shock. Measured directly
  (`RE-AUDIT.md`), clustered t is a median **0.86** of the event-level value,
  and `vwap_reversion` holds at clustered **t = +4.34** (df 17). See the
  correction section at the end of this document.
- **The 24 arms share 19 sessions**, so their signs are correlated and the
  sign-test p is overstated for the same reason. One dominant regime would line
  every arm up regardless of predicate quality.
- **19 sessions is one regime** and is below the repository's own 30-cluster
  floor. The first five sessions of this window favoured continuation; the
  full window favours reversion.
- A control-adjusted forward return is **not** a tradeable P&L. No stop, no
  bracket, no costs on either leg.

So: a strong, coherent, economically legible pattern that is **not yet
significant** under the repository's own standards. That is exactly what a
lead looks like before it is either confirmed or killed by more data.

### Five independent measurements now agree

`vwap_reversion` is the strongest arm under every method applied in this work:

| measurement | result |
| --- | --- |
| Q2 raw forward return, 19 sessions | 14 of 19 sessions positive |
| Q1 post-fix bracket replay | strongest arms, clustered t +2.15 / +2.79 |
| Step 5 held-out geometry split | positive out-of-sample (+6.71, +2.63) |
| Step 6 control-adjusted signal quality | +9.00 bps at 60m, highest delta |
| Step 6 mirror pair | its inverse is the worst arm in the catalogue |

---

## What to do with this

1. **Predeclare one hypothesis and power it properly.** Session-VWAP reversion
   on this universe, entry at a stated deviation threshold, hold to 60-120
   minutes, over 30+ session clusters. One hypothesis, not one of 24 competing
   for alpha. The repository's gates are built for exactly this and have never
   had a candidate worth spending them on.
2. **Do not add reversion-tuned arms now.** That would be selecting on an
   in-sample result, which the protocol forbids and which Step 5 just
   demonstrated the cost of.
3. **Consider retiring the continuation families.** Eighteen of 24 arms sit in
   families that are consistently and substantially negative, with the effect
   growing in horizon. They are not merely unprofitable; they are
   systematically backwards on this universe. Retiring them shrinks the
   false-discovery denominator for the hypotheses that are worth testing.
4. **Fix the clustered standard error in `signal_quality.py`.** Done on
   22 September: every horizon now reports a session-clustered t, its degrees
   of freedom and a cluster sign-flip p. (The "roughly 3x" overstatement stated
   here originally was wrong; the measured ratio is a median 0.86.)
5. Exit geometry: leave it. Measured at +0.44 bps.

---

## Step 2, costs: still operator-blocked, with the evidence quantified

Unchanged from `REMEDIATION.md`: fitting `costs.measured_quote` needs a quote
corpus with 500+ quotes per cell and exact feed provenance. This session has
bars, not quotes, and fabricating a schedule would put an unmeasured number in
the place the repository is most careful about.

`docs/audit-2026-09-21/spread-reference.json` is added as a **diagnostic**
reference, explicitly `authorizing: false` and not wired into any cost path.
It records, per symbol, the tick-implied spread floor and the Corwin-Schultz
high-low estimate over the same 19 sessions, so an operator can sanity-check a
fitted schedule before trusting it. Median across the universe is near 1 bps
against the shipped global 4.0, with the overstatement ranging from 1.7x
(XLU, XLRE) to 30x (SPY).

The operator command remains:

```sh
.venv/bin/python -m research.cost_rerun \
  --calibration-only --corpus /absolute/path/to/frozen/quotes.jsonl \
  --config config.yaml --min-quotes-per-cell 500 \
  --publish-latest /absolute/path/to/calibration-latest.json
```

Note the sizes involved. At 60 minutes `vwap_reversion` shows +9.00 bps
control-adjusted against a realistic round trip near 1.2 bps and a modelled one
of 17 bps. Which of those two numbers is correct decides whether the lead is
tradeable, so Step 2 now matters more than it did before this measurement.

---

## Verification

No source change was made for Steps 5, 6 or 2. They are measurements and
documentation. The full `deploy/test_suite.py` result is recorded in the commit
message.

---

## Correction, 22 September 2026

Re-measured with the session-clustered inference now built into
`research/signal_quality.py`, on the 18 examined sessions still available
from the source (26 August to 21 September), with the public data labelled
honestly as `historical_backfill` on `delayed_sip`.

| arm, 60 minutes | delta | event t | **clustered t** | df | sign-flip p |
| --- | --- | --- | --- | --- | --- |
| `vwap_reversion` baseline | +9.44 | 4.79 | **+4.34** | 17 | 0.0010 |
| `vwap_reversion` variant | +9.07 | 3.97 | +3.23 | 17 | 0.0027 |
| `opening_range_fade` baseline | +19.51 | 3.82 | +2.95 | 17 | 0.0036 |
| `vwap_trend` baseline (mirror) | -13.12 | -4.96 | **-6.36** | 17 | 1.0000 |

What changes:

- **The in-sample evidence is materially stronger than stated above.**
  Clustering costs a median 14% of the t-statistic here, not two thirds.
- **Eleven continuation arms are individually and significantly negative**
  once clustered (t from -2.45 to -6.36, df 17), so the reversion/continuation
  split is not only a pattern across arms; it holds arm by arm.
- **Against the multiplicity actually incurred** (24 arms by 5 horizons, 120
  one-sided tests, Bonferroni 0.00042), `vwap_reversion` passes on the clustered
  t (p = 0.00022) and fails on the sign-flip (p = 0.0010). The preregistration
  requires both, so even in-sample it falls just short of its own bar.

What does not change:

- **All of this is the data that selected the hypothesis.** It strengthens the
  case for the sealed forward test; it cannot substitute for it. That test,
  `vwap-reversion-control-adjusted.v1`, was sealed in commit `c674f2f` before
  this re-measurement was run, and its rules are hash-pinned.
- **Cost.** +9.44 bps at 60 minutes against a realistic ~1.2 bps round trip is
  tradeable; against the modelled 17 bps it is not. Step 2 still decides that.
- **Recommendation 3 is withdrawn.** Retiring the continuation families would
  select on in-sample evidence from one regime. Its stated benefit, a smaller
  false-discovery denominator, no longer applies to the hypothesis that
  matters: the preregistered test spends its own predeclared alpha, outside the
  factory's multiplicity, and the diagnostic shadow cohort spends none
  (`online_fdr: false`). Keeping them is free, and their forward data is the
  regime evidence the preregistered mirror endpoint reads.

