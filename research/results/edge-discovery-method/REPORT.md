# How to infer an edge, and where to look for one

Two questions: how would we *recognise* an edge in this strategy, and how do
we *find* one. The first has a concrete answer that this project has already
paid for. The second has a ranked list, and the top item is not a smarter
signal.

---

## Part A — The evidence standard

An edge is not "a backtest that made money". It is a claim that survives
every attempt to explain it away. Six tests, in the order they should be
applied, each of which killed something real in this project:

### 1. Beat an explicit null

A result means nothing without knowing what nothing scores. Three nulls:

- **random timing** — same instruments, same stop/target, random entry bars;
- **random direction** — real entry times, coin-flip side;
- **inverted signal** — trade the exact opposite.

The shipped strategy failed this immediately. At zero cost its own signal
(-0.046% per trade) was **worse than random entry timing** (+0.018%) and
worse than inverting itself (+0.021%). No further testing was needed.

### 2. Survive realistic costs

Round-trip taker is 0.10% before spread. Measured cross-sectional spreads in
this market are 0.05-0.3% gross. **The edge and the fee are the same size**,
so a result that ignores costs is not evidence about a tradable strategy.
Report the breakeven cost, not just the net at one assumption.

### 3. Survive out-of-sample

Discover on the first 60%, confirm on the last 40%, purge the gap. Of 79
parameter variants tested this way, **zero** were positive out-of-sample.
Across 64 selector rules, the in-sample-to-out-of-sample expectancy
correlation was **-0.155** — rules that looked best in period one did
*worse* in period two.

### 4. Survive a placebo

The one that matters most, and the one nearly everyone skips.

Take the candidate. Destroy its information — shuffle the signal across
instruments within each timestamp — and run the *identical* procedure. A
real edge goes to zero. An artifact does not.

This killed the best candidate this project found. Long the highest 24h range
position among instruments in an uptrend, short the lowest, held 48h: +0.949%
in-sample (t=4.75), +1.297% out-of-sample (t=2.93), 8 of 9 quarters positive,
and it survived beta-neutralisation. Every conventional check passed.

Its placebo scored **+0.455% with t=2.37** — 41% of the "real" result.

The cause was a bias in the method, not the market. Decile thresholds were
computed on the distribution pooled across all time, and range position moves
together across instruments: in an up-market almost everything sits near its
24h high. So the top decile was sampling bullish *moments* and the bottom
decile bearish ones. The trade was harvesting market timing.

Ranking within each timestamp instead — both legs from the same moment, so
market direction cancels — the placebo **beats** the real signal in most
cells.

### 5. Have a mechanism

Why would this money be available, and who is losing it? "The backtest says
so" is not an answer. A signal with no economic story is a signal you cannot
tell apart from overfitting, and you will not know when it stops working.

### 6. Be large enough to detect

Trade dispersion here is sd ≈ 1.07R. Detecting a +0.10R edge at 95%
confidence needs ~900 trades ≈ 10 months at this trade rate. If a proposed
edge is smaller than that, no forward test of reasonable length can confirm
it, and you are choosing to trade on faith.

### The noise floor, measured

Across roughly 2,250 hypotheses on two years of data, **a t-statistic above
2 arose routinely from pure noise** — the placebo reached t=2.60. Overlapping
windows and clustered returns inflate conventional statistics badly here.

**Practical bar: t>2 means nothing on this data. Require a placebo control,
out-of-sample confirmation, and a mechanism — or treat it as noise.**

---

## Part B — Where an edge could actually live, ranked

### 1. Execution cost (highest probability, lowest glamour)

Every result in this project pays 0.05% per side plus spread. Maker execution
is 0.02% per side: **0.06% of round-trip saving**, against measured gross
spreads of 0.05-0.3%. That single change flips several point estimates from
negative to positive.

This is not a prediction problem. It is the only lever that improves *every*
trade regardless of whether the signal works.

The catch is architectural. The agent posts marketable IOC orders with
exchange-side SL/TP attached — taker by construction, chosen so a position is
never unprotected. Maker execution means resting limit orders, uncertain
fills, adverse selection (you get filled precisely when you are wrong), and a
window where the position exists without a stop. That is a real redesign with
real new risks, not a config change.

### 2. Data that is not in OHLCV (highest ceiling)

The candle search failed for a structural reason: **price history is the most
mined dataset in this market**, and 15-minute OHLCV cannot contain
order-book state, aggressor direction, or liquidation cascades. No
re-arrangement recovers information that was never sampled.

What exists but is not being collected:

| Signal | Availability |
| --- | --- |
| Order-book depth and imbalance | **Never served historically.** Must be recorded. |
| Retail long/short ratio | ~30 days |
| Taker buy/sell volume | ~30 days |
| Open interest | ~60 days |
| Funding rate | ~97 days |

Every hour this is not being recorded is evidence that cannot be bought back.

### 3. Breadth

Cross-sectional strategies need instruments to rank against. The universe is
10 names; after a regime filter, 3-5 remain. This is why pooled statistics
looked strong while the implementable form collapsed to t≈0 — selection among
four candidates is not selection. Raising `universe.top_n` to 20-30 costs
nothing but API calls and would materially improve any cross-sectional test.

### 4. The LLM, given something it can actually use

The model currently receives fields derived from OHLCV — the same fields whose
in-sample-to-out-of-sample information content measured **-0.155**. It is
being asked to find signal in data that provably has none, and its plausible
advantage (reading unstructured context: news, narrative, protocol events) is
not in the snapshot at all. Until the inputs change, the selector cannot help.

---

## Part C — What was measured today

### Taker volume: rejected on its sign check

OKX publishes hourly taker buy/sell volume. Before testing whether it
predicts anything, it must be established that it measures what it claims: if
the series tracks aggressor direction, it must correlate strongly with the
**same hour's** return.

Measured across 14 instruments and 720 hours each: **mean correlation
+0.0063**, individual values from -0.06 to +0.04.

Real order flow correlates with contemporaneous returns at 0.3-0.7. This is
zero. The series aggregates across every contract type for a currency and is
smoothed to hourly buckets, so it does not correspond to the instrument being
traded. **Rejected — no further testing warranted.**

This check cost minutes and prevented an entire analysis built on a series
that measures nothing.

### Retail long/short ratio: a live hypothesis

| Feature | 16h | 24h | 48h |
| --- | ---: | ---: | ---: |
| Raw level | +0.170% (t=2.24) | +0.290% (t=2.40) | +0.598% (t=2.85) |
| Per-instrument demeaned | +0.235% (t=1.13) | +0.413% (t=1.36) | **+1.114% (t=2.72)** |

The obvious objection is a fixed effect: if retail structurally prefers coins
that happened to rise, sorting on the level captures that and nothing else.
Tested directly — the cross-sectional correlation between a coin's average
long/short ratio and its 30-day return is **-0.432**, a *headwind*. And
removing the fixed effect by per-instrument demeaning makes the signal
**stronger**, not weaker.

Reading: within an instrument, when retail's long/short ratio rises relative
to its own average, that instrument tends to outperform over the next 16-48h.
Effect size at 48h is ~11x the round-trip taker cost.

**This is not tradable evidence.** It is 30 days, ~210 independent
observations at the 48h horizon, and only the 48h cell reaches significance
while 16h and 24h do not. Given the measured noise floor, it is exactly the
kind of result that dissolves under a placebo on more data. It is a
hypothesis worth *collecting data for* — nothing more.

### Order-book microstructure: now measurable

Two bugs surfaced while building the recorder, both of which would have
silently corrupted any depth analysis:

- **Depth bands were saturated.** The top 50 levels of BTC-USDT-SWAP span
  under 1 basis point; 400 levels span ~7. Bands of 25-50 bps returned "the
  entire book" every time and were indistinguishable. Bands are now 1-25 bps,
  and `book_span_bps` records where the book ends so a saturated band is
  identifiable as a lower bound.
- **Sizes are in contracts, not base units.** The multiplier differs per
  instrument (0.01 BTC, 1000 DOGE), so raw `price × size` overstated BTC
  depth by 100x and understated DOGE by 1000x — making instruments
  non-comparable, which is the entire purpose of recording depth.

Corrected snapshot, resting bid notional:

| Instrument | Spread (bps) | Book span (bps) | $ @1bp | $ @5bps | $ @25bps |
| --- | ---: | ---: | ---: | ---: | ---: |
| BTC | 0.015 | 6.7 | 1.37M | 8.33M | 14.1M |
| ETH | 0.052 | 20.6 | 1.63M | 6.57M | 23.8M |
| SOL | 1.321 | 527 | 0.04M | 0.77M | 4.11M |
| DOGE | 1.390 | 554 | 0.01M | 0.32M | 2.54M |
| ZEC | 0.206 | 92 | 0.00M | 0.05M | 0.63M |
| BEAT | 0.247 | 125 | 0.00M | 0.00M | 0.02M |

Two immediate implications. **Spread varies ~90x across the tradable
universe** (BTC 0.015 bps, DOGE 1.39 bps) while `execution.max_spread_pct` is
15 bps — permissive enough that it never binds, so it is not protecting
anything. And **depth is not the constraint at retail size**: even BEAT
offers ~$20k within 25 bps, comfortably above a $3k position from a $10k
account. Capacity limits matter later, not now.

---

## Part D — What to do

**Start recording.** `research/record_flow.py` captures order-book depth and
imbalance every 5 minutes plus the four short-retention series, day-
partitioned and deduplicated, restart-safe, ~20 MB/month:

```bash
nohup python research/record_flow.py --out runtime/research/recorded &
```

The data does not exist until it is collected, and it cannot be backfilled at
any price. In three months there is enough to test the long/short hypothesis
properly; in a year, enough to test order-book imbalance the way it deserves.

**Widen the universe** — `universe.top_n` from 10 to 20-30. Costs API calls,
materially improves every cross-sectional test.

**Cost the maker redesign** before building it. The question is not whether
0.06% helps — it obviously does — but whether adverse selection on resting
orders costs more than the fee saved, and whether an unprotected fill window
is acceptable. That is a design study, not a code change.

**Do not trade any of this yet.** The long/short result is one month of data
in a market where t=2.6 has already been shown to arise from nothing.

---

## Reproduce

```bash
python research/fetch_flow_data.py --out runtime/research/flow --days 32
python research/analyse_flow.py --data runtime/research/data \
                                --flow runtime/research/flow
python research/record_flow.py  --out runtime/research/recorded
```
