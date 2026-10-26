# Strategy and edge performance, refreshed on 26 October 2026

This refresh re-runs the 15 October review's research ([`../review-2026-10-15/README.md`](../review-2026-10-15/README.md), sections 4 and 6) on newer bars. It adds eight one-minute sessions, 14 to 23 October 2026, that no analysis had seen before. Everything here is diagnostic. The bars are public consolidated-tape bars, not the Alpaca IEX `forward_observed` corpus the repository's protocol requires, so nothing in this document is decision-bearing.

## 1. What was run

| item | detail |
| --- | --- |
| one-minute bars | 30 sessions, 14 September to 23 October 2026 (the review had 22). Every symbol-session has 362 to 390 bars |
| five-minute bars | 49 sessions, 17 August to 23 October (the review had 42) |
| hourly and daily bars | 486 complete hourly sessions from 29 October 2024; daily bars from January 2010 |
| fresh window | the 8 sessions from 14 to 23 October, reported separately |
| code | the review's scripts (`../review-2026-10-15/research/scripts/`) run from a frozen copy of commit `5702786`, so the repository's own evaluators are used unchanged |
| merge check | the 14 October and 25 October fetches agree on all 114,182 overlapping one-minute bars; 75 volume figures differ (0.07%) and no price differs (`tools/merge.py`) |
| reproduction check | restricted to the review's 22 sessions, the new replay reproduces the review's trade count and net mean for all 80 trading arms exactly (`tools/check_repro.py`: 0 mismatches) |

Costs are those of the review's realistic level: the larger of one tick and the per-symbol Corwin-Schultz spread, plus 1.0 bps slippage and 0.28 bps fees per round trip. The view is runtime-faithful: one trade per symbol-session, entry at the next bar's open, no entry at or after 15:00, flat at 15:50.

## 2. Headline

| measure | 30 sessions | review (22 sessions) | fresh window (8 sessions) |
| --- | --- | --- | --- |
| arms producing trades | 80 of 82 | 80 of 82 | 80 |
| arms with a positive mean net of costs | 3 (all opening-range fade) | 3 (all opening-range fade) | 23 |
| arms significantly positive (t above +2) | 0 | 0 | 0 |
| arms significantly negative (t below -2) | 34 | 47 | 4 |
| average arm, net bps per trade | -3.41 | -4.37 | -0.99 |
| rank correlation of arm results with the review's | | | **-0.35** |

**Verdict: still no edge.** Not one of 82 arms is significantly positive. More telling, the ranking of the arms inverted in the fresh window:

- the reversion families that led on 15 October lost money: opening-range fade -4.3 bps per trade, mean reversion -7.0 and VWAP reversion -3.1;
- the continuation families that lost before made money: IBR +5.7, VWAP trend +2.8 to +4.0 and momentum continuation +2.8.

A negative rank correlation between consecutive windows is what regime noise looks like. A persistent edge would keep its rank.

## 3. Scorecard by family (baseline and variant arms of the 24-arm cohort, and the IBR baseline)

| family | arm | trades | gross | net bps (t) | win | review, 22 sessions | fresh, 8 sessions (t, trades) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| cross_sectional_residual | baseline | 540 | +0.5 | -2.2 (-0.94) | 40% | -3.4 | +0.9 (+0.20, 144) |
| cross_sectional_residual | variant | 535 | +0.2 | -2.6 (-1.10) | 38% | -3.6 | +0.2 (+0.05, 144) |
| mean_reversion | baseline | 609 | -2.2 | -4.9 (-5.23) | 41% | -4.2 | -7.0 (-3.08, 152) |
| mean_reversion | variant | 603 | -2.4 | -5.1 (-5.89) | 41% | -4.4 | -7.2 (-3.95, 151) |
| momentum_continuation | baseline | 653 | -0.3 | -3.1 (-1.59) | 40% | -5.4 | +2.8 (+0.65, 181) |
| momentum_continuation | variant | 574 | -1.2 | -4.1 (-1.79) | 38% | -7.1 | +3.1 (+0.62, 167) |
| opening_drive | baseline | 283 | -2.4 | -5.4 (-1.55) | 37% | -4.7 | -6.7 (-0.72, 94) |
| opening_drive | variant | 201 | -4.5 | -7.6 (-2.12) | 34% | -6.5 | -9.1 (-1.09, 79) |
| opening_range_breakout | baseline | 671 | -1.3 | -4.1 (-2.17) | 41% | -5.2 | -1.2 (-0.27, 180) |
| opening_range_breakout | variant | 653 | -0.8 | -3.6 (-1.95) | 42% | -4.7 | -0.9 (-0.18, 178) |
| opening_range_fade | baseline | 171 | +6.0 | +3.0 (+0.89) | 49% | +5.8 | -4.3 (-1.14, 47) |
| opening_range_fade | variant | 74 | +9.4 | +6.4 (+1.30) | 53% | +9.0 | -4.0 (-0.56, 15) |
| range_expansion | baseline | 695 | -4.1 | -6.9 (-4.61) | 35% | -8.8 | -1.6 (-0.66, 186) |
| range_expansion | variant | 637 | -0.2 | -3.0 (-1.63) | 41% | -3.7 | -1.0 (-0.36, 158) |
| trend_pullback | baseline | 720 | -1.3 | -4.1 (-2.69) | 42% | -4.3 | -3.5 (-0.86, 192) |
| trend_pullback | variant | 720 | -1.0 | -3.8 (-2.06) | 42% | -5.0 | -0.6 (-0.11, 192) |
| volatility_breakout | baseline | 108 | -0.5 | -2.9 (-1.04) | 42% | -3.9 | +1.4 (+0.28, 19) |
| volatility_breakout | variant | 282 | +1.2 | -1.5 (-0.75) | 43% | -1.1 | -2.8 (-1.06, 69) |
| volume_breakout | baseline | 456 | +0.7 | -2.1 (-1.41) | 41% | -3.3 | +0.9 (+0.50, 129) |
| volume_breakout | variant | 433 | -0.0 | -2.9 (-1.96) | 41% | -3.9 | -0.3 (-0.19, 123) |
| vwap_reversion | baseline | 660 | +0.8 | -2.0 (-1.07) | 43% | -1.6 | -3.1 (-0.98, 183) |
| vwap_reversion | variant | 564 | +0.0 | -2.8 (-1.69) | 42% | -1.4 | -6.4 (-2.08, 163) |
| vwap_trend | baseline | 534 | -2.5 | -5.3 (-2.59) | 39% | -8.6 | +2.8 (+1.01, 151) |
| vwap_trend | variant | 410 | -3.0 | -5.9 (-2.72) | 37% | -10.1 | +4.0 (+1.99, 121) |
| ibr | ibr.baseline | 507 | -5.2 | -8.0 (-2.11) | 39% | -13.1 | +5.7 (+0.81, 138) |

All 82 arms, including the mechanism cohorts and the seven IBR variants, are in [`results/appendix_all_arms.md`](results/appendix_all_arms.md).

**The former lead, opening-range fade.** Over 30 sessions it is still the only family with a positive mean: +3.0 bps net for the baseline (t 0.89) and +6.4 for the variant (t 1.30). But it lost 4.3 bps per trade in the eight fresh sessions, and its 30-session figure is lower than the review's. The review's verdict stands: it is not a use of capital.

## 4. The sealed preregistered hypotheses

`research.preregistered.evaluate`, run with `decision_eligible=False`. Both are still short of the first formal look, which needs 30 sealed sessions.

| hypothesis | sealed sessions | 60-minute control-adjusted delta (t, sign-flip p) | 120-minute delta (t) | mirror at 60 minutes (t) | review (15 October) |
| --- | --- | --- | --- | --- | --- |
| `vwap-reversion-control-adjusted.v1` | 24 | +1.94 bps (0.55, p 0.29) | +6.97 bps (1.57) | -1.57 bps (-0.55) | +3.50 bps (0.92) on 16 sessions |
| `opening-range-fade-control-adjusted.v1` | 22 | +2.74 bps (0.45, p 0.34) | +2.32 bps (0.28) | -1.06 bps (-0.33) | +2.04 bps (0.36) on 14 sessions |

The VWAP-reversion effect has shrunk again: from +9.44 bps in sample, to +3.50 on 15 October, to +1.94 now. Both are far from significance. The review's finding D1-01 also applies: the decision rule and the measurement code changed after these windows were sealed, so even a pass on the v1 hypotheses would not be clean. New v2 registrations with a fresh sealed window are part of the gap work below.

## 5. Edges tested independently over long samples

| hypothesis | sample | result now |
| --- | --- | --- |
| Overnight versus intraday drift | 16 years, daily | Still the one robust premium. Overnight: SPY +3.57 bps a night (t 3.39), QQQ +5.04 (t 4.12), IWM +5.19 (t 4.02). Intraday drift is small and mostly insignificant. A book that is flat overnight gives this up |
| Market intraday momentum (first half hour predicts the last) | 486 hourly sessions | Dead: SPY slope -0.003 (t -0.13); pooled sign strategy +0.20 bps (t 0.92). GLD alone +1.7 bps (t 2.30) |
| Fade large opening gaps (beyond 2 sd) | 16 years | No robust effect; GLD and SLV gaps continue (t -2.12 and -1.60 for the fade) |
| Cross-sectional first-hour reversal | 486 sessions | +2.6 bps a day gross (t 0.84) before the costs of eight legs. Nothing |
| Professional VWAP reversion (volatility-scaled entry, VWAP target, 60-minute time stop) | 30 one-minute and 49 five-minute sessions | Net -2.6 and -1.0 bps per trade (gross +0.2 and +1.8) |
| Professional opening-range fade (structural stop) | same | Net -2.2 bps (t -1.95) and -2.3 (t -2.17) |
| Opening-range breakout on the first five-minute bar | 49 five-minute sessions | Net -3.7 bps (t -1.78) |

**Intraday mean reversion has weakened.** The 60-minute variance ratio on one-minute bars, after the first 15 minutes, is now 0.905 with a 95% session-bootstrap interval of [0.810, 1.053]. That interval includes 1, where the review's 0.885 [0.796, 0.980] did not. On five-minute bars over 49 sessions it is still below 1: 0.835 [0.758, 0.912]. Over two years of hourly bars it is about 1. Mild reversion is still present in recent weeks on the five-minute view, but it is not stable. The run labels in `results/variance_ratio.txt` and `results/pro_variants.log` still say "22 sessions" and "42 sessions": those are fixed strings in the scripts. The runs used 30 and 49 sessions.

## 6. The liquid-ETF reversion study (smoke runs, not evidence)

`studies/liquid_reversion/run.py` at the fixed middle grid point on the extended public bars ([`results/liquid_smoke-1m`](results/liquid_smoke-1m/report.md) and [`results/liquid_smoke-5m`](results/liquid_smoke-5m/report.md)).

| | one-minute | five-minute |
| --- | --- | --- |
| sessions (known day type) / traded | 30 (19) / 12 | 49 (38) / 22 |
| trades | 23 | 38 |
| net bps per trade, mean / median | -10.26 / -2.76 | -2.46 / -3.46 |
| sign-flip p (one-sided) | 0.93 | 0.77 |
| session mean at 2x cost | -22.4 | -6.7 |
| whole-rule minimum detectable effect at 2,500 sessions | 1.89 bps per trade | 1.75 bps per trade |

The five-minute smoke result moved from +0.40 to -2.46 bps per trade with the extra sessions. It is still a smoke run: the decision needs the ten-year SIP walk-forward on the VM.

## 7. Live and paper performance

I don't know the live or paper results since 11 October. Those are in the VM's runtime state and journals, which this environment cannot reach. At the last verified deployment (11 October) the paper account was paused and flat, the frozen ORB trial had no accepted sessions, and no strategy was eligible to trade (`../current-findings.md`).

## 8. Reproduce

```sh
# fetch into a new empty directory (one-minute bars only reach back 30 days)
python -I docs/review-2026-10-15/research/scripts/fetch.py DATA/bars_1m.json 1m 30 7
python -I docs/review-2026-10-15/research/scripts/fetch.py DATA/bars_5m.json 5m 59 30
python -I docs/review-2026-10-15/research/scripts/fetch.py DATA/bars_60m.json 60m 729 120
python -I docs/review-2026-10-15/research/scripts/fetch_daily.py DATA/bars_1d.json
python -I docs/research-2026-10-26/tools/merge.py OLD/bars_1m.json DATA/bars_1m.json MERGED/bars_1m.json

# replay and score, from a run directory containing data/ and results/
export REVIEW_BARS_1M=MERGED/bars_1m.json REVIEW_FRESH_FROM=2026-10-14
python -I docs/review-2026-10-15/research/scripts/replay.py results/replay 3
python -I docs/review-2026-10-15/research/scripts/analyze_replay.py results/replay data/bars_1m.json results/analysis.json
python -I docs/review-2026-10-15/research/scripts/runtime_faithful.py
python -I docs/research-2026-10-26/tools/check_repro.py results/replay docs/review-2026-10-15/research/results/runtime_faithful.json
python -I docs/research-2026-10-26/tools/scorecard.py results/runtime_faithful.json docs/review-2026-10-15/research/results/runtime_faithful.json results/scorecard.json
```

The 82-arm replay takes about 4.5 CPU-hours. Its per-arm output (about 99 MB) is not committed.
