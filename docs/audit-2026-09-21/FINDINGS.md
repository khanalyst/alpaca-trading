# Full strategy, signal and variant audit — 21 September 2026

Measured audit of all 43 registered arms against 44,993 real one-minute bars
(24 configured ETFs, 5 regular sessions, 15–21 September 2026), replayed
through this repository's own `agent.contracts.rule.evaluate_rule_signal`.

Reproduction scripts and raw per-arm output are in this directory.

## Summary

The system has never produced a trade, in any lane, and with the shipped
configuration it cannot. This is not a weak edge or a costing problem. It is a
geometric impossibility that is measurable to four decimal places.

Every historical "negative result" in this repository is one of two things:

1. `execution_blocked` — no trade occurred, so expectancy is undefined, not
   negative; or
2. output from a diagnostic replay lane that applies a different stop rule,
   a different cost model, and none of the runtime admission filters.

---

## 1. The stop floor admits 0.0000% of bars

The authored equity stop is `max(1 × ATR14(1-minute), 30 bps)`. The
stressed-cost gate refuses any plan whose stop is below
`scenario_bps / max_cost_to_risk_ratio` = `25.0 / 0.30` = **83.33 bps**. The
stop is vetoed, never widened (`agent/risk.py:739`,
`research/factory_core.py:818`, `research/edge_discovery_core.py:1071`).

Measured ATR14 on one-minute bars, 43,193 observations:

| statistic | ATR (bps of price) |
| --- | --- |
| p5 | 1.37 |
| p25 | 2.21 |
| median | 3.17 |
| p75 | 4.85 |
| p95 | 9.54 |
| **maximum, entire universe** | **44.8** |

* The 30 bps grammar floor binds on **99.882%** of bars.
* The 83.33 bps stress floor binds on **100.000%** of bars.
* The single most volatile bar observed anywhere reaches 44.8 bps, which is
  **half** the required floor.

`stop_atr` is therefore inert on every one of the 43 arms. ATR14 is computed
and discarded on every bar. The system has no volatility normalisation at all:
XLRE and SPY receive an identical stop in basis points.

### The search space cannot escape it either

`_STOP_ATR_LADDER` tops out at 10.0. Fraction of real bars where
`stop_atr × ATR ≥ 83.33 bps`:

| stop_atr | ATR needed | % of bars qualifying |
| --- | --- | --- |
| 1.0 (all 43 shipped arms) | 83.3 bps | **0.0000%** |
| 2.0 | 41.7 bps | 0.009% |
| 4.0 | 20.8 bps | 0.345% |
| 8.0 | 10.4 bps | 3.642% |
| 10.0 (ladder maximum) | 8.3 bps | 7.383% |

## 2. The unblocking lever is a step function, and the counterfactual tested the wrong side of it

`research/cost_counterfactual.py` defaults to comparing ratio 0.30 against
0.60. Measured admission rates:

| lever | value | required stop | % signals admitted |
| --- | --- | --- | --- |
| ratio | 0.30 (shipped) | 83.3 bps | 0.000% |
| ratio | 0.60 (counterfactual alternative) | 41.7 bps | **0.009%** |
| ratio | 0.80 | 31.2 bps | 0.100% |
| ratio | **0.8334** | 30.0 bps | **100.000%** |
| scenario | 25 bps (shipped) | 83.3 bps | 0.000% |
| scenario | 15 bps (preregistered) | 50.0 bps | 0.000% |
| scenario | **9 bps (preregistered)** | 30.0 bps | **100.000%** |

Admission is a step function that flips at the point where
`scenario / ratio` falls to the 30 bps grammar floor. The counterfactual's
0.60 alternative sits on the wrong side of the cliff and would have reported
"no material change", confirming the status quo.

**The 9 bps scenario is already inside the preregistered set
(9 / 15 / 25 / 50). Selecting it moves admission from 0% to 100% with no code
change, no new data, and no threshold invention.**

## 3. The cost model is 9–14x too harsh, measured two independent ways

Shipped: 4.0 bps spread + 6.0 bps slippage/side + 0.5 bps fee/side = **17 bps**
round trip. `research/quote_costs.py` already states this is "the dominant term
in every replayed result".

| estimator | median quoted spread |
| --- | --- |
| one tick / price | 0.92 bps |
| Corwin-Schultz high-low | 1.18 bps |

Realistic all-in round trip for this universe: **~1.2 bps** (cross spread twice
≈ 0.92, SEC Section 31 sell-side ≈ 0.28, TAF and impact ≈ 0 at a $25k clip).

A single global spread constant is wrong in both directions across a universe
spanning $41 to $774:

| symbol | price | 1 tick | shipped 4 bps is |
| --- | --- | --- | --- |
| SPY | 760.10 | 0.132 bps | 30.4x too high |
| QQQ | 716.22 | 0.140 bps | 28.6x too high |
| SMH | 560.45 | 0.178 bps | 22.4x too high |
| XLRE | 42.96 | 2.328 bps | 1.7x too high |
| XLU | 41.40 | 2.415 bps | 1.7x too high |

`costs.measured_quote.enabled` is `false`. The fitter that replaces the
assumption with a measurement is written, tested, and switched off.

## 4. All 7 IBR arms are structurally incapable of firing

`evaluate_ibr_breakout` requires `0.25 ≤ opening-range width / ATR14 ≤ 3.0`.
The opening range spans 15–45 minutes; the ATR is a one-minute ATR. The ratio
is structurally 5–15.

| range_minutes | median width/ATR | minimum observed | sessions passing |
| --- | --- | --- | --- |
| 15 | 7.2 | 3.09 (QQQ, 09-16) | **0 of 117** |
| 30 | 8.6 | 3.33 | **0 of 116** |
| 45 | 12.0 | 4.75 | **0 of 115** |

Not one of 348 symbol-sessions passes. The closest miss was 3.09 against a
bound of 3.00.

### The research and runtime IBR are different strategies

`agent.contracts.ibr.IBRConfig` and `research.ibr.IBRConfig` share only
**3 substantive fields of 25**: `range_minutes`, `target_r`,
`breakout_buffer_bps`. Those are exactly the three axes the 6 IBR variants
vary.

Present in runtime, absent from the research replay: `min_ibr_width_atr`,
`max_ibr_width_atr`, `max_ibr_width_pct`, `min_relative_volume`,
`max_spread_bps`, `max_entry_extension_r`, `stale_minutes`,
`latest_entry_time`, `atr_period`, `session_start`, `session_end`,
`force_flat_minutes_before_close`.

The research lane also uses a fixed `stop_pct = 0.003` rather than the runtime's
opposite-range-edge stop.

**Consequence:** the only positive result in this project's history
(`ibr.range.45`, +$1.055386 over 42 one-share trades) was produced by a code
path that omits the filter which makes the runtime path fire zero times. It is
not weak evidence for the deployed strategy. It is evidence about a different
strategy.

## 5. Three filters are no-ops on real data

| filter | used by | admits |
| --- | --- | --- |
| `volatility` confirmation (`atr_bps ≤ 45`) | both `mean_reversion` arms | **100.000%** |
| `volatility_breakout` compression ≤ 55 bps | baseline | 98.31% |
| `volatility_breakout` compression ≤ 65 bps | variant | 98.93% |

The `volatility` confirmation cannot reject anything: the maximum ATR observed
anywhere is 44.8 bps against a 45 bps bound. Both `mean_reversion` arms are
declared as confirmed and are in fact unconfirmed.

The `volatility_breakout` family is named for compression and does not select
compression. Its entire variant axis (55 → 65) moves the admitted population by
0.6 percentage points.

Filters that do work: `range_expansion` 2.0x (4.7%), `volume_breakout` 1.5x
(18.1%), `volume` confirmation 1.25x (24.5%).

## 6. Ten of thirty-one spec fields are identical across all 36 rule arms

| field | value in every arm |
| --- | --- |
| `atr_period` | 14 |
| `stop_atr` | 1.0 |
| `side` | both |
| `entry_after_minutes` | 0 |
| `min_atr_bps` | 0.0 |
| `max_atr_bps` | 5000.0 |
| `breakeven_r` | null |
| `exit_before_minutes` | null |
| `confirmations` | [] |
| `target_lookback` | 20 |

`min_atr_bps`/`max_atr_bps` at 0/5000 means the volatility regime band is
never active. `confirmations: []` means the v2 multi-filter list has never been
used; only the single legacy `confirmation` field is populated.

## 7. All 12 variant axes are entry-selectivity knobs

| family | axis | population change | 30-min return delta |
| --- | --- | --- | --- |
| opening_range_breakout | threshold_bps 5→8 | -11.1% | +0.22 |
| opening_range_fade | threshold_bps 8→12 | -51.6% | +7.60 |
| momentum_continuation | threshold_bps 18→24 | -39.7% | +3.31 |
| mean_reversion | zscore 1.5→1.75 | -33.8% | -0.18 |
| **trend_pullback** | threshold_bps 15→20 | **+2.0%** | +0.16 |
| **volatility_breakout** | compression_bps 55→65 | **+3.1%** | +1.55 |
| volume_breakout | volume_multiplier 1.5→1.75 | -18.3% | +0.20 |
| vwap_reversion | threshold_bps 25→35 | -39.4% | -0.87 |
| vwap_trend | threshold_bps 8→12 | -66.0% | -14.09 |
| range_expansion | volume_multiplier 2.0→2.5 | -45.5% | -2.80 |
| opening_drive | threshold_bps 30→40 | -34.6% | -0.19 |
| cross_sectional_residual | threshold_bps 12→16 | -44.8% | +0.43 |

Not one axis tests an exit, a holding period, a side, a time-of-day window, or
a volatility regime. Two axes (`trend_pullback`, `volatility_breakout`) barely
move the traded population and are effectively null experiments that still
consume false-discovery-rate multiplicity budget.

`trend_pullback`'s axis is inert because the predicate is
`abs(close - fast) / fast <= max(threshold, 0.0005)`; both 15 bps and 20 bps are
far wider than the actual one-minute deviation distribution.

## 8. The bracket is 4–6x wider than the path the trades actually take

Replaying all 36 arms under the shipped geometry:

| statistic | value |
| --- | --- |
| mean time-exit rate across 36 arms | **65.5%** |
| mean target-hit rate | 10.9% |
| mean stop-hit rate | 23.6% |
| median MFE | +10 to +15 bps |
| median MAE | -11 to -18 bps |
| target distance | 45 to 60 bps |

The median trade never travels a quarter of the way to its target. Sixteen
arms exit on time more than 75% of the time; the worst reaches 92.5%.

This independently reproduces the repository's own September 10 audit finding
of 79.08% time exits, on fresh data the system has never seen.

## 9. Measured signal value, and why none of it is an edge

Zero of 36 arms beat the modelled 17 bps round trip at any horizon. Six beat a
realistic 1.2 bps round trip at 30 minutes:

| arm | fires | 30-min return | naive t |
| --- | --- | --- | --- |
| momentum_continuation variant | 426 | +9.84 bps | 4.04 |
| volume_breakout variant | 165 | +8.74 bps | 2.90 |
| volume_breakout baseline | 202 | +8.54 bps | 3.30 |
| volatility_breakout variant | 299 | +8.38 bps | 4.00 |
| volatility_breakout baseline | 290 | +6.83 bps | 3.29 |
| momentum_continuation baseline | 706 | +6.54 bps | 4.07 |

**These are not edges.** Broken down by session:

| arm | 09-15 | 09-16 | 09-17 | 09-18 | 09-21 | pooled | clustered t |
| --- | --- | --- | --- | --- | --- | --- | --- |
| momentum_continuation variant | +0.80 | **+13.70** | -2.29 | -2.72 | +1.25 | +9.84 | **0.72** |
| volatility_breakout variant | -2.84 | **+15.06** | -0.60 | -1.94 | +8.65 | +8.38 | **1.04** |
| volume_breakout baseline | -6.40 | **+15.43** | +0.85 | -3.73 | +8.90 | +8.54 | **0.74** |
| vwap_reversion baseline | +1.97 | **-12.99** | +3.13 | +3.85 | -4.48 | -2.75 | -0.54 |

Every result is one session. 16 September was a trending day: continuation
families won, reversion families lost by the same amount. Clustering on the
session (the only defensible unit) collapses t from 4.07 to 0.43. Significance
at df=4 needs |t| > 2.78.

This is exactly what the repository's clustered bootstrap and 30-session-cluster
floor exist to catch. **The statistical machinery is correct and would reject
all six.** The lesson is that pooled t-statistics over overlapping windows on
24 correlated ETFs are meaningless, which is also why "more variants on the
same data" has never produced anything.

## 10. Documentation defects

| file | claim | reality |
| --- | --- | --- |
| `ARCHITECTURE.md:165` | "If it binds, a fixed-R target is recomputed from the effective stop" | All four lanes veto. `README.md` and `research/README.md` correctly say the stop is never widened. |
| `ARCHITECTURE.md:650` | "The current `REPLAY_ENGINE_EPOCH` is 5" | `research/edge_ledger_store.py:58` sets it to 6. |
| `agent/contracts/rule.py:1998` | field named `volume_multiplier` | means a *range* multiple for `range_expansion`; the LLM tuner reads field names |
| `research/mechanism_cohort.py` fair-value arms | `target_r: 2.0` | inactive; `target_mode` is `session_vwap` |

Two further semantic defects, already acknowledged in the repository's own
docs but still live:

* `cross_sectional_residual` is single-leg SPY-relative momentum with fixed
  beta 1, not a residual or a hedge, and its allowlist excludes SPY, GLD and TLT.
* `trend_pullback`'s `trend` confirmation duplicates the family predicate's own
  fast/slow SMA test, so it is a no-op for that family.

## 11. `opening_drive` does not implement its name

`_family_direction` measures open-to-window-close drive, then requires only a
green or red current candle. It never requires price to hold the drive. Open
100, endpoint 106, pullback to 103, current 103.05 green: passes.

## 12. Evidence budget

Immutable floors: 100 trades + 30 sessions + 30 clusters (backtest), the same
again (sealed qualification), 150 trades + 30 sessions (live-shadow tail).
`ARCHITECTURE.md` states 210 forward sessions total, about 0.83 years.

The binding constraint is sessions, not trades — provided signals fire. They do
fire (4,196 on one arm over 5 sessions), and 100% are vetoed, so the trade floor
is unreachable no matter how long the system waits.

---

## Ordered remediation

### Step 1 — unblock admission honestly (no code change)

Set `risk.stressed_cost_scenario_bps` from 25.0 to **9.0**, already in the
preregistered set. Keep `max_stressed_cost_to_risk_ratio` at 0.30. Admission
goes from 0% to 100%; the required stop becomes 30 bps, which is the grammar
floor. 9 bps remains ~7x more conservative than the measured ~1.2 bps round
trip.

Do **not** raise the ratio instead. It is the same cliff approached from the
wrong direction, and it weakens the risk contract rather than correcting a
measurement.

### Step 2 — replace the cost assumption with the measurement

Enable `costs.measured_quote` and fit a per-symbol, per-half-hour schedule with
`research.cost_rerun --calibration-only`. Every replayed result to date is
dominated by two constants that are 30x wrong for SPY and 1.7x wrong for XLU.
Until this is fitted, no P&L number in the repository means anything.

### Step 3 — fix the IBR width band or retire the 7 arms

`max_ibr_width_atr: 3.0` compares a 15-minute range against a 1-minute ATR.
Either compare like with like (range width against a range-period ATR, so the
band means what it says), or set the band from the measured distribution
(median 7.2, p5 3.6, p95 14.0). As shipped, the 7 arms are dead weight in the
FDR denominator.

### Step 4 — repair or retire the three dead filters

* `volatility` confirmation: bound of 45 bps against a max observed ATR of
  44.8 bps. Set it from the measured distribution or remove it.
* `volatility_breakout` compression: 55/65 bps against a p50 of 11.3 bps.
  The family does not select compression. A meaningful bound is near p25 (~7 bps).
* `trend_pullback` proximity: `max(threshold, 0.0005)` makes both variant values
  inert.

### Step 5 — make the variant axes test what matters

Every axis is entry selectivity. The measured failure is exit geometry:
65.5% time exits, MFE a quarter of the target. Add axes on `max_hold_bars`,
`target_r`, `stop_atr`, `entry_after_minutes`/`entry_before_minutes`, and
`min_atr_bps`. The ladders already exist in `research/factory_core.py`; they
have simply never been exercised because the factory has never completed a
graded cycle.

### Step 6 — run the signal-quality measurement as the primary instrument

`research/signal_quality.py` measures conditional forward returns against a
clock-matched control at horizons 5/15/30/60/120/390. It needs bars only: no
quotes, no accepted forward sessions, no paper trial. Run it over 60+ sessions
for all 43 arms before touching another parameter.

Predeclared kill criterion: if no arm shows a control-adjusted mean above
+3 bps at any horizon with a **session-clustered** t above 2 over 30+ session
clusters, the 12-family grammar on 24 ETFs contains no edge, and universe
change is the only remaining move.

### Step 7 — fix the data foundation

IEX carries roughly 2% of consolidated volume. Every volume, RVOL and VWAP
feature in the codebase consumes it, and IEX quotes are not the NBBO. Alpaca's
full SIP feed is about $99/month. Until then, `volume_breakout`, the `volume`
confirmation on 7 of 12 families, both VWAP families and IBR relative volume
are conditioning on noise.

### Step 8 — change the working ratio

157,119 lines of code, 2,173 tests, ~150 lines of signal logic, zero trades.
Until one arm shows a positive control-adjusted forward return, cap work on
provenance, hashing, WAL lifetime, epoch identity and test scaffolding. The
apparatus is finished and is better than it needs to be. Nothing in this audit
required any of it; it required five days of bars and the repository's own
evaluator.
