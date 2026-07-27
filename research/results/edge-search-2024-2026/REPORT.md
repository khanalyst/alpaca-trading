# Edge search — day-trading horizons, OKX USDT perpetuals

**Brief:** find a tradable edge at day-trading horizons (up to 48 hours, no
longer), then set the parameters accordingly.

**Result:** no edge found. About 2,250 hypotheses were tested across eight
signal families and horizons from 15 minutes to 48 hours. One candidate
looked strong enough to change the answer — and its own placebo control
killed it. What *did* survive is a parameter finding, not a signal finding:
**allowing a 48-hour hold instead of 24 improves the existing strategy in
8 of 8 matched walk-forward cells**, worth roughly +0.11% per trade. That
change is applied. It reduces the loss rate; it does not create profit.

---

## 1. What was searched

Data: 27 instruments, 2024-07 to 2026-07, point-in-time top-10-by-turnover
universe, 691,046 tradable instrument-bars, median 10 instruments per bar —
the same shape the live agent sees.

Forward returns are measured from the **next bar's open** to the open N bars
later, so every number is executable. Horizons: 15m, 30m, 1h, 2h, 4h, 8h,
16h, 24h, 48h.

Signal families, ~30 predictors in total:

| Family | Predictors |
| --- | --- |
| Reversal / overextension | 1/4/16/96-bar return per ATR, distance from 1h EMA20 per ATR |
| Residual reversal | the same, with BTC beta removed (idiosyncratic move) |
| Momentum | 4/16/96/192-bar returns |
| Bar shape | close location in bar, range per ATR, gap, consecutive same-direction bars |
| Participation | relative volume, volume z-score, Amihud illiquidity |
| Volatility | ATR ratio, 24h range position |
| Carry / positioning | 8h-equivalent funding, open-interest change |
| Cross-sectional | per-timestamp ranks of the above across live instruments |

Four passes: 252 linear quintile spreads, 609 extreme-tail spreads, 1,160
regime- and session-conditioned spreads, 232 implementable top-N portfolios.

Discovery ran on the first 60% of history only. Everything was then
re-measured on the held-out final 40%, with a 48-hour purge gap.

## 2. Pass 1 — nothing clears cost

Of 252 feature/horizon pairs, 18 had a gross long/short spread exceeding the
0.10% round-trip taker cost. After controlling the false discovery rate at
10%, **zero** survived both significance and cost. Zero were confirmed
out-of-sample.

Encouragingly, the measurements themselves were stable — in-sample to
out-of-sample correlation of 0.671 with 77.4% sign consistency. The signals
are consistently measurable; they are consistently too small.

## 3. Pass 2 — a candidate that looked real

Conditioning on regime surfaced one coherent structure: among instruments
whose own 1h and 4h trends were both up, long the highest 24h range position
and short the lowest.

| Hold | In-sample gross | IS t | Out-of-sample gross | OOS t |
| ---: | ---: | ---: | ---: | ---: |
| 16h | +0.405% | 3.93 | +0.503% | 2.14 |
| 24h | +0.453% | 3.14 | +0.691% | 2.01 |
| 48h | +0.949% | 4.75 | +1.297% | 2.93 |

Three adjacent horizons, positive in both halves, t>2 in both, monotone in
horizon, stronger out-of-sample than in-sample, 8 of 9 quarters positive, and
still positive after stripping BTC beta out of the forward return (+0.80%
full-sample, t=3.19). By every check applied so far, it looked like an edge.

## 4. Why it was not real

Two controls killed it.

**The placebo.** Shuffling the signal randomly across instruments within each
timestamp — destroying all information, preserving everything else — still
produced **+0.455% at 48h with t=2.37**, about 41% of the "real" result.

The cause is a time-selection bias in the tail method. Decile thresholds were
computed on the distribution pooled across all time, and `range_pos` moves
together across instruments: in a strong up-market nearly everything sits
near its 24h high. So the pooled top decile preferentially samples bullish
moments and the pooled bottom decile bearish ones. Long-top / short-bottom
was harvesting market timing, not cross-sectional selection.

**The fix, and the verdict.** Ranking *within each timestamp* — both legs
drawn from the same moment, so market direction cancels by construction — and
using non-overlapping holds:

| Condition | Hold | Real signal | Placebo |
| --- | ---: | --- | --- |
| trend_up | 4h OOS | -0.019% (t=0.42) | **+0.182% (t=1.47)** |
| trend_up | 8h IS | +0.098% (t=1.02) | **+0.212% (t=1.64)** |
| trend_up | 16h full | +0.238% (t=1.02) | **+0.334% (t=1.00)** |
| none | 24h IS | -0.012% (t=-0.46) | **+0.213% (t=2.60)** |
| none | 48h full | +0.547% (t=1.20) | +0.435% (t=0.81) |

**The placebo beats the real signal in most cells, and reaches t=2.60 on pure
noise.** That is the number worth remembering: at this sample size and with
overlapping windows, t-statistics above 2 arise routinely from nothing. Every
"discovery" in this search sits inside that noise band.

The candidate's apparent +1.10% at 48h was roughly 80% methodological
artifact. What remains is +0.547% with t=1.20 — not significant, and barely
distinguishable from its own placebo.

## 5. What did survive: hold time

Not a signal, but a real and robust property of the existing strategy. Every
comparison below is matched on reward:risk and cost scenario:

| Metric | 48h minus 24h | Cells improved |
| --- | ---: | --- |
| In-sample expectancy | +0.084% | **8 / 8** |
| Out-of-sample expectancy | +0.159% | **8 / 8** |
| Full-sample expectancy | +0.108% | **8 / 8** |

Sign-consistent across every reward:risk level and both cost scenarios, in
both halves independently. The mechanism is visible in the exit mix: the 24h
clock was force-closing trades that had not yet resolved. Timeouts fall from
~29% of exits to ~20%.

Reward:risk interacts with it. At the 48h hold, a 3R target beat 2R in all
four matched comparisons; beyond 3R the gain reverses out-of-sample, so 3R is
a measured optimum rather than "wider is better".

Best configuration found (48h hold, 3R target):

| Costs | In-sample | Out-of-sample | Full |
| --- | ---: | ---: | ---: |
| frictionless | +0.009% (PF 1.005) | +0.234% (PF 1.122) | +0.107% |
| base (taker) | -0.242% (PF 0.881) | **-0.019% (PF 0.991)** | -0.145% |

Out-of-sample at ordinary costs this is nearly break-even, against -0.216%
(PF 0.877) for the shipped 24h/2R settings. That is a material improvement in
a losing strategy. It is not a profitable one.

## 6. Parameters changed

| Parameter | Was | Now | Why |
| --- | --- | --- | --- |
| `risk.max_hold_hours` | 24 | **48** | 8/8 IS, 8/8 OOS, 8/8 full-sample improvement |
| `strategy.fixed_reward_risk` | 2.0 | **3.0** | Beat 2R in 4/4 matched comparisons at the 48h hold |
| `strategy.extended_reward_risk` | 3.0 | **4.0** | Keeps the extended policy meaningfully wider than the default |
| `trading_costs.expected_hold_hours` | 8 | **20** | Measured average hold at the new settings is ~21h; 8h understated funding cost in sizing |

`max_hold_hours` is now hard-capped at 48 in `agent/config.py`. This is a
day-trading strategy; a multi-day hold has a different risk profile (weekend
gaps, accumulated funding, overnight news) and must not be reachable by
nudging one number.

## 7. Why no edge was found, and what would change it

Three structural reasons, in order of how much they matter:

1. **Cost.** Round-trip taker is 0.10% before spread and slippage. The
   honest cross-sectional spreads measured here are 0.05-0.3% gross with
   standard errors as large as the estimates. The edge and the fee are the
   same size. Maker execution at 0.04% round-trip flips several point
   estimates positive — but the agent is architecturally taker-only
   (marketable IOC with exchange-side SL/TP), so capturing that is a rewrite,
   not a config change. It is still the single biggest available lever.

2. **Data resolution.** Everything here is 15-minute OHLCV. Genuine intraday
   crypto edges live in data this repository does not collect: order-book
   imbalance, trade-flow signing, liquidation cascades, funding-settlement
   microstructure. No amount of re-arranging OHLCV recovers information that
   was never sampled.

3. **Breadth.** Cross-sectional strategies need instruments to rank against.
   The universe is 10 names, and after a regime filter 3-5 remain. That is
   far too thin for cross-sectional selection to overcome noise — visible in
   the implementable-form tests, where t-statistics collapse to ~0 once
   selection happens per-bar among the handful actually available.

## 8. Honest bottom line

The parameter changes are real, validated out-of-sample, and worth keeping.
They move the strategy from clearly losing toward break-even before costs and
roughly break-even out-of-sample after costs — which is exactly what removing
a self-inflicted handicap looks like.

They do not constitute an edge, and this search did not find one. The most
valuable output is arguably the placebo result: it establishes that on this
data, at these sample sizes, a t-statistic of 2.6 can come from pure noise.
Any future candidate — including one proposed by the LLM layer — has to clear
that bar before it means anything.

## Reproduce

```bash
python research/find_edge.py --data runtime/research/data          # pass 1
python research/deep_edge.py --data runtime/research/data          # passes 2-4
python research/validate_candidate.py --data runtime/research/data # adversarial
python research/unbiased_recheck.py --data runtime/research/data   # placebo-controlled
```
