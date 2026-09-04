# Trader's review and grading at `6988bdc`

- **Supersedes:** `edge-discovery-capability-review-2026-09-04.md`, whose
  emphasis was wrong. See section 2.
- **Reviewed commit:** `6988bdc`, tree state only
- **Method:** read as a trading system first (what is the trade, can I get the
  fill, what happens to the position), then as a research system. Every claim
  checked against executing code. Handoff notes deliberately not consulted.
- **Status:** review only. Nothing changed.

---

## 1. The one-line answer

**This is an A-grade research apparatus wrapped around an F-grade trade.**

The measurement machinery is excellent and mostly not the problem. The problem
is that the thing being measured is not a trade any competent day trader would
take, and in several places it is not even the trade the strategy authored. The
system is faithfully, rigorously, and reproducibly measuring a broken position.

---

## 2. Correction to the previous review

The previous note concluded that the statistical gate stack was the wall,
requiring a Sharpe of 10 to 20. That was arithmetically right and causally
wrong, and it buried the real problem.

Simulating the actual shipped position on SPY-like dynamics changes the
picture:

| Configuration | 5 bps signal | 10 bps signal | 17 bps signal | 25 bps signal |
| --- | --- | --- | --- | --- |
| **Shipped** (stop 83.3, 2R target, 90 min, 17 bps cost) | -0.145R | -0.084R | -0.000R | +0.095R |
| Costs fixed (3 bps) | +0.024R | +0.083R | +0.165R | **+0.262R** |
| Costs + real stop geometry (25 bps stop, 1R, 30 min) | +0.013R | +0.143R | **+0.312R** | **+0.483R** |

Bold entries clear the roughly 0.2R per trade that the gate stack needs at the
30-cluster floor.

The correct statement is therefore:

> **The shipped configuration mathematically caps attainable per-trade R at
> about +0.1R even for a strong signal, and then the gates demand 0.2R to 0.4R.
> The gates are not the primary defect. The trade construction is. Fix the
> trade and the gates become reachable.**

That is a far better position to be in than the previous note implied. Nothing
in the statistics needs rebuilding first.

---

## 3. Grades

| Dimension | Grade | One-line justification |
| --- | --- | --- |
| Point-in-time and look-ahead discipline | **A** | Next-bar entry, availability bounded by max(event, as_of, observed_at), no delayed backfill. Better than most desks. |
| Anti-overfit machinery | **A** | Sealed qualification window, chronological splits, content-addressed proofs, replay-epoch quarantine. |
| Engineering quality and test coverage | **A** | 108k lines, ~90 test modules, fail-closed everywhere. |
| Documentation honesty | **A-** | Diagnosed its own cost defect accurately and refused to overclaim. Two stale claims and one contradicted design decision (section 5). |
| Statistical calibration | **C** | Each component correct; jointly unpowered at the 30-cluster floor. Reachable once T1 to T3 are fixed. |
| Execution realism | **C-** | Bracket market orders; symmetric cost charged on structurally asymmetric legs. |
| Position sizing and portfolio construction | **D** | Runs at 42% of configured risk and 2 of 3 configured positions, silently. |
| Research/live parity | **D** | Research measures one trade per symbol-session. Live has no per-session cap. |
| Hypothesis space | **D** | Twelve textbook single-symbol bar patterns on the 24 most arbitraged ETFs listed. |
| Search efficiency (LLM and deterministic) | **D-** | Most rungs of the preregistered tuning ladders cannot fire or cannot differ. |
| Data foundation | **D** | IEX is a partial tape; nine of twelve families read biased features from it. |
| Reward geometry (exits) | **F** | The take-profit is hit 0.0% of the time. |
| Cost model | **F** | Charges roughly 34x the real round trip of this universe. |
| Risk geometry (stops) | **F** | A constant overrides every strategy's authored stop on most of the universe. |

**Overall: D+.** Weighted toward the trade, because the trade is what makes
money. Weighted toward the apparatus it would be a B+.

---

## 4. Findings, in a trader's order

### T1. The stop you authored is not the stop you get, and on 10 of 24 symbols the stop parameter does nothing at all

`agent/contracts/rule.py:1753`:

```
distance = max(atr * spec["stop_atr"], close * MIN_STOP_DISTANCE_FRACTION)
```

`_atr` (`agent/contracts/rule.py:1361`) is a simple mean true range over
`atr_period` **one-minute bars**, default 14. That is a 14-minute ATR, not a
daily one. On a 15% annualised-vol name it is about **7.7 bps**.

Then `agent/contracts/risk_geometry.py:33` overrides it:

```
floor = max(30 bps, scenario_bps / max_cost_to_risk_ratio) = max(30, 25/0.30) = 83.33 bps
```

`stop_atr` is bounded at 10.0 (`_BOUNDS`). So the authored stop and the floor
compare as follows:

| Symbol | Ann. vol | 14-min ATR | stop_atr=1 | stop_atr=10 (max) | Floor binds until |
| --- | --- | --- | --- | --- | --- |
| SMH | 30% | 15.3 bps | 15.3 | 153.1 | stop_atr 5.4 |
| IWM | 22% | 11.2 bps | 11.2 | 112.3 | stop_atr 7.4 |
| QQQ | 20% | 10.2 bps | 10.2 | 102.1 | stop_atr 8.2 |
| XLI | 17% | 8.7 bps | 8.7 | 86.8 | stop_atr 9.6 |
| **SPY** | 15% | 7.7 bps | 7.7 | **76.6** | **never** |
| **VTI, EFA, XLV** | 15% | 7.7 bps | 7.7 | **76.6** | **never** |
| **DIA, TLT, GLD** | 14% | 7.1 bps | 7.1 | **71.5** | **never** |
| **XLP** | 12% | 6.1 bps | 6.1 | **61.2** | **never** |
| **HYG** | 6% | 3.1 bps | 3.1 | **30.6** | **never** |

**On 10 of the 24 symbols, including SPY itself, no value of `stop_atr` in the
entire grammar can reach the floor. Every setting produces a byte-identical
83.33 bps stop.** On the other 14 the floor binds across roughly 70 to 90% of
the ladder.

What this means in trading terms, and each of these is independently serious:

1. **The risk unit is volatility-blind.** A quiet SPY day and a violent SPY day
   get exactly the same 83.33 bps stop. Every ATR-adaptive property the twelve
   families were designed around is switched off.
2. **Every strategy becomes the same strategy.** Family, lookback, threshold
   and `stop_atr` all vary, and the position that results has an identical
   risk geometry. You are not testing twelve hypotheses. You are testing twelve
   entry timers attached to one fixed trade.
3. **The `stop_atr` search axis is dead.** The deterministic coordinate lane and
   the LLM tuning lane both spend bounded budget probing an axis with one
   distinct outcome, and then grade lessons on the result.
4. **83.33 bps is not a day-trade stop on SPY.** It is roughly 0.83% on an
   instrument whose whole daily range is often 1.0 to 1.5%. No intraday trader
   risks most of a day's range on a 1-minute breakout signal.

### T2. The take-profit is never hit. Not rarely. Never.

Default `target_r` is 2.0, applied to the forced stop, so the target sits
**166.7 bps** away, with `max_hold_bars` default 90.

Simulated on SPY-like dynamics, 200,000 paths:

| True signal edge | Mean R | Target hit | Stop hit | Time exit |
| --- | --- | --- | --- | --- |
| 0 bps | -0.203R | **0.0%** | 5.8% | 94.1% |
| 10 bps | -0.086R | **0.0%** | 3.8% | 96.1% |
| 25 bps | +0.096R | **0.1%** | 1.8% | 98.0% |

A 1.66% move in 90 minutes on SPY is a tail event. The bracket is decoration.
94 to 98% of trades resolve at the clock, which matches the repository's own
observed 71 to 84% time expiry.

Consequences a trader would care about:

- **There is no reward structure.** Exits are a coin flip on where price
  happened to be when the timer expired, minus a fixed cost.
- **The R denominator is 83.33 bps while the numerator is a 90-minute drift of
  perhaps 5 to 20 bps.** Every outcome is compressed into a narrow band around
  -0.2R, which is exactly the cost toll. That is why every reported variant has
  a similar, small, negative mean: they are all measuring the same constant.
- **The win rate is meaningless.** At 44% "wins", most winners are time exits a
  few basis points above entry, not target hits. There is no positive
  expectancy structure to find in that distribution.

I checked whether the existing grammar can escape this. Holding the forced stop
and sweeping `target_r` and `max_hold_bars` across their ladders, with a
genuine 10 bps signal edge:

| target_r | Target | Hold | Mean R | Win % | Target hits |
| --- | --- | --- | --- | --- | --- |
| 2.00 | 166.7 bps | 90 | -0.080 | 44.1% | 0.0% |
| 0.50 | 41.7 bps | 45 | -0.102 | 42.8% | 25.1% |
| 0.25 | 20.8 bps | 30 | -0.131 | 51.9% | 50.4% |

**Every combination is negative under the shipped 17 bps cost.** There is no
escape inside the current parameters. The cost is the binding term, not the
target. Change the cost to the measured 3 bps and the same rows turn +0.085,
+0.065, +0.036. That is the whole story in three numbers.

### T3. The cost model charges roughly 34x the real cost of this universe

`research/costs.py:619` and `:665` give `per_side = spread/2 + slippage = 8 bps`,
so **17 bps round trip**. The universe is 24 of the most liquid ETFs listed.
SPY's real round trip, penny spread on a 600 dollar price with no commission,
is about **0.5 bps**.

The repository already measured this and wrote it down in
`docs/measured-cost-model-2026-08-30.md`:

> "execution drag came out at 0.162 to 0.179R across nine variants spanning five
> families with different lookbacks, thresholds, holds and symbols. A range that
> tight across that much variety is a constant divided by a constant, not a
> property of the strategies."

> "the measured model produces a 3 bps round trip against the shipped 17 bps,
> and execution drag falls from 0.136R to 0.018R"

And it named the families that are **positive before costs**: range expansion
+0.085, trend pullback +0.097 and +0.146, ORB +0.024 and +0.033.

`research/quote_costs.py` fits the real schedule per symbol and per half-hour.
It is imported only by `research/cost_rerun.py` (diagnostic) and
`research/stressed_cost_calibration.py` (disabled by default). `config.yaml`
carries no `costs.vehicles.equity` override. **The authorising lane still spends
the assumption.**

The 0.60 counterfactual is the proof. At ratio 0.60 the forced stop is 41.67 bps
and the cost toll is `17/41.67 = 0.408R`. The reported outcome was a pooled mean
of **-0.5416R** across 513 trades. That result is very close to the cost model
measuring itself.

### T4. The risk configuration is fiction

`config.yaml` says 0.5% risk per trade, 3 concurrent positions, 50% gross.
Trace what actually happens with the forced 83.33 bps stop:

```
notional required = 0.5% / 0.8333%      = 60.0% of equity
capped at max_position_notional_pct     = 25.0% of equity   (agent/risk.py:126,138)
realised risk per trade = 25% x 0.8333% =  0.208% of equity  (41.7% of configured)
position 3 gross check: 50% + 25% > 50% = REJECTED           (agent/risk.py:768)
```

So the deployed system runs at **0.208% risk, not 0.5%**, and **2 concurrent
positions, not 3**. Neither is reported as a binding constraint anywhere an
operator would look; the cap silently reduces `shares` and the gross check
returns a generic refusal string.

`docs/edge-discovery-upgrade-2026-08-29.md` predicted exactly this: "the cap
binds for stop distances below roughly 2% of price. Tight-stop variants can
therefore deploy only a fraction of their configured risk budget." The
subsequent stop-widening change made that cap bind on **every** trade rather
than on tight-stop variants only.

For a trader this matters twice over: the compounding path is wrong, and any
position-sizing intuition drawn from the config is wrong by more than half.

### T5. The live trade is not the researched trade

Three parity breaks, each of which would invalidate a proof in my book:

**(a) Trade frequency.** `research/factory_core.py::_simulate_trade` returns one
`trade_row` per symbol-session: the first valid signal of the day and nothing
after it. The live runtime has **no per-session trade cap**. `agent/risk.py:599`
blocks only holding the same symbol concurrently, and `:606` applies a post-loss
cooldown that is not configured in `config.yaml`. So live will take the first
signal *and* every later one once flat. The second through Nth signals of the
day have entirely unmeasured expectancy, and for opening-anchored families the
edge is most plausibly concentrated in the first.

**(b) Order type versus cost symmetry.** Entry is a **bracket market order**
(`agent/alpaca_domain.py:470`, `agent/alpaca_provider.py:556`). The take-profit
leg is a limit; the stop leg becomes a market order on trigger. Those legs have
structurally different costs: a resting limit fill pays no slippage, a triggered
stop in a moving market pays more than a marketable entry. The cost model
charges both symmetrically. The repository flags this itself at the end of the
measured-cost note. Net effect: winners are over-charged and losers are
under-charged, which flatters the loss tail and penalises the win tail.

**(c) Market entry on breakout signals.** Every entry takes liquidity at the
exact moment a breakout predicate fires, which is the moment the book is
thinnest and adverse selection is highest. There is no marketable-limit, no
spread check at the moment of entry beyond the blanket
`execution.max_spread_bps: 100` rejection cap, and no participation logic. A
70%-win-rate discretionary trader does the opposite: joins the bid, works the
offer, and skips the fill when the spread widens.

### T6. There is no trade management at all

Once filled, the position has exactly two possible futures: bracket or clock.
There is no scaling in, no partial profit-taking (`docs/edge-audit-remediation-2026-09-01.md`
records partial exits as "Not implemented" and deferred), no adding on
confirmation, no cutting early on thesis invalidation, no re-entry after a
stop, and no discretionary skip.

Breakeven exists only if a v3 `breakeven_r` is set, and it defaults to `None`.
A trailing ratchet exists only in v4 and defaults to `None`.

This is the difference between a signal and a trade. High win rates come from
managing the middle of the trade, and there is no middle here.

### T7. The tuning ladders are mostly rungs that cannot fire

The LLM is told (`research/llm_strategy.py:141`) that when it sees
`execution_blocked` it may use exactly these preregistered ladders:

```
stop_atr     = [0.2, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10]
min_atr_bps  = [0, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200, 300, 500, 1000, 2000]
```

`min_atr_bps` is compared against the same 14-minute ATR
(`agent/contracts/rule.py:1416`), which is about 8.7 bps on a median name.
**Only the rungs 0 and 5 can ever pass. Fifteen of seventeen are dead.** And as
T1 established, on the largest names the whole `stop_atr` ladder is dead.

So the system's response to "execution is blocked" is to hand the model a
ladder that cannot unblock it, on an axis that cannot move. The model then
correctly records that lowering the target does not help, that raising the
volatility floor does not help, and that the family appears dead. Those graded
lessons persist in the factory ledger across cycles and are fed to future
proposals.

**The learning ledger is being filled with false negatives.** That is the most
expensive form of this bug, because it compounds: the accumulated "evidence"
that these families do not work is an artefact of the cost and geometry
constants, and it will steer every future proposal away from families that may
be fine.

### T8. Nine of twelve families read biased features from a partial tape

`config.yaml` sets `data_feed: "iex"` and `deploy/recorder_market.py:42` pulls
1-minute bars from it. IEX is roughly 2% of consolidated volume. The protocol
acknowledges the limitation but the specific consequences are not addressed:

- `volume_breakout` and `range_expansion` compare current volume or range to a
  trailing average. On an IEX-only tape both are dominated by IEX's fluctuating
  routing share, not by market activity. These two families are measuring
  venue noise.
- `volatility_breakout`'s `compression_bps` gate reads bar highs and lows. An
  IEX bar range is systematically narrower than the consolidated range, so
  compression is detected spuriously and the filter fires in the wrong regime.
- `opening_range_breakout`, `opening_range_fade` and `opening_drive` set their
  levels from IEX opening highs and lows. Those are not the levels the rest of
  the market is trading against.
- `vwap_reversion` and `vwap_trend` anchor on an IEX volume-weighted price,
  which is not session VWAP.
- Replay stop and target resolution reads bar highs and lows, so a narrower
  range under-triggers both brackets and compounds T2.

The bias is directional, not mean-zero. And note the interaction with the ATR
problem: an IEX-only bar range understates true range, so the 14-minute ATR is
understated too, which makes the 83.33 bps floor bind even harder.

### T9. The statistics, correctly weighted

Everything the previous review said about the gate stack remains arithmetically
true: `GATE_REQUIRED_CHECKS` has 30 members; family BY, cycle-global BY, a
cluster veto and LORD++ are stacked on one multiplicity; BY is BH times the
harmonic number (`research/stats.py:1239`); LORD++ allocates
`W0 x 1/(t(t+1))` with `W0 = alpha/2` and never recovers without a discovery
(`research/factory_ledger.py:241,258`), reaching arithmetic exhaustion at test
224.

What changes is the ranking. At the 30-cluster floor the stack needs roughly
0.2R to 0.4R per trade. Section 2 shows that a corrected trade delivers 0.26R
to 0.48R on a genuinely good signal. **So the gates are demanding, defensible,
and roughly reachable once the trade is fixed.** They only look impossible
because the shipped geometry caps attainable R at about 0.1R.

Two statistical items remain genuine defects rather than mis-weighted ones:

- **LORD++ wealth decay with no recovery.** The acceptance threshold tightens
  monotonically toward zero regardless of evidence quality. Any discovery
  system with that property converges to zero discoveries.
- **No stated control of Type II error.** Thirty required checks control false
  positives superbly. Nothing anywhere states the false-negative rate. For a
  discovery system that is the more expensive error: a false positive costs one
  demoted paper candidate under a lifecycle that already demotes on drift, and
  a false negative silently discards the edge and records it as "the market has
  none".

---

## 5. Where documentation and code disagree

| Document | Claim | Reality at HEAD |
| --- | --- | --- |
| `docs/edge-discovery-upgrade-2026-08-29.md:20` | The 83.33 bps implication "is an admission gate, **not an instruction to widen stops**" | Commit `43891ff` (2026-09-01) made `research/factory_core.py:765`, `research/edge_discovery_core.py:1028` and `agent/risk.py:681` widen stops to it. The documented design decision was reversed without the note being updated. |
| `docs/measured-cost-model-2026-08-30.md:117` | "the gate does not widen stops for a candidate" | False at HEAD, same commit. |
| `research/protocol.md` | A 30 bps-floor trade "is vetoed" by the stressed-cost control | The research path now widens instead, so the veto in `research/costs.py::check_stressed_cost_plan` is a backstop that no longer fires there. The failure mode changed from "no trades" to "trades with alien geometry", which is materially different and undocumented. |

The first row is the important one. It is not documentation drift, it is a
**regression against a recorded design decision**, and it is the direct cause of
T1, T2 and T4.

---

## 6. The corrected causal chain

This is what actually produces the uniformly negative results, in order:

```
25 bps stress / 0.30 ratio
  -> forced 83.33 bps stop on every equity trade
     -> overrides a 7.7 bps authored ATR stop (10.8x)
        -> stop_atr axis dead on 10 of 24 symbols; ladders cannot move
        -> 2R target lands at 166.7 bps -> hit 0.0% of the time
        -> 94-98% of exits are the clock
     -> notional needs 60% of equity -> capped to 25%
        -> realised risk 0.208% not 0.5%; max 2 positions not 3
  -> 17 bps assumed cost / 83.33 bps stop = 0.204R toll on EVERY trade
     -> pre-cost-positive families (+0.085, +0.097, +0.146) go negative
        -> graded lessons record false negatives
           -> LLM reasons from poisoned history, burns its 64-call budget
  -> attainable R capped near +0.1R even for a strong signal
     -> gate stack needs 0.2R to 0.4R
        -> nothing can ever pass
```

Every arrow is verified in code. The root is one configuration pair plus one
unwired cost model.

---

## 7. What to do, in order

### Do this first, this week

**1. Wire in the measured cost model.** `research/quote_costs.py` already fits
per-symbol, per-half-hour spread and depth from the recorded corpus. Set
`costs.vehicles.equity` from the fitted schedule, keeping the conservative p75
percentile so it stays defensible. Predicted effect, from the repository's own
measurement: drag falls from about 0.136R to about 0.018R.

**2. Stop widening stops.** Restore the documented behaviour: the stressed-cost
control is an admission gate, not a geometry instruction. Concretely, either
apply it at portfolio level on realised cost-to-risk, or re-derive
`stressed_cost_scenario_bps` from the measured p95 or p99 spread per symbol and
half-hour, which the schedule already computes. A 25 bps shock on SPY is not a
stress scenario for SPY.

**3. Add one telemetry line that would have caught all of this.** Report per run:
`stop_floor_binding_rate`, `target_hit_rate`, and `cost_as_fraction_of_R`. If
the first is above a few percent, or the second is near zero, or the third is
above 0.1, the result is about the configuration and not about the strategy.
Refuse to grade a lesson from such a run.

**4. Re-run the frozen 44-variant cohort** through `research/cost_rerun.py`
under 1 and 2. The cohort and the runner exist. This is the cheapest test of
whether the negatives were an artefact. Prediction to check against: range
expansion, trend pullback and ORB should come out positive after costs.

**5. Quarantine the poisoned lessons.** Every graded lesson produced under the
17 bps cost and the binding stop floor is evidence about the configuration, not
about the family. Mark them non-citable for future proposals or the model will
keep reasoning from them.

### Then fix the trade

**6. Make the reward geometry match the holding period.** A 90-minute hold on
SPY should target 20 to 40 bps, not 167. Either scale `target_r` to realised
volatility, or move the default hold down and the target with it. The
`target_hold_reachability` telemetry already computes the right ladder; make a
target with a sub-1% historical hit rate a hard refusal rather than a
diagnostic.

**7. Fix the ATR timescale mismatch, or rename it.** A 14-**minute** ATR driving
a stop compared against a 30 bps grammar floor and a 0 to 2000 bps `min_atr_bps`
ladder is three different scales in one expression. Either lengthen the ATR
window to something comparable to the hold, or rescale the ladders so their
rungs can fire.

**8. Charge the exit legs asymmetrically.** A triggered stop pays more than a
resting limit. The repository already identified this. Until it is fixed, every
measured loss is understated and every measured win is overstated.

**9. Close the research/live parity gap on trade frequency.** Either cap live at
one trade per symbol-session to match what was proved, or extend the replay to
measure subsequent signals. Deploying a policy that was never measured is the
one thing in this system that could actually lose real money.

### Then give the search something worth finding

**10. The twelve families are the long-term ceiling.** Eleven are textbook
single-symbol bar patterns on the 24 most arbitraged ETFs listed, and the one
with genuine structural justification, `cross_sectional_residual`, is expressed
as a directional single-leg bet rather than a hedged spread, which discards the
mechanism. Once T1 to T3 are fixed you will be able to measure these families
honestly, and my expectation is that they are small but real: a few basis points,
consistent with the +0.024 to +0.146 pre-cost readings already observed.

To find something larger, the grammar needs mechanisms rather than patterns, in
this order of expected value:

- **Cross-asset lead-lag.** The recorder already captures all 24 symbols
  synchronously. SPY leading sector ETFs and SMH leading XLK at the 1-minute
  horizon is real and measurable with data you already have.
- **Prior-session levels and the overnight gap.** Gap fade and prior-day
  high/low interaction are among the most durable intraday effects that survive
  costs. The protocol already lists prior-session context as a bounded
  extension.
- **Make the residual family hedged.** This is the one family with a real
  mechanism and it is currently expressed in the form least likely to work.
- **Time of day as a first-class axis**, not a flat entry window. The open, the
  European close, the 15:00 and 15:45 rebalance windows and the close have
  different dynamics, and the current grammar cannot condition on them.

**11. Buy the SIP feed.** Nine of twelve families read biased features from IEX.
No amount of statistical rigour repairs a biased input, and the cost is a
rounding error against the engineering already invested here.

**12. Add a positive control.** Inject a synthetic signal with a known, tunable
edge and confirm the pipeline recovers it at the expected effect size. Every
finding in this document would have surfaced within a day of such a test
existing. A discovery system that has never demonstrated it can find a known
edge has not been validated as a discovery system. This is the single highest
-value permanent addition, because it turns "we found nothing" into a
falsifiable statement.

---

## 8. What I would say to the desk

The negatives to date are not a market result and should not be recorded as
one. They are the signature of a 17 bps cost assumption and an 83.33 bps
constant stop, applied to instruments whose real cost is under 1 bps and whose
14-minute ATR is under 10 bps. Three families already read positive before
costs. The apparatus around them is genuinely excellent and does not need
rebuilding.

Fix the cost model and stop overriding the stops, re-run the cohort you already
have, and you will find out within a week whether there is anything there. Until
then the system has not tested a single hypothesis about the market. It has
tested its own constants.

---

## Appendix: verification index

| Claim | Verified at |
| --- | --- |
| ATR is a 14-minute simple mean true range | `agent/contracts/rule.py:1361-1371`, bounds at `:101` |
| Stop = max(ATR x stop_atr, 30 bps), then floored at 83.33 | `agent/contracts/rule.py:1753`; `agent/contracts/risk_geometry.py:33,53,64` |
| Widening applied in research and runtime | `research/factory_core.py:765`; `research/edge_discovery_core.py:1028`; `agent/risk.py:681` |
| Target recomputed when the floor binds | `research/factory_core.py:783`; `agent/risk.py:694` |
| 17 bps round trip | `research/costs.py:619,665`; `config.yaml` costs block |
| Measured model gives ~3 bps; three families positive pre-cost | `docs/measured-cost-model-2026-08-30.md` |
| Measured path not wired to the authorising lane | imports of `research/quote_costs.py`; no `costs.vehicles` in `config.yaml` |
| Notional cap silently reduces shares | `agent/risk.py:124-138` |
| Gross cap rejects the third position | `agent/risk.py:767-769` |
| One trade per symbol-session in replay | `research/factory_core.py::_simulate_trade` single `trade_row` return |
| No per-session trade cap in the live runtime | `agent/risk.py:599,606`; no `max_trades` anywhere in `agent/` |
| Bracket market order; limit take-profit, stop-market stop | `agent/alpaca_domain.py:452-492`; `agent/alpaca_provider.py:553-556` |
| min_atr_bps compared to the 14-minute ATR | `agent/contracts/rule.py:1416-1423` |
| Tuning ladders | `research/factory_core.py:1984-1991`; `research/llm_strategy.py:141` |
| LLM cannot add signals; one field, 20% local, 8 novel values | `research/llm_strategy.py:105,127,141`; `research/strategy_factory.py:111` |
| 30 required checks; BY = BH x harmonic; LORD++ W0=alpha/2 | `research/gates.py:63`; `research/stats.py:1239`; `research/factory_ledger.py:241,258` |
| IEX 1-minute bars | `config.yaml`; `deploy/recorder_market.py:42,50` |
| Partial exits not implemented | `docs/edge-audit-remediation-2026-09-01.md` |
| "not an instruction to widen stops" | `docs/edge-discovery-upgrade-2026-08-29.md:20` |
| Zero trades at 0.30; -0.5416R at 0.60 | `docs/cost-risk-counterfactual-research-findings-2026-08-25.md` |
