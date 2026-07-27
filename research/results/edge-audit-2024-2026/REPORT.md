# Independent edge audit — phase1-v2 momentum strategy

**Question asked:** is there a tradable edge in this strategy, and is the
system ready to be run forward on an OKX demo account?

**Answer:** the engineering is ready; the strategy is not. Across 24 months,
28 instruments and 115,929 signals, the entry contract carries no measurable
directional information. It is negative before costs and significantly
negative after them. Running it on demo is safe and worth doing to validate
the plumbing, but no demo run of realistic length can discover an edge that
the historical record says is not there.

---

## 1. Method, and why it should be believed

This audit does not reuse the existing `phase1_v2_backtest.py`. It is a
second, independently written feature engine (`research/edge_lab.py`),
cross-validated against the production code in two directions:

| Check | Scope | Result |
| --- | --- | --- |
| Vectorized evidence contract vs `agent.strategy.setup_evidence` and `build_setup_plan` | 240 sampled bars, 10 symbols, all 3 setups × 2 directions, all 3 exit policies | **0 disagreements** |
| Research features vs `agent.market.symbol_snapshot` — the actual live code path, driven through a fake exchange with a frozen clock | 290 sampled bars, 10 symbols, 25 snapshot fields | **0 mismatches** |

Reproduce with:

```bash
python research/validate_features.py --data runtime/research/data \
  --samples 30 --symbols "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,..."
```

So when this report says "the strategy did X", it means the repository's own
contract code, fed the exact inputs the live agent would compute, did X.

### Data

- OKX public v5 REST, 15m swap candles, **2024-07-27 → 2026-07-27** (24 months).
- 28 USDT-margined perpetuals; 27 with usable history; 70,080 bars each for
  the full-history names. Zero gaps in the majors.
- Funding-rate history and hourly open-interest history where OKX serves it.

### Design choices that remove bias

1. **Point-in-time universe.** The live agent rebuilds a top-10-by-24h-turnover
   universe every hour above a $50m floor. This audit reconstructs that
   ranking hour by hour from trailing 24h quote volume, rather than testing a
   fixed hand-picked symbol list. A symbol only produces signals in hours when
   it would actually have been in the universe.
2. **No look-ahead.** Entries fill at the open of the bar *after* the signal
   bar. Every indicator uses only completed bars. Higher-timeframe values are
   mapped to the newest 1h/4h bar that had closed at decision time.
3. **Conservative ambiguity.** When a bar spans both stop and target, the stop
   is assumed to fill first. Stop fills take the worse of the stop level and
   the bar open. Target fills get no gap improvement.
4. **Explicit null baselines.** Every result is reported next to random entry
   timing, random direction, and the inverted signal. "Positive expectancy" is
   meaningless without knowing what nothing scores.
5. **Purged walk-forward.** Parameter searches are scored on a held-out final
   40% with a 3-day purge gap, and the number of hypotheses tested is stated.

### Known limitations, stated up front

- **Survivorship.** Symbol discovery used the *current* instrument list, so
  perpetuals delisted before July 2026 are absent. This biases results
  *upward*, and they are still negative.
- **Funding history.** OKX serves only ~97 days of funding rates. Outside that
  window each symbol is charged its own median observed rate per settlement.
  Measured rates are ~0.002%/8h on majors, so funding is second-order against
  0.10% round-trip fees either way.
- **No historical order book.** Spread and depth are carried by declared cost
  scenarios, not measured. This is why the cost-breakeven sweep matters more
  than any single cost assumption.
- **The LLM is not replayed.** Replaying a current model on 2024-2025 data
  would leak its training knowledge. Section 5 bounds what any selector could
  contribute instead.

---

## 2. The decisive test: does the entry predict direction?

Stops and targets are trade *management*. Underneath them there must be a
directional forecast. This measures the raw sign-adjusted forward return after
every qualifying signal, with no exit overlay at all, against the same
symbol's unconditional drift.

| Setup | Horizon | Signals | Mean fwd % | Excess vs drift % | Hit rate % | Monthly t |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| combined | 15 min | 115,929 | -0.0013 | -0.0013 | 45.76 | -1.06 |
| combined | 30 min | 115,929 | -0.0072 | -0.0072 | 45.60 | -1.69 |
| combined | 1 h | 115,929 | -0.0029 | -0.0029 | 45.91 | -0.70 |
| combined | 2 h | 115,929 | +0.0049 | +0.0049 | 45.84 | 0.26 |
| combined | 4 h | 115,929 | +0.0444 | +0.0444 | 46.71 | 1.48 |
| combined | 8 h | 115,929 | +0.0986 | +0.0986 | 47.29 | 1.66 |
| combined | 16 h | 115,929 | +0.1938 | +0.1938 | 47.31 | 1.28 |
| combined | 24 h | 115,929 | +0.1591 | +0.1591 | 46.70 | 0.66 |

Read this carefully, because it is the whole story:

- **The hit rate is below 50% at every horizon** — 45.6% to 47.3%. The setup is
  slightly *more* likely to be wrong about direction than a coin flip.
- **In the first hour the signal is actively negative.** The worst point is 30
  minutes after entry (t = -1.69). The contract buys a completed 15m impulse in
  the direction of the 1h/4h trend; on average that is the local exhaustion of
  a short move, and price gives it back immediately.
- The positive means at 8-24h come from a fat right tail, not from being right.
  With a sub-50% hit rate the whole expectancy depends on a handful of large
  runners surviving a 24h max hold.
- **The best number in the table is +0.194% at 16 hours with t = 1.28** — not
  statistically significant, and barely twice the 0.10% round-trip taker fee
  before spread and slippage. There is nothing here to pay for a trade.

---

## 3. The shipped configuration against null baselines

Non-overlapping trades, point-in-time universe, 24 months.

| Costs | Variant | Trades | Win % | Exp R | Exp % | PF | Monthly t | 95% CI on R |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| **frictionless** | **signal** | 9,425 | 39.38 | +0.0080 | **-0.0462** | 0.970 | 0.37 | [-0.035, +0.054] |
| frictionless | signal inverted | 13,989 | 35.76 | +0.0045 | **+0.0205** | 1.018 | 0.23 | — |
| frictionless | null: random direction | 11,064 | 37.69 | +0.0010 | -0.0372 | 0.973 | -0.06 | — |
| frictionless | null: random timing | 13,788 | 38.01 | +0.0052 | **+0.0177** | 1.013 | 0.46 | — |
| **base** | **signal** | 9,486 | 37.79 | **-0.0963** | -0.2799 | 0.836 | **-4.60** | **[-0.135, -0.056]** |
| base | null: random timing | 13,896 | 36.31 | -0.1224 | -0.2260 | 0.855 | -9.35 | — |
| realistic_alt | signal | 9,542 | 36.48 | -0.1411 | -0.4116 | 0.770 | -7.24 | [-0.176, -0.104] |
| stress | signal | 10,347 | 31.44 | -0.2344 | -0.6961 | 0.634 | -14.73 | [-0.264, -0.204] |

Cost scenarios: `frictionless` = zero. `base` = 0.05%/side fee, 0.05% entry and
exit slippage, 0.15% stop slippage. `realistic_alt` = 0.10% slippage, 0.25%
stop slippage. `stress` = entry at the configured 0.35% IOC boundary.

Two things stand out:

- **At zero cost, the strategy's own signal has the worst percentage
  expectancy of the four variants.** Inverting the signal (+0.0205%) and
  entering at random times (+0.0177%) both beat following it (-0.0462%). The
  95% CI straddles zero. This is what no information looks like.
- **At ordinary costs the result is significantly negative**: -0.096 R, CI
  entirely below zero, t = -4.60, and **only 4 of 24 months positive**.

### Decision latency does not matter — which is itself the finding

The agent's 300s cycle is not aligned to 15m bar boundaries, so it can act up
to ~15 minutes after the signal bar closes.

| Entry delay | Trades | Exp R | Exp % | PF |
| ---: | ---: | ---: | ---: | ---: |
| 0 min | 9,486 | -0.0963 | -0.2799 | 0.836 |
| 15 min | 9,370 | -0.0950 | -0.2799 | 0.836 |
| 30 min | 9,544 | -0.0943 | -0.2697 | 0.842 |
| 45 min | 9,855 | -0.0977 | -0.2769 | 0.839 |

A real short-horizon edge decays as you delay entry. This is flat to three
decimal places. That is the signature of noise, not of alpha you are late to.

---

## 4. Where is expectancy positive? Nowhere that survives.

### By cost — including zero

| Round-trip cost % | Expectancy % | Exp R | PF |
| ---: | ---: | ---: | ---: |
| **0.00** | **-0.0443** | +0.0082 | 0.972 |
| 0.04 | -0.0827 | -0.0136 | 0.948 |
| 0.10 (OKX taker) | -0.1549 | -0.0471 | 0.905 |
| 0.20 | -0.2859 | -0.0990 | 0.832 |
| 0.30 | -0.3859 | -0.1335 | 0.781 |

**No cost level tested, including exactly zero, produced positive expectancy.**
This is the single most important line in the audit: the problem is the
signal, not the fee schedule, not the exchange, not the execution layer.

### By regime, symbol and setup

Every regime is negative: `high_volatility` -0.059 R, `trend_down` -0.079 R,
`transition` -0.101 R, `trend_up` -0.111 R.

Every setup is negative: `trend_continuation` -0.078 R (52.7% stop-out rate),
`range_breakout` -0.099 R (40.4% of trades time out at max hold).

Both directions are negative: short -0.060 R, long -0.115 R.

By symbol, only two of 22 are positive — **LAB** (+0.300 R, 87 trades, t = 0.50)
and **PUMP** (+0.044 R, 188 trades, t = 0.33). Both are small samples on newly
listed instruments with insignificant t-statistics, i.e. launch-period noise.
Meanwhile the deepest, cheapest instruments are among the *worst*: **BTC
-0.139 R (t = -5.38)** and **ETH -0.123 R (t = -3.67)**. That rules out
"it just needs better liquidity" as an explanation.

**All 24 calendar months are negative at base costs**, best -0.006 R.

### The parameter sweep

79 pre-registered variants — stop width, reward:risk, hold time, exit policy,
no-chase limit, relative-volume floor, ATR-ratio bands, regime filters,
breakout strictness, session filters, funding alignment, setup subsets, and
the explicit contrarian "fade the setup" hypothesis — each scored on a purged
walk-forward split.

```
hypotheses tested: 79
OOS expectancy across all 79 variants:
    mean = -0.1037   median = -0.1053
    max  = -0.0498   min    = -0.1741
    share positive = 0.0%

best in-sample variant's out-of-sample result: -0.1119 R over 306 trades
```

**Not one of 79 variants achieved positive out-of-sample expectancy.** There is
no multiple-testing correction to argue about here, because there is no
winner to discount. The best OOS result in the entire search is -0.05 R.

---

## 5. Could the LLM rescue it? What the ceiling actually is.

The LLM cannot be replayed honestly on historical data, so instead: bound it.

| Selector | Keeps | Expectancy R |
| --- | ---: | ---: |
| take everything (today's behaviour) | 9,486 | -0.0963 |
| **perfect foresight**, best 50% | 4,743 | **+0.7853** |
| perfect foresight, best 25% | 2,371 | +1.5559 |
| random selector, keeps 50% | 4,743 | -0.0956 |

Interpolating between no skill and perfect foresight:

| Selector keeps | Share of *perfect foresight* needed just to break even |
| ---: | ---: |
| 75% of signals | 32.4% |
| 50% of signals | **10.9%** |
| 25% of signals | 5.8% |

So the bar is not absurd — a selector keeping half the signals needs about 11%
of perfect foresight to reach zero. The problem is that **nothing observable
supplies it.** Testing 64 threshold rules on every feature the model can see
(ATR ratio, relative volume, extension from EMA20, momentum, range position,
stop width, BTC beta, funding, 24h turnover), scored in-sample then
out-of-sample:

- best rule: `extension_atr >= 1.977` → in-sample **-0.0035 R**, out-of-sample
  **-0.0999 R**;
- **correlation between in-sample and out-of-sample expectancy across all 64
  rules: -0.155.**

A *negative* IS→OOS correlation means the rules that looked best in the first
period did systematically worse in the second. That is the fingerprint of
fitting noise. The LLM receives exactly these fields; it has no private data
source. Asking it to find 11% of perfect foresight in features that carry
negative out-of-sample information is not a plan.

---

## 6. The account simulation: it self-kills on day 50

Section 3 measures signals. This runs a whole account through the
repository's real `RiskEngine` — concurrent-position limits, exposure, beta
and planned-risk caps, cooldowns, failed-thesis re-entry rules, the daily
loss stop and the drawdown self-kill — on the point-in-time universe, from
$10,000.

| Costs | Drawdown kill | Return % | Max DD % | Sharpe | Trades | Killed on | Fees $ |
| --- | --- | ---: | ---: | ---: | ---: | --- | ---: |
| frictionless | **ON** (real behaviour) | +0.79 | 15.17 | 0.09 | 204 | **2024-09-15** | 0 |
| base | **ON** (real behaviour) | -3.07 | 15.26 | -0.12 | 195 | **2024-09-15** | 603 |
| frictionless | off (diagnostic) | **-58.17** | 72.90 | -0.80 | 3,336 | — | 0 |
| base | off (diagnostic) | **-95.10** | 96.01 | -3.32 | 3,360 | — | 3,270 |

The headline "+0.79%" is an illusion. **The agent breached its 15% drawdown
limit and self-killed 50 days into a 730-day test.** The remaining 680 days
are a frozen account, not a flat strategy.

Turning the breaker off shows what it was protecting against: **-58% at zero
cost, -95% at ordinary costs**, with a 96% peak drawdown. At base costs the
account paid **$3,270 in fees on $10,000 of starting capital** — a third of
the account per year, purely in taker fees, which is the mechanism by which a
zero-edge signal becomes a total loss.

The drawdown self-kill works exactly as designed. It is the only reason the
executable line reads -3% instead of -95%.

Note also the top rejection reason: `max concurrent positions reached`
(2,111 times). The agent is signal-saturated — it holds 3 positions almost
continuously and turns away most candidates. Selection therefore matters a
great deal in practice, which is what Section 5 bounds.

## 6b. Cross-check: the repository's own backtest, same data

To make sure this is not an artifact of a new codebase, the repository's
existing `research/phase1_v2_backtest.py` was run unmodified on the same
freshly downloaded bars, restricted to the same six instruments its committed
result used (AAVE, BTC, DOGE, ETH, SOL, XRP) — but over a *different* window
(2024-07 → 2026-07 rather than 2025-01 → 2026-07).

| Source | Combined fixed_rr, frictionless | Combined fixed_rr, base |
| --- | --- | --- |
| This audit (28 symbols, point-in-time universe) | +0.008 R, PF 0.970 | **-0.096 R**, CI [-0.135, -0.056] |
| Repo backtest (6 symbols, fresh data, new window) | +0.014 R, CI [-0.021, +0.049] | **-0.084 R**, CI [-0.123, -0.047] |
| Repo backtest (committed 2025-2026 result) | +0.012 R, CI [-0.033, +0.059] | **-0.088 R**, CI [-0.136, -0.039] |

Three runs — two independently written simulators, three data windows, two
universe constructions — land within 0.012 R of each other and reach the same
verdict. Its own no-kill diagnostic on this data reads -25% at base costs and
-57% at the entry-cap stress.

This result is not fragile, and it is not an artifact of how it was measured.

## 7. How long would a demo have to run to settle this?

Observed dispersion of trade outcomes: **sd = 1.067 R**. The agent is capped at
3 concurrent positions and takes the highest-priority candidate per bar, so a
realistic ceiling is ~3 trades/day.

| If the true edge were… | Trades needed (95% conf, 80% power) | Days at 3/day | Months |
| ---: | ---: | ---: | ---: |
| +0.05 R/trade | 3,572 | 1,191 | 39.2 |
| +0.10 R/trade | 893 | 298 | 9.8 |
| +0.20 R/trade | 224 | 75 | 2.4 |
| +0.30 R/trade | 100 | 34 | 1.1 |

**A two-week demo produces roughly 42 trades.** That is enough to detect an
edge of about +0.46 R/trade — five times larger than anything in this report's
range, and far larger than any real intraday crypto edge. The README's advice
to "run demo for at least two weeks" is right about validating *operations*
and misleading if read as validating *profitability*.

---

## 8. Findings in the code, independent of the edge question

These are real and worth fixing whatever you decide about the strategy.

### 8.1 The margin guard has exactly zero headroom at a full book

`max_position_notional_pct: 40` at `entry_leverage: 2` is 20% initial margin
per position. `max_concurrent_positions: 3` gives **60.0%** margin usage on a
full book. `max_margin_usage_pct: 60` fires above 60%.

Because usage is initial margin ÷ *mark-to-market* equity, **any** unrealized
loss on a full book pushes usage past the threshold and force-closes the
largest position (`agent/engine.py:1692`) — a realized loss plus a taker
round-trip caused by configuration arithmetic, not by the strategy.

Verified numerically: for every stop below ~3.7% the 40% notional cap binds,
so sizing lands on exactly 20% margin every time.

```
 stop%  est_loss%  notional%eq  margin%eq  cap_binds
  0.60      1.222        40.00      20.00        YES
  1.00      1.622        40.00      20.00        YES
  2.00      2.622        40.00      20.00        YES
  4.00      4.622        32.45      16.23         no
```

Fix: lower `max_position_notional_pct` to ~30 (45% at full book), or raise
`max_margin_usage_pct` to ~75, or reduce `max_concurrent_positions` to 2.

### 8.2 `risk_per_trade_pct` is not the binding constraint

Same table: with typical 0.6-2% stops the notional cap binds first, so real
risk per trade is **0.49-1.05% of equity, not the configured 1.5%**. The README
does document this, but the parameter does not do what its name implies —
turning it up changes nothing until stops exceed ~3.7%.

### 8.3 `funding_squeeze` is unreachable on every liquid instrument

`funding_extreme_pct: 0.03` requires |funding| ≥ 0.03% per interval. Measured
across 7,500 real settlements:

| Instrument | Settlements ≥ 0.03% | Max observed |
| --- | ---: | ---: |
| BTC, ETH, DOGE, XRP, PUMP | **0.00%** | 0.010-0.025% |
| SOL, SHIB, PEPE | 0.34% | 0.031-0.036% |
| BEAT | 34.99% | 0.374% |
| RE | 68.55% | 1.000% |

The setup can therefore only ever trigger on thin, newly listed alts — exactly
where spreads are widest and a "fade the crowded position" trade is most
dangerous. As shipped it is either dead code or a thin-alt-only strategy;
neither is what the prompt describes.

### 8.4 Live snapshot mixes a live price with completed-bar structure

`agent/market.py` derives `swing_low_pct`, `swing_high_pct`,
`ema20_1h_dist_pct` and `range_pos_pct` from the *live ticker* `last`, while
trends, breakouts and momentum come from the completed signal bar. On a 300s
cycle these can be up to ~15 minutes apart. Consequences seen while building
the validator: `swing_low_pct` can go negative when price has already broken
the swing, which `build_setup_plan` then rejects as "structure invalidation
distance is unavailable" — a silent, price-drift-dependent rejection rather
than a deliberate rule. Economically this is immaterial (sections 2-3), but it
makes behaviour non-reproducible from bar data alone.

### 8.5 Reproducibility gap in the existing backtest

`research/README.md` documents a data layout but pins no source with two years
of funding history. OKX's public API serves ~97 days. A dataset lacking older
funding rows makes `funding_return_pct` silently return 0.0 for older trades,
so a run can report "funding included" while charging nothing for most of the
period. The committed `phase1-v2-backtest-2025-2026` result cannot be
reproduced or checked because its input data is not committed or hash-pinned.

### 8.6 Things that are genuinely well built

Worth saying plainly, because it is unusual:

- 194 tests pass; the safety surface is thoroughly covered.
- No look-ahead in `_closed_ohlcv`; the incomplete bar is correctly dropped.
- The evidence contract and stop/target derivation reproduced **exactly** under
  independent reimplementation — 0 disagreements in 240 sampled bars.
- Exchange-side SL/TP attached at entry, with the position closed if the stop
  cannot be verified; ambiguous network responses are never blindly retried.
- Equity basis, transfer rebasing, position-age recovery, single-process
  locking and the journal are all handled carefully.

The risk engine does what it claims. It is protecting a strategy that has
nothing to protect.

---

## 9. Verdict

**On edge: none found, and the search was thorough.** 115,929 signals, 9,425
non-overlapping trades, 79 walk-forward variants, 64 selector rules, 5 cost
scenarios, 4 entry-latency settings. Negative before costs, significantly
negative after them, negative in every regime, every setup, both directions,
all 24 months, and 0 of 79 parameter variants positive out-of-sample. Sub-50%
directional hit rate at every horizon from 15 minutes to 24 hours.

**On paper-trading readiness:** the *software* is ready — run it on demo. The
*strategy* is not, and the demo will not tell you otherwise, because a
two-week run yields ~42 trades against a noise floor that needs ~900.

**Recommended framing for a demo run:** treat it as an operations rehearsal
with a fixed, short scope — verify order placement, that SL/TP really attach
exchange-side, reconciliation after a deliberate process kill, transfer
rebasing, the daily stop, the kill switch, and LLM cost per day. Give it 1-2
weeks, judge it on "did the machinery behave", and do not read its PnL as
evidence either way.

**Before risking anything real, the strategy needs a different entry.** The
current one is "buy a 15m impulse aligned with the 1h/4h trend", which this
data says is at or slightly below a coin flip on direction and actively
negative in the first hour. Fee-and-spread-paying intraday crypto momentum on
15m bars is a crowded, well-arbitraged space; a taker strategy needs an edge
well above 0.10% round-trip to survive there, and this one has none.

Two honest directions, in order of expected value:

1. **Find the edge first, then wire it in.** Use `research/edge_lab.py` as the
   bench: propose a hypothesis, test it against the null baselines and the
   walk-forward split, and only promote it to `agent/strategy.py` if it clears
   costs out-of-sample. The harness is now the cheap part.
2. **Stop paying taker.** Every result here pays the spread and 0.05%/side.
   A maker-based or funding-carry design changes the cost sign, which is the
   one lever big enough to matter at these effect sizes.

What is *not* worth doing is tuning the existing parameters. That search has
been run — 79 ways — and it is empty.

---

## Reproduce

```bash
python research/download_okx_history.py --out runtime/research/data \
  --days 730 --min-volume-usd 30000000 --max-symbols 26
python research/validate_features.py --data runtime/research/data --samples 30
python research/edge_report.py --data runtime/research/data --stage all
```

Full machine-readable output: `summary.json` in this directory.
