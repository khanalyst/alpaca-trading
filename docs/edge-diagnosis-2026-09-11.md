# Why every edge is negative: an end-to-end diagnosis

**Date:** 11 September 2026. **Scope:** the research lane (`research/`), the rule
grammar (`agent/contracts/rule.py`), the cost and risk policy (`research/costs.py`,
`agent/contracts/risk_geometry.py`, `config.yaml`), and the recorded results in
[the September 8 comparison](trading-edge-comparison-2026-09-08.md) and
[the historical variant appendix](trading-edge-historical-variants-2026-09-07.md).

This continues [the September 7 audit](trading-edge-audit-2026-09-07.md), which
established the engineering defects. This document answers the economic
question the audit left open: *why is the sign negative everywhere, and is there
an edge underneath.*

**Verdict.** The uniform negative result is not twelve failing strategies. It is
one arithmetic identity showing up twelve times. Four causes compound, and they
are ranked below by how much of the negative sign each one explains. The first
two are configuration errors and are fixable this week. The third is a design
error in how risk geometry is derived. The fourth is the real problem and no
configuration change touches it: the hypotheses are candle-shape patterns on
one venue's one-minute bars, an information set with no plausible surviving
edge in the most arbitraged instruments in the world, and the sample design
cannot resolve effects of the size being claimed even if they existed.

---

## 1. The four causes, in order of magnitude

### Cause 1: the cost model charges 5 to 7 times the real cost

`research/costs.py` ships `spread_bps = 4.0`, `slippage_bps = 6.0`,
`fee_bps = 0.5`. The arithmetic in `CostModel.entry_cost_bps` is
`spread/2 + slippage = 8 bps` per side, plus `0.5 bps` fee per side, so
**17 bps round trip**. That is the hurdle every arm in the comparison table was
measured against.

The configured universe is 24 penny-quoted US ETFs. A one-cent spread at their
actual prices:

| Statistic across the configured universe | One-penny spread (bps) |
| --- | ---: |
| Median (XLV / XLI area) | 0.71 |
| Mean | 0.97 |
| Tightest (SPY, QQQ) | 0.17 |
| Widest (SLV) | 3.57 |

The configured 4 bps is **5.6x the universe median and 23x SPY**. It is wider
than the widest member of the universe.

The 6 bps slippage assumption is the larger error. For a marketable order of a
few thousand dollars in SPY or QQQ, slippage beyond the touch is close to zero;
these names show full displayed size at the inside. Six basis points on SPY is
about 36 cents of adverse fill on a one-cent-wide market. Alpaca charges no
commission on US equities, so the only real fees are the SEC Section 31 fee on
sales and the FINRA trading activity fee, together roughly 0.3 bps round trip
rather than 1.0.

| Schedule | Spread | Slippage/side | Fee/side | Round trip |
| --- | ---: | ---: | ---: | ---: |
| Configured (shipped) | 4.0 | 6.0 | 0.5 | **17.0 bps** |
| Defensible stress | 3.0 | 3.0 | 0.15 | 9.3 bps |
| Conservative | 1.5 | 1.5 | 0.15 | 4.8 bps |
| Plausible for this universe | 1.0 | 0.5 | 0.15 | 2.3 bps |

Re-scoring the measured conditional returns from the comparison table against
each schedule, using the same control-adjusted gross figures:

| Arm | Gross minus control | Net @ 17.0 | Net @ 9.3 | Net @ 4.8 | Net @ 2.3 |
| --- | ---: | ---: | ---: | ---: | ---: |
| opening_range_breakout / 0 | +5.08 | −11.92 | −4.22 | −1.22 | **+1.78** |
| opening_range_breakout / 3 | +5.76 | −11.24 | −3.54 | −0.54 | **+2.46** |
| trend_pullback / 2 | +7.20 | −9.80 | −2.10 | **+0.90** | **+3.90** |
| vwap_reversion / 0 | −9.04 | −26.04 | −18.34 | −15.34 | −12.34 |

**Every sign flip in that table is the cost constant, not the strategy.** The
comparison document states "a gross conditional return smaller than the measured
trading cost cannot support an investable edge." The 17 bps was never measured.
`config.yaml` labels its own provenance `shipped_conservative_v1_plus_25bps_stress`,
and `research/costs.py` calls it "the cost of a normal marketable fill." It is
neither. It is a placeholder that has been treated as a measurement for the
entire life of the project.

### Cause 2: the stressed-cost veto is arithmetically unreachable, so nothing trades

`risk.stressed_cost_scenario_bps = 25.0` divided by
`risk.max_stressed_cost_to_risk_ratio = 0.30` requires a minimum stop distance
of **83.33 bps** (`agent/contracts/risk_geometry.py::required_stop_distance_bps`).
Any authored stop narrower than that is refused as `stressed_cost_risk_limit`
in `research/ibr.py:925`, before sizing and before any fill.

The grammar cannot produce an 83 bps stop on these instruments. The stop is
`max(ATR × stop_atr, 30 bps)`, ATR is a 14-period average true range on
**one-minute bars**, and `stop_atr` is bounded at 10.0.

| Annualised vol | 1-min sigma | 1-min ATR(14) | Widest grammar stop (10 ATR) | Clears 83.33 bps? |
| ---: | ---: | ---: | ---: | --- |
| 10% | 3.2 bps | ~4 bps | 40 bps | No |
| 15% | 4.8 bps | ~6 bps | 60 bps | No |
| 20% | 6.4 bps | ~8 bps | 80 bps | No |
| 25% | 8.0 bps | ~10 bps | 100 bps | Only at the ladder maximum |

This is why the comparison table shows **first actionable signals equal to
cost/risk refusals, one for one, in all twelve arms**. It is a 100% refusal
rate, and it is deterministic, not empirical. The mechanism cohort authors
`stop_atr: 1.0`, giving an authored stop of 4 to 6 bps, floored to 30 bps, and
refused against 83.33. Zero trades was the only possible outcome.

A comment in `research/factory_core.py:2048` already records half of this:
"a local +/-20% nudge around a one-ATR root never reaches the several-ATR
distance needed for a 25 bps / 30% cost-to-risk gate on SPY-like ATRs." The
response was to widen `_STOP_ATR_LADDER` to 10.0. That was not enough, and the
diagnosis stopped one step short of the real conclusion: **the stress policy
demands a stop wider than the instrument's typical full-day range while the
thesis horizon is 30 to 60 minutes.** Those two requirements are incompatible.

There is a second-order consequence. With the one-signal-per-symbol-per-session
cap consumed at emission (`agent/engine_cycle.py:789`) and the stress veto applied
after emission, the session's entire opportunity budget is spent on signals that
are then refused. In the comparison run, roughly 200 of 240 available
symbol-sessions were burned this way.

### Cause 3: risk geometry is derived from the cost policy, not from the signal

The stop width is the single most consequential parameter in an intraday
strategy, and here it is set by `max(30 bps floor, stress policy)` with no
reference to what the signal actually does. `stop_atr` is therefore a dead
coordinate for most of the grammar: at ATR 5 bps, `stop_atr` of 1, 2, 3, 4 and 6
all produce the same 30 bps stop. Variants that perturb it are identical
strategies with different identifiers.

The consequence is documented in the repository itself.
`research/cost_rerun.py` opens with: "The diagnostic factory reported a
near-constant 0.16-0.18R execution drag across every family it could execute.
That constancy is the finding: a fixed round trip divided by a stop the risk
gate pins near a fixed width is the same number whatever the strategy does."
That is exactly right and it is 17 bps divided by roughly 100 bps.

Cost as a fraction of risk, at each admissible stop width:

| Round-trip cost | 30 bps stop | 50 bps stop | 83.33 bps stop |
| ---: | ---: | ---: | ---: |
| 17.0 bps | 0.567R | 0.340R | 0.204R |
| 9.3 bps | 0.310R | 0.186R | 0.112R |
| 4.8 bps | 0.160R | 0.096R | 0.058R |
| 2.3 bps | 0.077R | 0.046R | 0.028R |

Break-even win rate, taking winners at the nominal target and losers at one R:

| Cost | Stop | Target | Break-even win rate |
| ---: | ---: | ---: | ---: |
| 17.0 bps | 30 bps | 1.8R | **56.0%** |
| 17.0 bps | 83.33 bps | 2.0R | 40.1% |
| 4.8 bps | 30 bps | 1.8R | 41.4% |
| 4.8 bps | 83.33 bps | 2.0R | 35.3% |

Observed win rates in the historical cohorts were **25% to 40%**. So even at a
corrected 4.8 bps cost, the historical variants would still have lost money.
Fixing the cost model is necessary and it is not sufficient. Anyone reading only
Cause 1 will draw the wrong conclusion.

Two further geometry problems follow:

- **The 83 bps stop is never touched.** At 15% annualised vol, an 83 bps barrier
  is 2.25 sigma over a 60-minute hold, with roughly a 2.5% chance of being
  reached. A stop that does not trigger is not risk control, and R-multiples
  measured against it are not risk units. This is why 79% of the preserved
  historical exits were time exits: the strategy being tested was never
  "breakout with a stop", it was "hold 60 minutes with a decorative bracket."
- **Position sizing is capped by notional, not by risk.** At an 83 bps stop,
  reaching the 0.5% risk budget requires 60% of equity in one name, so the 25%
  `max_position_notional_pct` cap binds first and actual risk per trade is
  0.208% of equity. At a 30 bps stop it is 0.075%. The risk budget in
  `config.yaml` describes an intent the system never expresses.

### Cause 4: the effect sizes being chased are below the resolution of the sample

This is the cause that survives every configuration fix.

The gross conditional effects in the comparison table are +2 to +7 bps over
30 to 60 minutes. At 15% annualised vol the standard deviation of a 60-minute
return is about **37 bps**. The sample size needed to distinguish an effect at
t = 2:

| Claimed edge | Independent observations needed |
| ---: | ---: |
| 2 bps | 1,374 |
| 5 bps | 220 |
| 10 bps | 55 |
| 20 bps | 14 |
| 30 bps | 6 |

The comparison run had 124 to 216 observations, but they are not independent:
eight highly correlated equity ETFs on 30 shared sessions, with overlapping
signal windows. The effective independent count is closer to the session count
than the row count. The comparison document says as much ("shared sessions and
overlapping signals are correlated; counts are not independent portfolio
trials") and then presents the point estimates anyway.

Under the protocol's own multiple-testing regime, this is worse. Twelve arms,
Benjamini-Yekutieli correction, plus a shadow leg and a sealed qualification
window, at 100-trade and 30-session floors. Detecting a 5 bps effect with
family-wise control needs something in the order of a year of forward sessions
for a **single** hypothesis. The readiness calculation already reports 210
required sessions; that figure is not pessimistic, it is optimistic, because it
does not account for the effect size.

The practical implication: **stop looking for 5 bps effects.** Either the
hypothesis has an expected move large enough to be measured in tens of
sessions, or the research budget has to be stated honestly in years.

---

## 2. Are these the edges traders actually trade?

No. This is the part no configuration change fixes.

All twelve families in `RULE_FAMILIES` are functions of the same input: the
last N completed one-minute OHLCV bars from a single venue, for a single
symbol. Opening range breakout, momentum continuation, volatility breakout,
volume breakout and range expansion are five different ways of saying "the
recent bars went up." VWAP trend and momentum continuation are near-collinear.
The audit made this point and it is worth restating quantitatively: twelve
family names do not mean twelve independent information sources; there is
roughly one, and it is price shape.

Candle-shape patterns on one-minute bars of SPY, QQQ and the sector SPDRs have
been systematically traded by better-capitalised, lower-latency participants
for two decades. The prior that a 15-minute opening-range breakout with a 5 bps
buffer contains 5 bps of exploitable drift in SPY should be very low, and the
measured results are consistent with that prior being correct.

What professional intraday traders in this instrument class actually monetise,
and where each stands relative to this system:

| Real edge source | What it needs | Status here |
| --- | --- | --- |
| Overnight (close-to-open) risk premium in index ETFs | Daily bars, one trade per day | **Reachable and untested.** Well documented in the literature; `session.entries_regular_session_only: true` forbids it outright |
| Intraday momentum: the first half-hour predicting the last half-hour | Session-anchored return from open, and a close-auction entry | Not expressible; no primitive for "return since open to time T" |
| ETF relative value / pairs (SPY-VTI, XLK-SMH, TLT-IEF) | Two-leg simultaneous execution and a spread contract | Not implemented; `cross_sectional_residual` is explicitly single-leg and unhedged |
| Order-flow imbalance, signed trade flow, book pressure | Trade and quote tape, depth | Quotes are recorded but never used in a signal; depth only enters the disabled cost model |
| Relative volume versus the same minute-of-day in prior sessions | Cross-session volume history, consolidated | The `volume` confirmation compares to the **adjacent** bars on **IEX only**, which is 2% to 10% of consolidated volume |
| Overnight gap, prior-day high/low/close, opening auction context | Prior-session levels in the entry predicate | Diagnostics exist; no entry predicate can read them |
| Scheduled-event conditioning (CPI, FOMC, earnings, OPEX, rebalance) | An economic calendar | Absent, and named as absent in the audit |
| Cross-asset lead-lag (futures or a faster ETF leading a slower one) | Synchronised multi-symbol bars at decision time | Partially present via `bars_by_symbol`, used only by the residual family |

Two specific signal defects are worth calling out because they are wrong on
their own terms, not just weak:

**`vwap_reversion` is a momentum signal wearing a reversion label.** The
predicate at `agent/contracts/rule.py:1770` is: go long whenever
`close/vwap - 1 <= -threshold`. There is no exhaustion condition, no reversal
confirmation on the legacy trigger, and no volatility normalisation of the
threshold. Being 20 bps below session VWAP in a down-trending session is a
*continuation* state, not a reversion state. Buying it is buying weakness with
no evidence the weakness has stopped. That the two `vwap_reversion` legacy arms
returned **−8.73 and −8.84 bps gross** is the predicate working exactly as
written. The v5 `entry_trigger: "reclaim"` option is the correct fix and it is
not the default.

**A fixed basis-point threshold across 24 instruments is not one hypothesis.**
`threshold_bps: 20.0` means something completely different for SPY (roughly
4 sigma of a one-minute move) and for SLV or XLRE (under 2 sigma). The same
number is a different statistical event per symbol, per hour and per volatility
regime. Every threshold in the grammar should be in units of the instrument's
own realised volatility, not in basis points of price. This alone makes the
pooled cross-symbol statistics hard to interpret.

**The `volume` confirmation is close to noise.** It compares the signal bar's
IEX volume to the mean of the preceding N bars' IEX volume. On a feed carrying
2% to 10% of consolidated volume, per-minute counts are small, lumpy and
frequently zero. A ratio of two noisy small integers is not a participation
measurement. The paired-feed study in the comparison document measured this
directly (IEX/SIP paired volume 1.76% to 9.83%) and the conclusion was not
carried back into the signal layer.

---

## 3. Are the variants done properly?

No, in three separate ways.

**The search is a ±20% one-factor nudge around an arbitrary seed.**
`_coordinate_values` in `research/factory_core.py:2139` returns
`value ± max(0.25, 0.2 × value)` for floats and `value ± max(1, 0.2 × value)`
for integers. This is visible in the persisted parameter sets: 18.0 becomes
14.4 or 21.6; 1.4 becomes 1.12; 45 becomes 36. Coordinate descent inside a
20% ball around a hand-picked seed cannot recover from a mis-specified seed. If
the mechanism is wrong, no neighbourhood of it is right. The ladder axes
(`_STOP_ATR_LADDER`, `_MIN_ATR_BPS_LADDER`) are the exception and they are gated
behind an `execution_blocked` diagnosis.

**Several "variants" are the same hypothesis and the report does not say so.**
In the twelve-arm mechanism cohort, `trailing_stop_r`, `max_hold_bars` and
`regime_timeframe_minutes` changes that do not alter the entry predicate produce
arms with byte-identical entry sets. The comparison table proves it:

| Pair | Signals | Distinct entry predicate? |
| --- | ---: | --- |
| opening_range_breakout 1 and 3 | 201 / 201 | No, identical; only the measurement horizon differs |
| trend_pullback 2 and 3 | 133 / 133 | No, identical; arm 3 adds an exit-only trailing stop |
| vwap_reversion 2 and 3 | 192 / 192 | No, identical; arm 3 adds a shorter max hold |

So there are **nine distinct entry hypotheses presented as twelve**. The
multiple-testing count is wrong in both directions: it over-counts duplicated
entries, and it fails to count the choice of evaluation horizon (30 vs 60 vs 15
minutes, chosen per arm) as the researcher degree of freedom it is. Reporting
`trend_pullback / 2` and `/ 3` as two arms with identical +7.43 bps is a
presentation error that makes a single unconfirmed observation look like two.

**The LLM lane cannot discover anything.** `TUNING_SYSTEM_PROMPT` states it
plainly: "The signals themselves are fixed code that you are tuning, not
designing. You cannot introduce a new signal, indicator or data source."
`DISCOVERY_SYSTEM_PROMPT` is restricted to the same twelve families. The
"autonomous edge discovery" loop is a constrained numeric optimiser over a
fixed, single-input hypothesis space. Calling it discovery sets an expectation
the architecture cannot meet, and it is why 120 lesson records produced zero
graded outcomes.

---

## 4. Are the parameter values realistic?

| Parameter | Shipped value | Assessment |
| --- | --- | --- |
| `costs.spread_bps` | 4.0 | **Wrong.** 5.6x the universe median, wider than its widest member |
| `costs.slippage_bps` | 6.0 | **Wrong by roughly an order of magnitude** for retail size in penny-quoted ETFs |
| `costs.fee_bps` | 0.5/side | ~3x actual; real regulatory fees are sell-side only, roughly 0.3 bps round trip |
| `stressed_cost_scenario_bps / max_ratio` | 25 / 0.30 | **Incompatible with the grammar.** Demands 83.33 bps; grammar maximum is ~40-80 bps |
| `MIN_STOP_DISTANCE_BPS` | 30.0 | Defensible in isolation: ~0.8 sigma over a 60-min hold, implying a 42% noise stop-out rate. It should be derived from the signal's MAE distribution, not fixed |
| `stop_atr` (mechanism cohort) | 1.0 | Produces a 4-6 bps stop on 1-min ATR. Always floored. Meaningless as authored |
| `stop_atr` (historical cohort) | 4.5-6.5 | 18-36 bps. Floored most of the time. A dead search axis |
| `min_atr_bps` / `max_atr_bps` | 15 / 35 | A 1-min ATR band of 15-35 bps corresponds to roughly 25%-55% annualised vol. Excludes ordinary mid-session conditions for SPY entirely |
| `threshold_bps` | 5-30 | Fixed bps across 24 instruments of wildly different volatility. Should be in sigma units |
| `target_r` | 1.2-2.0 | Reasonable in isolation, unreachable in combination with an 83 bps stop and a 60-minute hold |
| `max_hold_bars` | 45-90 | Fine, but with 79% time exits this **is** the strategy, not a backstop |
| `universe` | 24 US ETFs | Nearly one factor. SPY, QQQ, DIA, VTI, VO, VB and nine sector SPDRs are one bet with different weights. Three concurrent positions are one market position |
| `broker.data_feed` | `iex` | 2%-10% of consolidated volume. Breaks VWAP, breaks relative volume, and creates the `no_contiguous_feature_window` refusals (32,277 of 44,352 rows in one cohort) |

---

## 5. What the backtest engine gets right

It is worth being explicit, because the fixes below must not damage these.

The replay is honest and, if anything, pessimistic. Stops win ties within a bar
(`research/ibr.py:1077`). Gaps fill at the bar open, not at an impossible level
price. Entry is on the bar **after** the signal bar, never the signal bar
itself. Feature windows must be contiguous. Bar revisions are resolved by
receipt time rather than silently substituting the corrected value. The
chronological fit/held-out/sealed-qualification split, the randomised-entry
null control, moving-block session bootstrap and Benjamini-Yekutieli correction
are all correctly built. This is not an overfitted backtest optimiser, and the
negative results are not an artefact of a broken simulator.

The problem is the opposite of the usual one. The machinery for **rejecting**
false discoveries is excellent. The machinery for **generating** candidates
worth testing barely exists.

---

## 6. How to fix it

Sequenced so each step produces evidence the next one needs. Do not reorder.

### Step 1: measure the cost instead of assuming it (days, not weeks)

The tooling already exists and is switched off.

1. Backfill quotes for the existing corpus: `deploy/backfill.py --quotes`. It is
   opt-in there and was not used when the sealed snapshot was built, which is
   why that snapshot contains 86,998 bars and **zero quotes**.
2. Fit the per-symbol, per-half-hour schedule with `research/quote_costs.py` and
   enable `costs.measured_quote`.
3. Re-run the frozen twelve arms with `research/cost_rerun.py`, which exists
   precisely for this and has never had an input.

Two cautions on the measured model. It uses **p75 spread with p25 depth**, which
is a conservative corner on both axes simultaneously and compounds. And it will
be fitted on IEX quotes, which are one venue's book and systematically wider
than the NBBO a routed order actually meets. So the measured schedule is an
upper bound on cost, not an estimate. Record it as such.

**Acceptance:** a per-symbol, per-half-hour spread schedule with quote counts
per cell, and the twelve arms re-scored against it. Expect the round trip to
land between 2 and 6 bps, not 17.

### Step 2: make the risk policy internally consistent

The stress constraint is defensible as an idea and its current parameterisation
is not. Three options, in order of preference:

1. **Charge stress against the position's own expected cost, not a flat 25 bps
   of notional.** Once Step 1 gives a measured per-symbol cost, stress it by a
   multiple (say 3x measured) rather than a universal constant. A 1 bps SPY
   round trip stressed 3x is 3 bps, requiring a 10 bps stop at a 0.30 ratio.
   That is reachable and it means something.
2. **Keep 25 bps and raise the ratio** so that the implied stop is inside the
   grammar. At 0.30 the implied stop is 83.33 bps; at 0.60 it is 41.7 bps, which
   is reachable at `stop_atr` 6-8. The August 25 and 28 experiments already ran
   the 0.60 arm and it admitted trades.
3. **Keep both and widen the grammar's `stop_atr` bound** past 10. This is the
   worst option: it buys admission by taking stops so wide they never trigger,
   which is what produced the 79% time exits.

Take option 1. Whatever is chosen, add a startup assertion that
`scenario_bps / max_ratio` is achievable given `MIN_STOP_DISTANCE_BPS` and the
grammar's `stop_atr` bound at the universe's typical ATR. A policy that refuses
100% of signals should fail loudly at configuration load, not silently at
signal time, thirty sessions later.

Separately, move the one-per-symbol-per-session emission cap so it is consumed
**after** the risk veto, not before it. Burning the session's only opportunity
on a signal that is then refused is pure waste.

**Acceptance:** a configuration check that fails on an unreachable stress
policy, and a re-run where the refusal rate is materially below 100%.

### Step 3: derive geometry from the signal, not from the policy

For each candidate entry cohort, before any bracket is authored, measure the
distribution of maximum adverse and favourable excursion over the thesis
horizon. Set the stop at a percentile of the MAE distribution (the point beyond
which the thesis is genuinely wrong), set the target from the MFE distribution,
and set `max_hold_bars` from where the favourable excursion stops growing. The
audit already recommends the MFE/MAE view; make it an **input to authoring**, not
a dashboard chart.

This also repairs the R-multiple. Right now R is a policy constant, so
"expectancy in R" is expectancy in units of an arbitrary number, and the
constant 0.17R drag across every family is the proof.

**Acceptance:** stop and target for each arm traceable to that arm's own
measured excursion distribution, with the stop-out rate on noise stated
explicitly.

### Step 4: separate entry quality from trade management, permanently

Run the diagnostic in two stages and never blend them:

- **Stage A, entry only.** Signed forward return at 5, 15, 30 and 60 minutes
  versus the matched same-symbol, same-session-minute control. Report the
  session-clustered confidence interval and the effective sample size, not just
  the point estimate. Kill anything whose interval spans zero at a horizon that
  matters, before spending any execution modelling on it.
- **Stage B, execution.** Only for Stage A survivors: bracket geometry, costs,
  portfolio constraints, net expectancy.

Stage A also has to answer the question the comparison document raised and did
not test: **does the effect survive the one-minute entry lag?** The diagnostic
measures signal-close to horizon-close. The traded version enters at the next
bar. On a breakout, that minute is where most of the move happens. The gap
between the +5 bps diagnostic and the −1.6 bps traded gross P&L in the August 28
cohort is largely that lag plus path dependence, and nobody has decomposed it.

**Acceptance:** for each arm, a matched-control effect at each of four horizons
with a session-cluster interval, plus the same measurement computed from the
next bar's open.

### Step 5: stop testing candle shapes; add information

This is the step that determines whether the project has a future. Three
candidates, ranked by expected effect size per unit of implementation cost, and
all three are outside the current grammar.

1. **Overnight index premium.** Buy SPY or QQQ near the close, exit near the
   next open. One trade per day, round trip cost 2-3 bps, and the expected move
   is measured in tens of basis points rather than single digits, which makes it
   detectable in tens of sessions rather than hundreds. It needs daily bars and
   an MOC/MOO order path, nothing else. `session.entries_regular_session_only`
   currently forbids it. This is the single highest-value experiment available
   and it is close to free.
2. **Intraday momentum from the session's own return.** The return from the open
   through a fixed point in the session, predicting the return to the close. It
   needs one new grammar primitive (return since session open at time T) and a
   close-auction exit. Effect sizes reported in the literature are an order of
   magnitude larger than 5 bps.
3. **Relative value between two ETFs with a shared factor.** SPY-VTI, XLK-SMH,
   TLT-IEF. This requires a genuine two-leg contract, which is real
   engineering, but it removes the market factor that currently dominates every
   result and makes the 24-symbol universe into something closer to independent
   bets rather than one bet held 24 ways.

Supporting data work that unlocks all three: move to the SIP feed for
consolidated volume and a real VWAP, add prior-session levels and the overnight
gap to the causal feature contract, and add an economic and earnings calendar
so sessions can be stratified rather than pooled.

**Acceptance:** each new mechanism registered as a preregistered hypothesis with
a stated counterparty (who is on the other side and why they accept the loss),
an expected effect size, and a power calculation showing the required sample
**before** any data is touched.

### Step 6: state the research budget honestly

Compute, for every registered hypothesis, the minimum detectable effect at the
planned sample under the protocol's own multiple-testing correction. Publish it
next to the hypothesis. If the MDE is 15 bps and the hypothesis predicts 5 bps,
do not run it. That single discipline would have prevented every experiment in
this repository's history from being run in the form it was run.

---

## 7. What not to do

- **Do not loosen the cost model to produce positive results.** Fix it with
  measurement. The difference matters: the goal is a number that is right, not a
  number that is favourable. Note that Step 1 will almost certainly lower the
  cost, and that is fine precisely because it is measured.
- **Do not remove the stress veto.** Reparameterise it so it is reachable.
- **Do not widen stops to gain admission.** That is how 79% time exits happened.
- **Do not add variants.** Twelve arms of which nine are distinct, drawn from
  one information source, is already too many tests for the available sample.
  Fewer, larger, better-motivated hypotheses.
- **Do not treat a paper-trading pass as evidence.** Alpaca's paper simulator
  explicitly excludes market impact, queue position and latency
  ([paper trading documentation](https://docs.alpaca.markets/us/docs/paper-trading)).
  It can validate plumbing. It cannot validate execution economics.

---

## 8. The short answer

Every edge is negative because a 17 bps cost constant, which nobody measured,
is charged against gross effects of 2 to 7 bps, which nobody can measure at the
available sample size, on strategies that all read the same one-minute candle
shapes from 2% of the market's volume, on 24 instruments that are one bet.
On top of that, a stress policy requiring an 83 bps stop that the grammar
cannot produce refuses 100% of signals before any of it is even tested.

Fix the cost constant and the stress arithmetic, and the results move from
"definitively negative" to "indistinguishable from zero." That is progress and
it is not an edge. The edge has to come from Step 5: information the system does
not currently have.

---

## References

- Bailey, Borwein, López de Prado and Zhu, *The Probability of Backtest
  Overfitting* (2014). [SSRN 2326253](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)
- Harvey, Liu and Zhu, *… and the Cross-Section of Expected Returns*,
  Review of Financial Studies (2016). On why a t-statistic of 2 is an
  inadequate hurdle after multiple testing.
- Gao, Han, Li and Zhou, *Market Intraday Momentum*, Journal of Financial
  Economics (2018). The first-half-hour to last-half-hour effect referenced in
  Step 5.
- Bogousslavsky, *The Cross-Section of Intraday and Overnight Returns*,
  Journal of Financial Economics (2021). On the overnight/intraday
  decomposition referenced in Step 5.
- Alpaca, [paper trading](https://docs.alpaca.markets/us/docs/paper-trading),
  [real-time stock pricing data](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data),
  [market data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq).
