# Edge hypotheses — H-G through H-L

> Preserved detailed hypothesis rationale. Current registered strategies,
> settings, and implementation status are maintained in
> [`../HYPOTHESES_AND_VARIANTS.md`](../HYPOTHESES_AND_VARIANTS.md) and
> [`../../MAIN_REPO_REVIEW_PLAN.md`](../../MAIN_REPO_REVIEW_PLAN.md). The
> analysis below is the original pre-registration/rationale record and should
> not be read as evidence that every proposed hypothesis is currently active.

**Companion to** [`findings.md`](findings.md) and
[`batched-implementation.md`](batched-implementation.md).
**Date:** 2026-07-28
**Naming:** continues findings.md §6, which used H-A to H-F for hypotheses
already embedded in the code. H-G onward are new, and none of them is currently
represented anywhere in the codebase.

---

## 0. A note on the framing, stated once

You asked for edges "only a select few best traders know". The honest position
is that there is no secret list. Every mechanism below has been written about
somewhere. What is genuinely rare is not the idea, it is three things that
almost nobody does together:

1. **Collecting the data the idea requires**, months before you need it, at a
   time when it looks like wasted effort.
2. **Conditioning rather than averaging.** Most retail systems test a setup
   unconditionally, get an expectancy near zero, and conclude there is no edge.
   The frequent truth is that there were two populations of opposite sign and
   the average destroyed both.
3. **Refusing to decide on a sample that cannot support a decision.** The
   discipline `INSUFFICIENT_SAMPLE` in batched-implementation.md §9.1 is worth
   more than any of the six hypotheses below.

So the edge is not the idea. The edge is the substrate. That is also the
argument of findings.md, and this document does not contradict it.

**One structural consequence dominates everything else here.** Three of the six
hypotheses are testable today, offline, against data you already hold. Three
require fields that are **not currently being recorded**. Snapshot fields cannot
be back-filled: findings.md §9.2 establishes that a replay must read the
recorded snapshot verbatim, and §9.6 establishes that anything not captured at
the time is permanently unavailable. **Every day that passes without recording
open interest and per-cycle book state is a day of sample you can never
recover.** That is why the action plan moves data capture to the front of the
queue, ahead of the research infrastructure that will eventually consume it.

---

## Format

Each hypothesis states:

| Field | Meaning |
| --- | --- |
| **Claim** | The falsifiable proposition, in one sentence |
| **Mechanism** | Who is on the other side, and why they persistently lose |
| **Prediction** | The specific, pre-registered comparison that tests it |
| **Data** | Fields required, and whether they exist today |
| **Tier** | Replay / shadow / forward-only, per findings.md §4.3 |
| **Falsifier** | The result that kills it, agreed before looking |
| **Cost to test** | Engineering effort and calendar time |

---

## H-G — Positioning quadrant: funding level is not the signal, funding sign crossed with open-interest change is

**Claim.** The forward return distribution conditional on a setup firing is
materially different depending on whether open interest is rising or falling
while funding is extended, and funding level alone cannot distinguish the two
cases.

**Mechanism.** Funding tells you the *price* of carrying a side. Open interest
change tells you whether the crowd is *building* that side or *unwinding* it.
These are different states with different forward distributions, and the current
contract collapses them into one number.

- Price up, OI up, funding up: new leveraged longs entering. The position is
  crowded and every one of those positions has a liquidation price above the
  current level. Fragile.
- Price up, OI down, funding falling: shorts covering. The buying is
  terminal, not initiating. It stops when the shorts are done.
- Price down, OI up, funding down: new shorts entering. Crowded the other way.
- Price down, OI down: longs being closed or liquidated. Supply is being
  exhausted rather than replenished.

The counterparty is the leveraged directional trader whose position size is a
function of recent price action rather than of expected value, and whose exit is
mechanically forced rather than chosen. Forced flow is price-insensitive, which
is the cleanest structural source of edge available in perpetual markets. The
carry literature documents that funding is a persistent, systematically
harvestable cash flow rather than noise: see
[The Crypto Carry Trade (Christin et al.)](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf)
for the magnitude, and
[He et al. via the perpetual-futures mechanism literature](https://arxiv.org/abs/2506.08573)
for the no-arbitrage relationship that makes funding a state variable rather
than a fee.

findings.md H-B correctly rejects the current `funding_squeeze` contract. This
hypothesis is not a repair of that contract. It is a claim that funding belongs
in the system as a **conditioning variable on every setup**, not as a setup type
of its own.

**Prediction.**

1. Conditional on `range_breakout` long firing, expectancy in the cell
   {OI rising over 4h, `funding_percentile_30` >= 80} is lower than in the cell
   {OI rising, `funding_percentile_30` between 30 and 70}, by more than the MDE
   at the achieved cell sample.
2. Conditional on a >= 2 ATR adverse move over <= 8 bars, a counter-trend entry
   has higher expectancy when OI fell over the same window than when OI rose.
3. `funding_percentile_30` outperforms the raw `funding_rate_pct` threshold on
   both, which independently confirms findings.md D2.

**Data.** `open_interest` and `open_interest_change_1h` / `_4h` are **not
collected**. `funding_percentile_30`, `funding_mean_30_pct` and
`funding_samples_30` are already computed in `market.py:102-109` and are simply
unused by the contract.

**Tier.** Forward-only until OI collection begins. **This is the strongest
argument in the document for shipping snapshot enrichment immediately.**

**Falsifier.** OI change adds no incremental explanatory power to forward return
beyond funding percentile alone, in a conditional expectancy comparison with at
least 150 observations per cell.

**Cost to test.** One additional OKX endpoint per universe refresh and two
derived fields. Roughly half a day of engineering. Then eight to twelve weeks of
waiting, which is exactly why it goes first.

---

## H-H — The tradeable moment in a liquidation cascade is depth restoration, not the impulse

**Claim.** Entries conditioned on the microstructural *recovery* signature after
a forced-flow event have materially better expectancy and materially tighter
maximum adverse excursion than entries taken on the impulse bar itself.

**Mechanism.** A liquidation is a market order that must execute regardless of
price. While the cascade is running, you are trading against an unbounded
price-insensitive seller, and there is no reason for the move to stop at any
particular level. Once the forced flow is exhausted, the temporary component of
the price impact reverts, and the observable signature of that transition is in
the book rather than in the price: spread spikes then normalises, top-of-book
depth collapses then refills, and price is still at or near the extreme when the
refill happens.

Minute-level evidence from the October 2025 cascade documents exactly this
sequence: volume surging to many multiples of baseline several minutes ahead of
the price trough, intra-minute spread reaching several percent, and the mark
price used for liquidation triggers undershooting both spot and futures, which
creates a reflexive loop with no equivalent in equity markets. See
[Anatomy of a Crypto Cascade (Lim)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6579278)
and the
[event-study treatment in Ali (2025)](https://papers.ssrn.com/sol3/Delivery.cfm/5611392.pdf?abstractid=5611392&mirid=1).
The distinguishing feature is not the size of the candle. It is whether the book
has come back.

This is the hypothesis your deferred A3 agent was conceived around. It is the
one with the clearest theoretical justification for why the money is there, and
it is the one you currently have the least data to test.

**Prediction.** Define a restoration filter as: (a) adverse excursion >= N ATR
within <= K minutes, (b) `spread_pct` reaching >= its 95th trailing percentile
and then returning to within Q% of its trailing median, (c) top-of-book depth
inside 0.35% of mid recovering to >= R% of its pre-event level. Entries passing
this filter have both higher expectancy and lower mean MAE than entries taken on
condition (a) alone.

**Data.** Depth and spread are already *read* every cycle by the entry liquidity
check, but they are only *journalled on rejection*
(`entry_liquidity_rejected`). Every passing observation is discarded. **The fix
is one journal write, and it is the cheapest high-value change in the entire
plan.**

**Tier.** Partial replay is possible using `entry_liquidity_rejected` rows plus
cached 1m OHLCV for the range and volume components. Full test is shadow and
forward.

**Falsifier.** The MAE distribution conditional on the restoration filter is not
tighter than the unconditional distribution at n >= 100 events.

**Cost to test.** One extra journal event per cycle. An afternoon. Then two to
three months of accumulation, since cascades are infrequent by construction.

---

## H-I — Regime is a multiplier, not an additive term: the same contract has opposite-signed expectancy in compression and expansion

**Claim.** `range_breakout` has positive expectancy when volatility is expanding
from a compressed base and negative expectancy when volatility is already
elevated, and the pooled unconditional figure is near zero because it is the
average of two populations of opposite sign.

**Mechanism.** A breakout from compression is a real event: a tight range
accumulates resting stop orders on both sides, and the first decisive move
through the boundary triggers them, which supplies the follow-through. A
breakout when volatility is already high is not an event at all. The "break" is
an ordinary excursion within a distribution that is already wide, and the stop,
sized off an ATR that is itself elevated, sits inside the noise. Trend
continuation is the mirror image: it needs sustained expansion and dies in chop.

This matters more than any parameter in the sweep list, for a statistical reason
rather than a trading one. A test that pools the two populations will report an
expectancy near zero with a wide confidence interval, and the pre-registered
rejection rule in batched-implementation.md §9.1 will then correctly reject a
hypothesis that was true in half the sample. **Conditioning is the difference
between finding the edge and permanently discarding it.**

**This directly challenges batch 6.4.** That batch proposes making
`range_breakout` and `trend_continuation` mutually exclusive by requiring the
breakout to occur *without* prior multi-timeframe alignment. If H-I is right,
the correct partition is by volatility regime, not by trend-alignment label, and
6.4 would encode a proxy of the right idea in the wrong variable. **Test H-I
before 6.4 merges.**

**Prediction.** Partition every replayed setup by a volatility-state variable,
for example realised volatility over the trailing 8 bars divided by realised
volatility over the trailing 96 bars, or ATR as a percentile of its own trailing
30-day distribution. Then:

- Bottom tercile (compression): `range_breakout` expectancy > 0.
- Top tercile (expansion): `range_breakout` expectancy < 0.
- Pooled: within noise of zero.
- `trend_continuation` shows the opposite ordering or a materially flatter
  profile.

**Data.** Fully computable from the recorded snapshot ATR plus the 1m OHLCV
cache that batch 2 builds anyway. **No new collection required.**

**Tier.** Replay. Testable the day the batch 3 harness works.

**Falsifier.** The conditional expectancies do not differ by more than the MDE
at the achieved cell sample, across at least three definitions of the regime
variable.

**Cost to test.** One derived field in the replay scorer. Under a day, once
batch 3 exists.

---

## H-J — Trade the residual, not the price: in a universe 0.8-plus correlated to BTC, single-name signals are one repeated BTC bet

**Claim.** A meaningful fraction of the variance in your trade outcomes is
explained by contemporaneous BTC return rather than by the setup, and
conditioning entries on beta-adjusted residual strength both improves per-trade
expectancy and relaxes the portfolio constraint that is currently suppressing
trade count.

**Mechanism.** The top-10 perpetuals by volume move together intraday. A long
breakout on three of them is not three trades, it is one levered long BTC
position paying three sets of fees. Your risk engine already knows this
implicitly: the 100% net-direction cap exists as a crude correlation control,
and findings.md §5 identifies it as one of the vetoes most likely to be binding.
The system is therefore in a bind of its own making. Its signals all fire on the
same side at the same time because they are the same trade, and its risk layer
then refuses most of them.

Stripping the market factor fixes both ends at once. Estimate each symbol's beta
to BTC over a trailing window, take the residual return, and require that a long
setup ranks in the top decile of residual strength across the universe and a
short in the bottom decile. The surviving trades are genuinely idiosyncratic, so
the net-direction cap stops binding, so trade count goes *up* rather than down,
which is the only lever in this document that increases sample size instead of
consuming it.

There is also a documented factor behind it. The three-factor model for
cryptocurrency returns in
[Liu, Tsyvinski and Wu, *Journal of Finance* 2022](https://onlinelibrary.wiley.com/doi/10.1111/jofi.13119)
finds that market, size and momentum capture the cross-section, with momentum
robust across specifications. Residualising against the market factor is exactly
how you isolate the momentum component rather than accidentally trading the
market one.

**Prediction.** Three claims, in ascending cost:

1. **Free diagnostic, run this first.** Regress realised trade PnL on
   contemporaneous BTC return over the holding window. If R-squared is high, the
   premise holds and the rest is worth doing. If it is low, the trades were
   already idiosyncratic and H-J is dead for one query's cost.
2. Conditioning entries on residual-strength decile improves expectancy relative
   to the same setups unconditioned.
3. Executed trade count per day *rises* under the residual condition, because
   the net-direction veto fires less often.

**Data.** BTC 15m returns over the corpus window, which the batch 2 price cache
provides. **No new collection required.**

**Tier.** Replay, including the portfolio-simulation forward pass that batch 3
already specifies.

**Falsifier.** Claim 1 fails, or claims 2 and 3 both fail at n >= 100.

**Cost to test.** Claim 1 is one query against the corpus and the price cache
and should be run in the first week the harness exists. Claims 2 and 3 are one
conditioning axis in the sweep runner.

---

## H-K — Entry price is a larger lever than entry timing, and half of it is measurable from data you will already have cached

**Claim.** At a 2% stop and a 2R target, round-trip friction is roughly 10% of
the risk unit, and a modest improvement in fill quality changes expectancy by
more than most of the parameter axes currently queued for sweeping.

**Mechanism.** Every IOC entry crosses the spread and accepts adverse selection:
you buy at the moment a seller wants to sell to you. The system reserves
`max_order_book_slippage_pct` (0.35%), `expected_stop_slippage_pct` (0.15%),
spread, and two sides of taker fee inside `estimated_loss_pct`. findings.md §9.1
treats that reservation as an accounting artefact that makes demo R look
pessimistic. It is that, but it is also a real cost, and the correct response is
not to adjust the denominator, it is to reduce the numerator.

Two sub-claims, and they have very different test costs.

**H-K(i) — Retest fills, replayable.** Measure the distribution of adverse
excursion in the first K minutes after the signal bar closes. If a substantial
fraction of eventually-profitable trades first traded X ATR against the entry,
then a resting limit at `signal_price - X * ATR` fills often and improves R by a
computable amount. The cost is the trades that never come back, and that cost is
exactly quantifiable from the same 1m data. There is a clean optimum in X and
you can find it offline.

**H-K(ii) — Maker-first with taker fallback, forward-only.** Post passively for
T seconds and cross if unfilled. This converts taker fee to maker fee on the
filled fraction and captures the spread rather than paying it. Fill rate is not
knowable from historical data because your passive order was never there, so
this is a live experiment and nothing else.

The reason this hypothesis matters disproportionately: **it does not require any
signal to have edge.** It improves whatever edge exists and it removes a
constant drag from the measurement of the other five. Order flow and book
imbalance are documented as among the most reliably predictive features at short
horizons across asset classes, including crypto, which is the same body of work
that says crossing the spread into an imbalanced book is the most expensive
moment to do it: see
[Explainable Patterns in Cryptocurrency Microstructure](https://arxiv.org/html/2602.00776v1).

**Data.** H-K(i) needs the 1m OHLCV cache from batch 2 and nothing else.
H-K(ii) needs a live experiment on the demo account.

**Tier.** (i) replay. (ii) forward.

**Falsifier.** (i) There is no X for which the improved R on filled trades
exceeds the expectancy lost on missed ones. (ii) The passive fill rate at T
seconds is too low to matter.

**Cost to test.** (i) is a single analysis over the price cache, roughly a day.
(ii) is a change to the entry path and should not be attempted until batch 6 has
already proven the change-control process works.

---

## H-L — Session and settlement clock: the same setup is not the same trade at 03:00 UTC and 14:00 UTC

**Claim.** Expectancy conditional on setup type is not uniform across the UTC
day, and the variation is large enough to be worth gating on.

**Mechanism.** A 24/7 market is not a uniform market. Participant mix, book
depth and volatility follow a strong diurnal cycle driven by Asian, European and
US working hours, and by the 8-hour funding settlement clock at 00:00, 08:00 and
16:00 UTC. Two distinct effects follow.

First, breakouts in thin hours are disproportionately stop-runs, because it
takes materially less size to push price through a level and back when the book
is shallow. The same nominal breakout is a different event depending on how much
capital was required to produce it.

Second, there is mechanical flow around funding settlement that carries no
information: positions opened or closed purely to collect or avoid the payment.
A signal that fires on that flow is fitting to a cash-flow schedule.

The empirical literature on crypto intraday seasonality is unusually consistent
about the shape of the day. Studies of hourly data find realised volatility
lowest around 05:00 UTC and volume peaking in the European and US overlap, and
document persistent hour-of-day differences in returns as well as volume. See
[Intraday and daily dynamics of cryptocurrency (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S1059056024006506),
[Quantpedia's seasonality work](https://quantpedia.com/the-seasonality-of-bitcoin/)
and
[Concretum on intraweek seasonality of intraday trend](https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/).
None of that is proprietary. Almost no retail system acts on it.

**Prediction.** Expectancy of `range_breakout` differs across pre-specified
session buckets, with the thin-liquidity bucket materially worse. Bars adjacent
to funding settlement have a distinguishable return and reversal profile from
the rest of the day.

**Data.** Timestamps are already in the corpus. **Nothing new is required and
this is testable the moment the corpus loader in batch 1 runs.**

**Tier.** Replay. The cheapest test in the document.

**Falsifier.** No pre-specified bucket differs from the pooled mean by more than
the MDE, out of sample.

**The overwhelming risk here is p-hacking, and it is worse than for any other
hypothesis in this document.** Twenty-four hourly buckets against a few hundred
trades will always produce a "significant" hour. Three non-negotiable
constraints:

1. Buckets are **pre-registered as three or four economically motivated
   windows** (Asia, Europe, US overlap, off-hours), written down before the
   query runs. Not twenty-four hours. Not data-driven boundaries.
2. The out-of-sample split from §9.1 of the implementation plan is mandatory
   here, not optional.
3. A multiple-comparisons correction is applied across the bucket set, and the
   uncorrected figure is never quoted.

If those three cannot be honoured, do not run the test. A false positive here is
worse than no test, because a session filter feels intuitive and will therefore
survive scrutiny it has not earned.

---

## H-M — Reframing, not a new edge: signal decay is an alpha question, not a cost question

Not a seventh hypothesis. A correction to how an existing batch is justified.

batched-implementation.md §9.2 proposes decoupling
`decision_interval_seconds` (900s) from `interval_seconds` (300s), justified as
a roughly 3x reduction in LLM spend. That is true and worth having.

But the same corpus answers a more interesting question: **how quickly does the
value of a signal decay as a function of latency from the signal bar close?**
The agent currently acts anywhere from 0 to 10 minutes into a 15-minute bar,
depending on where the 300s cycle happens to land. That is a naturally occurring
randomised experiment in execution latency, sitting in your journal, free.

If expectancy decays sharply with latency, the correct change is not merely to
align the decision cadence to the bar close for cost reasons, it is to align it
because **the delay is destroying edge**, and the cost saving is incidental.
That reframing changes the priority of §9.2 from a housekeeping optimisation to
a first-order fix.

Run this analysis in batch 1, before §9.2 is scheduled. It costs one query.

---

## Summary and priority

| ID | Hypothesis | New data needed | Tier | Earliest testable | Priority |
| --- | --- | --- | --- | --- | --- |
| **H-I** | Regime multiplies setup expectancy | None | Replay | Batch 3 done | **1** |
| **H-J** | Residual, not price (BTC beta) | None | Replay | Batch 3 done | **2** |
| **H-L** | Session and settlement clock | None | Replay | Batch 1 done | **3** |
| **H-K(i)** | Retest fills improve R | None | Replay | Batch 2 done | **4** |
| **H-G** | Funding crossed with OI change | Open interest | Forward | +8 to 12 weeks | **5, collect now** |
| **H-H** | Depth restoration after cascade | Per-cycle book state | Shadow / forward | +8 to 12 weeks | **6, collect now** |
| **H-K(ii)** | Maker-first entry | Live fill rates | Forward | After batch 6 | 7 |
| **H-M** | Signal decay with latency | None | Replay | Batch 1 done | Diagnostic |

**The ordering has two independent tracks and they run concurrently.**

The **test track** is H-I, H-J, H-L, H-K(i) and H-M. All are replayable, none
needs a single new field, and all of them reuse the same trades rather than
requiring new ones, which is the only way to get statistical power out of a
corpus this small. They are gated purely on the batch 0-3 harness existing.

The **collection track** is H-G and H-H. Neither can be tested for months. Both
are dead permanently if collection does not start now. Neither requires the
research layer to exist, neither touches the strategy contract, and neither
changes a single trading decision.

**The collection track therefore goes first in calendar order and last in
analysis order.** That inversion is the central recommendation of the
accompanying action plan.
