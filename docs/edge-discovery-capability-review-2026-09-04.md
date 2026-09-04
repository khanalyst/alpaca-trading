# Can this system find a positive edge? Capability review at `6988bdc`

> **Superseded by** [trader-review-and-grading-2026-09-04.md](trader-review-and-grading-2026-09-04.md).
> That note corrects this one's emphasis: the gate stack is downstream of a
> broken trade construction, not the primary defect. The arithmetic here
> stands; the ranking does not.

- **Reviewed commit:** `6988bdc` (`Raise shadow replay memory limit`), tree state only
- **Perspective:** discretionary intraday trader and quantitative research reviewer
- **Method:** every claim below was read in Markdown *and* verified against the
  executing code path; file and line references are given so each is checkable
- **Status:** review only. No behaviour, config, threshold, or gate was changed.

---

## 1. Verdict

The engineering is genuinely first-rate. The evidence discipline (point-in-time
availability, sealed qualification windows, chronological splits, content-
addressed proofs, replay epochs, durable online FDR) is stronger than most
institutional research stacks.

But the question asked was not "is this rigorous". It was "can this find an
edge". The answer at `6988bdc` is **no, and not because the market lacks edges
in this space, but because five independent properties of the system each on
their own reduce the discoverable set to approximately empty.** Four of the
five are configuration and calibration problems, not architecture problems, and
are cheap to fix. One is a hypothesis-space problem and is expensive.

The uniformly negative results are not a market finding. They are the
predictable output of the current parameterisation. The system is currently
incapable of returning a positive result even if a strong edge were placed
directly in front of it.

Ranked by damage:

| # | Blocker | Class | Effort to fix |
| --- | --- | --- | --- |
| B1 | Cost model overcharges this universe by roughly 34x | Calibration | Low |
| B2 | Stressed-cost geometry forces an 83.3 bps stop on every trade | Policy | Low |
| B3 | Significance stack requires a Sharpe of 10 to 20 to pass | Statistics | Medium |
| B4 | Hypothesis space contains twelve textbook patterns with no plausible alpha | Research | High |
| B5 | Data is an IEX-only partial tape, and 210 forward sessions gate authorisation | Data | Medium |

B1 and B2 explain the *negative* P&L. B3 explains why nothing would be
*accepted* even if P&L were positive. B4 explains why there is probably nothing
there to find in the first place. B5 explains why the measurements are made on
a tape that is not the market.

---

## 2. B1: The cost model charges roughly 34x the real cost of this universe

### What the code does

`research/costs.py:619` defines the per-side charge:

```
per_side_bps = spread_bps / 2 + slippage_bps
```

`research/costs.py:665` charges it on both legs and adds fees on both sides.
With the shipped `config.yaml` values (`spread_bps: 4.0`, `slippage_bps: 6.0`,
`fee_bps: 0.5`):

```
per side      = 4/2 + 6            =  8.0 bps
round trip    = 8 x 2 + 0.5 x 2    = 17.0 bps
```

**Every simulated trade pays 17 bps.**

### Why that is wrong for this universe

The universe (`config.yaml`) is 24 of the most liquid ETFs listed. SPY quotes a
one-cent spread on a roughly 600 dollar price, which is 0.17 bps. Half-spread
is 0.08 bps. Marketable slippage on retail-scale size is a fraction of a basis
point. Alpaca charges no equity commission. A realistic round trip on SPY is
**about 0.5 bps**. Even the widest name in the list quotes a penny on a 30 to 40
dollar price, roughly 3 bps, so a 17 bps round trip is a large multiple of the
true cost for every symbol in the universe.

The cost block is labelled `provenance: "shipped_conservative_v1_plus_25bps_stress"`.
It is not conservative. It is an assumption that is wrong by more than an order
of magnitude, and it is spent as if it were measured.

### The repository already proved this

`docs/measured-cost-model-2026-08-30.md` is unusually honest and states the
finding directly:

> "execution drag came out at 0.162 to 0.179R across nine variants spanning five
> families with different lookbacks, thresholds, holds and symbols. A range that
> tight across that much variety is a constant divided by a constant, not a
> property of the strategies."

and, crucially:

> "the measured model produces a 3 bps round trip against the shipped 17 bps,
> and execution drag falls from 0.136R to 0.018R, a 7.5x reduction"

and the list of families that are **positive before costs**:

> "On the real cohort that is range expansion (+0.085), trend pullback (+0.097,
> +0.146 at a 90-bar hold) and ORB (+0.024, +0.033)."

Read that again. Three families showed positive pre-cost reference R. The
shipped cost model charges 0.136 to 0.179R of drag. That is precisely the
arithmetic that turns +0.097R into a loss, and it is an assumption, not a
measurement.

### The fix exists and is not wired in

`research/quote_costs.py` fits a real per-symbol, per-half-hour spread schedule
from the recorded quote corpus. It is imported only by `research/cost_rerun.py`
(a diagnostic comparison runner) and by `research/stressed_cost_calibration.py`
(disabled by default). `config.yaml` carries no `costs.vehicles.equity`
override, so the authorising lanes use the 4/6/0.5 constants. The measured path
can currently only produce a report; it cannot price a proof.

**This is the single highest-value change available.** The measurement code is
already written, tested, and documented. It is simply not connected to the lane
that decides.

---

## 3. B2: Every trade is forced to an 83.3 bps stop, which destroys the trade's identity

### The arithmetic

`agent/contracts/risk_geometry.py:33` implements:

```
required_stop_distance_bps = scenario_bps / max_cost_to_risk_ratio
```

With the shipped `stressed_cost_scenario_bps: 25.0` and
`max_stressed_cost_to_risk_ratio: 0.30`:

```
25 / 0.30 = 83.33 bps
```

`effective_stop_floor_bps` takes `max(MIN_STOP_DISTANCE_BPS=30, 83.33) = 83.33`,
and `effective_stop_distance` widens any authored stop up to that floor. Both
`research/factory_core.py:765` and `research/edge_discovery_core.py:1028` apply
it, and when the floor binds, a `fixed_r` target is recomputed at
`distance x target_r`, so the default 2R target lands **166.7 bps** away.

### Four consequences, each independently serious

**(a) The `stop_atr` search axis is collapsed to a single point.** The grammar
bounds `stop_atr` at `(0.2, 10.0)` (`agent/contracts/rule.py:_BOUNDS`). On
liquid ETFs, intraday ATR is typically 10 to 40 bps, so any `stop_atr` below
roughly 2 to 8 produces an authored stop under 83.3 bps and is overwritten. The
LLM tuning lane spends its bounded coordinate budget probing an axis where most
values produce a byte-identical trade. Lessons graded on that axis measure
nothing. This is why the repository's own note records that "the factory's own
bounded tuning had to reach `stop_atr = 7` before anything could execute".

**(b) The bracket becomes decorative.** A 166.7 bps target with a 90-bar
(90-minute) hold cap is essentially unreachable on SPY, QQQ, TLT or GLD. So is
an 83.3 bps stop. The repository measured this: "the resulting bracket never
triggers, leaving 71 to 84% time-expiry exits". The strategy being tested is no
longer "breakout with a 1-ATR stop and a 2R target". It is "hold a directional
position for N minutes and exit at the clock". Every hypothesis in the catalogue
is silently converted into the same trade.

**(c) The measured R distribution is compressed and cost-dominated.** With the
floor binding, net R per trade is approximately:

```
R = (captured move in bps - 17) / 83.33
```

Cost alone is `17 / 83.33 = 0.204R` on **every** trade. The protocol's own
`RETIREMENT_MIN_USEFUL_R = .05` (`research/gates.py:62`) sets the minimum useful
edge at 0.05R. The system therefore demands that a strategy overcome a fixed
0.204R toll in order to deliver a 0.05R edge, a hurdle four times the target.

**(d) Position sizing is capped, not sized.** With `risk_per_trade_pct: 0.5` and
an 83.3 bps stop, implied notional is `0.005 / 0.008333 = 60%` of equity, against
`max_position_notional_pct: 25.0`. Every trade is size-capped, so realised risk
per trade is roughly 0.21% rather than the configured 0.5%.

### The 0.60 counterfactual is direct empirical confirmation

`docs/cost-risk-counterfactual-research-findings-2026-08-25.md` reports:

> "At `0.30`, all 1,775 strategy signal opportunities were rejected by the
> stressed-cost gate. There were no trades."
>
> "At `0.60`, 513 trades were executed. Their pooled mean was `-0.541550R`."

At `0.60` the forced stop is `25/0.60 = 41.67 bps`, so the cost toll is
`17 / 41.67 = 0.408R` per trade. The observed mean of **-0.5416R** is that toll
plus a modest negative gross. The reported result is very close to *the cost
model measuring itself*. It is not evidence that the strategies are bad. It is
evidence that the cost assumption is dominant.

The report's own conclusion is the correct one and worth quoting: retaining
`0.30` "does not prove that `0.30` has positive expectancy, because `0.30`
produced no trades."

### Documentation drift found

Two documents no longer describe HEAD:

- `research/protocol.md` states a 30 bps-floor trade "is vetoed" by the stressed
  cost control. Since `43891ff` (2026-09-01) the research path **widens the stop**
  instead, so the veto in `research/costs.py:check_stressed_cost_plan` is a
  backstop that no longer fires on that path. The observable behaviour changed
  from "no trades" to "trades with alien geometry", which is a materially
  different failure and is not described.
- `docs/measured-cost-model-2026-08-30.md:117` states "the gate does not widen
  stops for a candidate". That was true when written and is false at HEAD.

Neither is a code defect, but both would mislead the next person diagnosing a
zero-trade or all-time-expiry result.

---

## 4. B3: The significance stack requires a Sharpe between 10 and 20

This is the part that means **no realistic edge can be accepted even if B1, B2,
B4 and B5 were all fixed.**

### The stack

`research/gates.py:63` requires **30 checks to pass simultaneously**
(`GATE_REQUIRED_CHECKS`). Three of them are significance hurdles applied in
series to the same underlying statistic:

1. **Family-local Benjamini-Yekutieli**
2. **Cycle-global Benjamini-Yekutieli** plus a frozen-dependence-cluster veto
3. **Durable online LORD++ (`shadow-confirmation-v6`)** on the confirmatory window

BY is `benjamini_hochberg` multiplied by the harmonic number
(`research/stats.py:1239`). LORD++ allocates
`alpha_t = W0 x gamma_t` with `gamma_i = 1/(i(i+1))` and `W0 = alpha/2 = 0.025`
(`research/factory_ledger.py:241` and `:258`), and the decision is
`p <= allocated` (`research/factory_ledger.py:1868`).

The authorising statistic is a paired cluster sign-flip permutation test on
session clusters (`research/stats.py:1162`, `research/gates.py:1055`), with the
protocol floor at **30 clusters** (`PROTOCOL_*_MIN_CLUSTERS = 30`).

### What each hurdle demands, in trader units

For a sign-flip test on `k` clusters, the attainable z is bounded by
`sqrt(k) x r / sqrt(1 + r^2)` where `r` is the ratio of per-session mean delta
to per-session standard deviation. At `k = 30`, four trades per session and a
per-trade R standard deviation of 1.0:

| Hurdle | Required raw p | Required net edge | Implied annual Sharpe |
| --- | --- | --- | --- |
| Nominal 0.05, no correction | 5.0e-02 | 0.157 R/trade | ~5.0 |
| LORD++ confirmatory, first ever test | 1.25e-02 | 0.224 R/trade | ~7.1 |
| Global BY, 12 variants in cycle | 1.34e-03 | 0.328 R/trade | ~10.4 |
| Global BY, 44 variants in cycle | 2.60e-04 | 0.409 R/trade | ~13.0 |
| LORD++ confirmatory, tenth test | 2.27e-04 | 0.417 R/trade | ~13.2 |
| LORD++ confirmatory, twentieth test | 5.95e-05 | 0.494 R/trade | ~15.7 |

The 44-variant row is not hypothetical: the cost-counterfactual report records
"one frozen set of 44 variants across 11 families".

**Even the most generous hurdle in the entire pipeline, a single uncorrected
test at alpha 0.05, requires an annualised Sharpe of 5 at the protocol's
minimum data.** The binding hurdles require 10 to 20. Renaissance Medallion is
reported in the 2 to 3 range on public estimates. The gate is calibrated to a
region that does not exist.

### The other side of the same coin: data required

Inverting the question, how much held-out data would a *realistic* edge need?

| True edge | Sessions for p<=0.05 | Sessions for global BY (44) | Sessions for LORD++ t=10 |
| --- | --- | --- | --- |
| 0.02 R/trade | 1,694 | 7,539 | 7,696 |
| 0.05 R/trade | 273 | 1,216 | 1,242 |
| 0.10 R/trade | 70 | 313 | 320 |
| 0.20 R/trade | 20 | 87 | 89 |

252 sessions is one trading year. A 0.05R per-trade net edge, which is exactly
the protocol's own stated minimum useful edge, needs **roughly 1,216 held-out
sessions, about 4.8 years of held-out data**, to clear the global BY hurdle. The
corpus split is `fit_fraction = .7` after a 20% sealed qualification window
(`research/strategy_factory.py:2995`, `:4010`), so held-out is roughly 24% of the
corpus. That implies a **twenty-year corpus** to authorise the minimum edge the
protocol says is worth having.

### Joint probability for a genuinely excellent strategy

At 30 clusters, for a strategy with a true 0.20R per-trade net edge, i.e. an
annualised Sharpe of about 6.3, a strategy far better than anything in this
signal class realistically produces:

```
significance 0.071  x  walk-forward 2-of-3 0.65  x  95% LCB 0.651  x  qualification 0.881
  = 0.026
```

**A 2.6% chance of acceptance.** For a realistic 0.05R edge it is roughly 0.15%.
And that estimate covers only four of the thirty required checks.

### Where the design reasoning went wrong

The individual components are each defensible and correctly implemented. The
error is compositional, and it is a classic one:

- BY is applied for arbitrary dependence, then LORD++ is applied on top for
  cumulative control, then a frozen-cluster veto on top of that. These are
  three corrections for substantially the same multiplicity. Each is
  individually justified; multiplying them is not, and nothing in the protocol
  computes the joint operating characteristic.
- LORD++ wealth decays as `1/t^2` and, with no discovery ever made, **never
  recovers**. The system becomes strictly harder to satisfy every cycle it runs,
  independent of evidence quality. After roughly 224 tests it is arithmetically
  dead: `_confirmatory_iterations` (`research/live_shadow_ingest.py:61`) would
  need more than `MAX_CONFIRMATORY_ITERATIONS = 2,000,000` permutation draws to
  resolve a p-value that small, and the run returns
  `confirmatory_resolution_exhausted`. The code correctly detects this. The
  design does not account for it.
- The 30-cluster floor was chosen for *validity* (clustered inference needs
  clusters). It was never checked for *power* against the correction stack that
  sits on top of it. The system computes an MDE report
  (`clustered_mde_power_report`) but treats it as "descriptive and
  non-authorizing" (`docs/edge-audit-remediation-2026-09-01.md`), so the one
  diagnostic that would have caught this is explicitly barred from influencing
  anything.

### The false-negative rate is nowhere controlled

Thirty required checks, three stacked FDR corrections, and a decaying online
budget give this system extraordinary control of Type I error and **no stated
control of Type II error at all**. For a discovery system, an uncontrolled false
negative rate is the more expensive failure: a false positive costs one demoted
paper candidate under a lifecycle that already demotes on drift, while a false
negative costs the entire edge, permanently and silently, and is recorded as
"the market has no edge here".

---

## 5. B4: The hypothesis space contains no plausible alpha

`agent/contracts/rule.py:34` defines the complete, closed signal vocabulary:

```
opening_range_breakout, opening_range_fade, momentum_continuation,
mean_reversion, trend_pullback, volatility_breakout, volume_breakout,
vwap_reversion, vwap_trend, range_expansion, opening_drive,
cross_sectional_residual
```

Reading the predicates at `agent/contracts/rule.py:1552-1690`, all twelve are
functions of one symbol's own 1-minute OHLCV prefix. Eleven are textbook
retail technical-analysis patterns of 1990s vintage. The twelfth,
`cross_sectional_residual`, is the only one with a genuine structural basis
(relative value versus SPY), and the grammar deliberately makes it a
**directional single-leg** trade rather than a hedged spread, which discards the
mechanism that would make it work.

The protocol is explicit that this is closed: "No lane may extend the signal
vocabulary... the feature computation itself is code that research never
generates or rewrites."

### What this means for the LLM lane

The LLM does not invent strategies. `research/llm_strategy.py:95-170` shows the
actual contract. Discovery selects one of the twelve families and sets numbers.
Tuning may change **only values of fields the root already carries**, may not
change `family` or `schema`, must change exactly one field in coordinate phase
and exactly two in interaction phase, and is limited to a 20% local
neighbourhood per move plus `MAX_NOVEL_TUNING_VALUES = 8` novel numbers per
lineage. The prompt states it plainly: "The signals themselves... are fixed code
that you are tuning, not designing."

So the search space is exactly: **12 families x a bounded numeric grid**. That
is the complete set of hypotheses this system can ever entertain.

From a trading standpoint that space is empty. Opening-range breakouts and VWAP
reversion on SPY and QQQ 1-minute bars are the most comprehensively arbitraged
patterns in existence. They have been mined by every retail platform, every
prop desk, and every market maker for two decades. If a stable, cost-surviving
version of any of them existed on the 24 most liquid ETFs listed, it would have
been competed away long before this system recorded its first bar.

Note the asymmetry this creates: the repository's own measurement found three
families positive *before* costs (range expansion, trend pullback, ORB). That is
consistent with weak, real, but tiny microstructure effects on the order of a
few basis points, which is exactly what one should expect. Those effects are
real and they are smaller than the assumed cost. Fixing B1 makes them
measurable. It does not make them large.

### What is conspicuously absent

Nothing in the vocabulary can express any of the mechanisms that actually
produce durable intraday edge:

- order-flow or quote imbalance, depth, and queue dynamics
- cross-asset lead-lag (ES futures leading SPY, sector leading constituent)
- ETF creation/redemption and premium/discount to NAV
- index rebalance and month-end flow
- overnight gap and prior-session reference levels (the grammar re-derives its
  session from the bars' own local dates and cannot see yesterday)
- event conditioning: macro prints, earnings, FOMC (the protocol defers this
  explicitly)
- options positioning, gamma, and dealer hedging flow
- breadth, dispersion, and correlation regime
- time-of-day interactions beyond a flat entry window

The protocol lists prior-session, multi-timeframe, and event conditioning as
"remaining extension boundaries" requiring explicit context. Those boundaries
are where the edges live.

---

## 6. B5: The tape is partial, and authorisation is 210 sessions away

### IEX is roughly 2% of consolidated volume

`config.yaml` sets `data_feed: "iex"`, and `deploy/recorder_market.py:42` pulls
1-minute bars from that feed. The protocol acknowledges the limitation: "IEX is
a limited venue view rather than the consolidated SIP tape, so coverage can be
sparse and its evidence is not interchangeable with a paid-feed corpus."

That acknowledgement understates the effect on the specific signals in use:

- `volume_breakout` and `range_expansion` compare current volume and range to a
  trailing average. On an IEX-only tape both quantities are dominated by IEX's
  fluctuating share of a minute's activity, not by real market activity. These
  two families are measuring venue routing noise.
- `volatility_breakout` gates on a `compression_bps` filter derived from bar
  highs and lows. An IEX-only bar range is systematically narrower than the
  consolidated range, so compression is detected spuriously and the filter fires
  in the wrong regime.
- `opening_range_breakout`, `opening_range_fade` and `opening_drive` set their
  levels from IEX opening-window highs and lows. Those are not the levels other
  participants see, so breakouts trigger at prices with no market meaning.
- `vwap_reversion` and `vwap_trend` anchor on an IEX volume-weighted price,
  which is not session VWAP. Reversion to the wrong anchor is not the VWAP
  effect.
- Replay stop and target resolution reads bar highs and lows. A narrower bar
  range under-triggers **both** brackets, which compounds B2's time-expiry
  problem and biases the measured exit distribution.

Nine of twelve families are materially degraded by the feed choice, and the
degradation is not mean-zero noise; it is directional bias in the feature.

### 210 forward sessions before anything can be authorised

`docs/edge-audit-remediation-2026-09-01.md` states the readiness arithmetic, and
`deploy/scheduler_output.py:207-221` implements it: shadow readiness is
`shadow_selection_sessions + shadow_confirmation_sessions = 30 + 30`, on top of
a 150-session offline requirement. **210 sessions, roughly ten calendar months
of continuous forward recording**, before a candidate can reach the
authorisation boundary at all.

Historical backfill is explicitly non-authorising
(`source_mode: historical_backfill`, marked `diagnostic_historical_backfill`,
"excluded from authorizing statistics"). So that ten months cannot be
shortened with history; it has to be waited out in real time. If forward
recording began recently, the correct current status is not "no edge found" but
"insufficient data to have looked".

---

## 7. What the system gets right

This should not be lost in the critique. These are genuinely excellent and
should be preserved through any remediation:

- **Point-in-time discipline.** Availability bounded by `max(event, as_of, observed_at)`,
  next-bar entry, no same-bar fills, delayed OHLC never backfilling an earlier
  entry. This is the failure mode that invalidates most backtests, and it is
  handled properly.
- **The clock-matched null control** in `research/signal_quality.py:44-60`. The
  reasoning that a uniformly-drawn intraday null "measures the difference between
  two clocks rather than the value of the predicate" is a subtle and correct
  insight that most professional research misses.
- **The sealed qualification window**, released once, for one preselected
  candidate. Correct design.
- **One shared cost model** across runtime, factory, IBR replay and null controls,
  with the invariant that a model expecting a cost the runtime would refuse
  fails closed. The model's *values* are wrong; its *plumbing* is right, which
  is why B1 is a cheap fix.
- **Honest self-documentation.** `docs/measured-cost-model-2026-08-30.md`
  diagnosed B1 and B2 accurately and refused to overclaim. The
  cost-counterfactual report explicitly declined to conclude that `0.30` has
  positive expectancy on zero trades. That intellectual honesty is rare.
- **Replay epoch quarantine.** Superseded evidence stays readable but cannot
  authorise. Correct.

The problem is not that this team cannot build a research system. It is that
the safety machinery was tuned to a level where it consumed the research.

---

## 8. Remediation, in priority order

### P0: Make the measurement honest (unblocks everything else)

1. **Wire the measured cost model into the authorising lane.**
   `research/quote_costs.py` already fits per-symbol, per-half-hour spread and
   depth from the recorded corpus. Set `costs.vehicles.equity` from that
   schedule, or make `measured_cost_resolver` the default resolver for equity
   replay with the shipped constants as an explicit fallback only when a cell
   is under-covered. Keep the conservative percentile choice (`p75`) so this is
   defensible, not optimistic. Expected effect, from the repository's own
   measurement: execution drag falls from roughly 0.136R to roughly 0.018R.

2. **Separate the stress scenario from the stop geometry.** The stressed-cost
   control conflates two different questions: "would this trade survive a cost
   shock" (a risk question) and "how wide must the stop be" (a strategy
   question). Forcing the second from the first destroys the hypothesis under
   test. Options, in order of preference:
   - Apply the stress test as a **portfolio-level admission check** on realised
     cost-to-risk, not as a per-signal geometry constraint.
   - Or re-derive `stressed_cost_scenario_bps` from the measured p95/p99 spread
     per symbol and half-hour, which the schedule already computes. A 25 bps
     shock on SPY is not a stress scenario; it is a different asset class.
   - Or, at minimum, raise `max_stressed_cost_to_risk_ratio` so the implied
     floor sits below typical intraday ATR, and record every run's
     `stop_floor_binding` rate as a first-class result. If that rate is above a
     few percent, the result is about the floor, not the strategy.

3. **Re-run the frozen 44-variant cohort under (1) and (2).** The cohort and
   the runner (`research/cost_rerun.py`) already exist. This is the cheapest
   possible test of whether B1 and B2 alone flip the sign, and the three
   pre-cost-positive families give a concrete prediction to check against.

### P1: Recalibrate the acceptance stack to a reachable region

4. **Preregister a target minimum detectable effect and derive the gates from
   it**, rather than choosing gates and discovering the MDE afterwards. Decide
   the smallest edge worth deploying (the protocol says 0.05R), decide the
   power required (say 80%), and let those two determine the required sessions
   and alpha. At present the system has an implicit MDE near 0.4R that nobody
   chose.

5. **Stop stacking three corrections for one multiplicity.** Pick the one that
   matches the actual decision being made. A defensible structure: BY within the
   cycle for candidate *selection*, and a single online allocation for
   *authorisation*, with the selection correction not also charged against the
   confirmatory p. Compute and preregister the joint operating characteristic.

6. **Fix the LORD++ wealth decay.** With `W0 = alpha/2` and no discovery ever,
   `alpha_t` falls as `1/t^2` forever and the system is provably dead at test
   224. Either scope the sequence per research campaign with a documented reset
   boundary, or adopt a construction with wealth recovery (SAFFRON or
   alpha-investing with an explicit floor). A discovery system whose acceptance
   threshold tightens monotonically toward zero regardless of evidence quality
   will always converge to zero discoveries.

7. **Promote the MDE and power report from "descriptive" to a required
   precondition.** Refuse to spend online alpha on a test that is underpowered
   for the preregistered minimum useful edge. This costs nothing in rigour and
   stops the budget being burned on tests that could not have passed.

### P2: Fix the tape

8. **Buy the SIP feed.** Alpaca's paid Algo Trader Plus tier is a rounding error
   against the engineering already invested here. Nine of the twelve families
   are measuring venue artefacts on IEX. No amount of statistical rigour repairs
   a biased feature.

9. **Until then, restrict the catalogue to feed-robust families.** Suspend
   `volume_breakout` and `range_expansion` (volume and range share are pure IEX
   artefacts) and flag opening-range levels as feed-dependent. Better to search
   a smaller honest space than a larger corrupted one.

10. **Report forward-session progress against the 210-session readiness bar in
    every cycle summary**, so "not yet measurable" is never reported as, or
    mistaken for, "no edge exists".

### P3: Give the search something worth finding

11. **Extend the grammar toward mechanisms rather than patterns.** The
    architecture supports this cleanly: families are appended, never reordered,
    and each is a bounded data-only predicate. The highest expected-value
    additions, in order:
    - **Cross-asset lead-lag.** The recorder already captures all 24 symbols
      synchronously. SPY leading sector ETFs, and SMH leading XLK, at the
      1-minute horizon is a real and documented effect.
    - **Overnight gap and prior-session levels.** Requires prior-session replay
      context, which the protocol already identifies as a bounded extension.
      Gap fade and prior-day high/low interaction are among the most durable
      intraday effects that survive costs.
    - **Make `cross_sectional_residual` hedged.** A directional single-leg
      residual bet is not the strategy; the spread is. This is the one family
      with genuine structural justification and it is currently expressed in the
      form least likely to work.
    - **Time-of-day interaction as a first-class axis** rather than a flat entry
      window. The open, the European close, the 15:00 and 15:45 rebalance
      windows, and the close have completely different dynamics.

12. **Add a deliberate positive control.** Inject a synthetic signal with a
    known, tunable edge into the corpus and confirm the pipeline recovers it at
    the expected effect size. Every claim in this review would have surfaced
    within a day of such a test existing. A discovery system that has never
    demonstrated it can find a known edge has not been validated as a discovery
    system.

---

## 9. The one thing to do first

Run the frozen 44-variant cohort through `research/cost_rerun.py` with the
measured schedule and with `max_stressed_cost_to_risk_ratio` relaxed so the
83.3 bps floor does not bind, and compare the reference-R decomposition. The
repository's own measurement predicts that range expansion (+0.085), trend
pullback (+0.097 and +0.146) and ORB (+0.024, +0.033) are positive before costs
and that drag falls by roughly 7.5x. If that reproduces, the negative results
to date are an artefact of the cost and geometry assumptions, and the research
programme is alive. If it does not reproduce, then B4 is the binding constraint
and the effort belongs in the grammar, not the gates.

Either way the answer arrives from evidence that already exists, without
weakening a single safety property.

---

## Appendix: Verification index

| Claim | Verified at |
| --- | --- |
| 17 bps round trip | `research/costs.py:619,665`; `config.yaml` costs block |
| Measured model gives ~3 bps; drag 0.136R -> 0.018R | `docs/measured-cost-model-2026-08-30.md` |
| Measured path not wired to authorising lane | imports of `research/quote_costs.py`; no `costs.vehicles` in `config.yaml` |
| Forced stop = 25/0.30 = 83.33 bps | `agent/contracts/risk_geometry.py:33,53,64` |
| Widening applied in research lanes | `research/factory_core.py:765`; `research/edge_discovery_core.py:1028` |
| Target recomputed when floor binds | `research/factory_core.py:783` |
| 30 required checks | `research/gates.py:63` |
| Cluster floors of 30, trade floors 100/150 | `research/gates.py:110-118` |
| BY = BH x harmonic number | `research/stats.py:1239` |
| LORD++ `W0=alpha/2`, `gamma=1/(i(i+1))` | `research/factory_ledger.py:241,258,305` |
| Decision is `p <= allocated` | `research/factory_ledger.py:1868` |
| Confirmatory p is the sign-flip p | `research/edge_discovery_core.py:1623,1782`; `research/live_shadow_ingest.py:1029` |
| Permutation floor `1/(iters+1)`, iters auto-scaled, dead at t=224 | `research/stats.py:254`; `research/live_shadow_ingest.py:61,44,45` |
| Twelve closed families | `agent/contracts/rule.py:34` |
| LLM cannot add signals; one field, 20% local, 8 novel values | `research/llm_strategy.py:105,127,141`; `research/strategy_factory.py:111` |
| IEX 1-minute bars | `config.yaml`; `deploy/recorder_market.py:42,50` |
| 210-session readiness | `deploy/scheduler_output.py:207-221`; `docs/edge-audit-remediation-2026-09-01.md` |
| Corpus split 20% sealed, then 70/30 | `research/strategy_factory.py:4010,2995` |
| Zero trades at 0.30; -0.5416R at 0.60 | `docs/cost-risk-counterfactual-research-findings-2026-08-25.md` |
