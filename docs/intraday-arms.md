# Every intraday arm, as last measured

The original intraday program (`research/`, `agent/contracts/rule.py` and `ibr.py`) has 82 registered arms: 12 rule families plus IBR, across six cohorts. This page holds their latest measured results. Nothing newer exists: the factory itself has never produced a passing candidate, and its nightly runs score every arm at the shipped 17 bps round trip (section 9 of [findings.md](findings.md) explains why that alone rules out a pass).

Measurement, done in October 2026 with the repository's own signal and exit code:

- 30 public consolidated one-minute sessions, 14 September to 23 October 2026; the last 8 had not been seen by any earlier analysis;
- runtime-faithful: the first trade per symbol and session, entry at the next bar's open, no entries from 15:00, flat at 15:50;
- realistic cost: the larger of one tick and the symbol's Corwin-Schultz spread, plus 1.0 bps slippage and 0.28 bps fees per round trip (about 2 to 3 bps, not 17).

Diagnostic only: the data are public bars, not the IEX corpus the protocol requires. Sources: [the 26 October appendix](https://github.com/khanalyst/alpaca-trading/blob/21d97293455b42ac2ef7298bf814b09b68680200/docs/research-2026-10-26/results/appendix_all_arms.md) and [the 28 October v3 replay](https://github.com/khanalyst/alpaca-trading/blob/21d97293455b42ac2ef7298bf814b09b68680200/docs/research-2026-10-28/README.md#4-did-the-corrections-change-performance-the-v3-cohort).

## Summary

| measure | 30 sessions | fresh 8 sessions |
| --- | --- | --- |
| arms producing trades | 80 of 82 | 80 |
| arms with a positive mean net of costs | 3 (all opening-range fade) | 23 |
| arms significantly positive (t above +2) | 0 | 0 |
| arms significantly negative (t below -2) | 34 | 4 |
| average arm, net bps a trade | -3.41 | -0.99 |

The arms' ranking in the fresh window was negatively correlated with their ranking before it (-0.35): what was best became worst.

## All 82 arms (26 October)

| cohort | family | arm | trades | net bps (t), 30 sessions | review, 22 sessions | fresh, 8 sessions (t, trades) |
| --- | --- | --- | --- | --- | --- | --- |
| cost_v1 | cross_sectional_residual | cross_sectional_residual#0 | 540 | -4.0 (-1.39) | -4.8 | -1.7 (-0.28, 144) |
| cost_v1 | mean_reversion | mean_reversion#0 | 220 | -2.0 (-2.52) | -2.0 | -2.1 (-0.99, 59) |
| cost_v1 | momentum_continuation | momentum_continuation#0 | 640 | -2.8 (-1.74) | -3.6 | -0.8 (-0.20, 177) |
| cost_v1 | opening_drive | opening_drive#0 | 238 | -2.1 (-0.49) | -4.6 | +3.2 (+0.28, 78) |
| cost_v1 | opening_range_breakout | opening_range_breakout#0 | 660 | -5.0 (-2.47) | -4.8 | -5.6 (-0.88, 177) |
| cost_v1 | opening_range_fade | opening_range_fade#0 | 21 | -3.7 (-1.10) | -4.7 | -2.6 (-0.38, 10) |
| cost_v1 | range_expansion | range_expansion#0 | 627 | -2.8 (-2.13) | -3.7 | -0.2 (-0.11, 173) |
| cost_v1 | trend_pullback | trend_pullback#0 | 701 | -3.7 (-2.11) | -3.3 | -4.6 (-0.85, 189) |
| cost_v1 | volatility_breakout | volatility_breakout#0 | 409 | -2.8 (-1.62) | -3.0 | -2.4 (-0.58, 122) |
| cost_v1 | volume_breakout | volume_breakout#0 | 338 | -2.9 (-1.26) | -5.2 | +1.6 (+0.39, 115) |
| cost_v1 | vwap_reversion | vwap_reversion#0 | 0 | n/a (n/a) | n/a | n/a (n/a, 0) |
| cost_v1 | vwap_trend | vwap_trend#0 | 689 | -1.4 (-0.65) | -1.7 | -0.6 (-0.12, 185) |
| deadline_v1 | mean_reversion | mean_reversion#0 | 720 | -1.3 (-0.90) | -0.1 | -4.8 (-1.05, 192) |
| deadline_v1 | opening_range_fade | opening_range_fade#0 | 109 | -1.2 (-0.41) | +0.1 | -3.8 (-0.55, 38) |
| deadline_v1 | vwap_reversion | vwap_reversion#0 | 562 | -2.6 (-2.92) | -2.4 | -2.9 (-1.51, 165) |
| diag24 | cross_sectional_residual | baseline | 540 | -2.2 (-0.94) | -3.4 | +0.9 (+0.20, 144) |
| diag24 | cross_sectional_residual | variant | 535 | -2.6 (-1.10) | -3.6 | +0.2 (+0.05, 144) |
| diag24 | mean_reversion | baseline | 609 | -4.9 (-5.23) | -4.2 | -7.0 (-3.08, 152) |
| diag24 | mean_reversion | variant | 603 | -5.1 (-5.89) | -4.4 | -7.2 (-3.95, 151) |
| diag24 | momentum_continuation | baseline | 653 | -3.1 (-1.59) | -5.4 | +2.8 (+0.65, 181) |
| diag24 | momentum_continuation | variant | 574 | -4.1 (-1.79) | -7.1 | +3.1 (+0.62, 167) |
| diag24 | opening_drive | baseline | 283 | -5.4 (-1.55) | -4.7 | -6.7 (-0.72, 94) |
| diag24 | opening_drive | variant | 201 | -7.6 (-2.12) | -6.5 | -9.1 (-1.09, 79) |
| diag24 | opening_range_breakout | baseline | 671 | -4.1 (-2.17) | -5.2 | -1.2 (-0.27, 180) |
| diag24 | opening_range_breakout | variant | 653 | -3.6 (-1.95) | -4.7 | -0.9 (-0.18, 178) |
| diag24 | opening_range_fade | baseline | 171 | +3.0 (+0.89) | +5.8 | -4.3 (-1.14, 47) |
| diag24 | opening_range_fade | variant | 74 | +6.4 (+1.30) | +9.0 | -4.0 (-0.56, 15) |
| diag24 | range_expansion | baseline | 695 | -6.9 (-4.61) | -8.8 | -1.6 (-0.66, 186) |
| diag24 | range_expansion | variant | 637 | -3.0 (-1.63) | -3.7 | -1.0 (-0.36, 158) |
| diag24 | trend_pullback | baseline | 720 | -4.1 (-2.69) | -4.3 | -3.5 (-0.86, 192) |
| diag24 | trend_pullback | variant | 720 | -3.8 (-2.06) | -5.0 | -0.6 (-0.11, 192) |
| diag24 | volatility_breakout | baseline | 108 | -2.9 (-1.04) | -3.9 | +1.4 (+0.28, 19) |
| diag24 | volatility_breakout | variant | 282 | -1.5 (-0.75) | -1.1 | -2.8 (-1.06, 69) |
| diag24 | volume_breakout | baseline | 456 | -2.1 (-1.41) | -3.3 | +0.9 (+0.50, 129) |
| diag24 | volume_breakout | variant | 433 | -2.9 (-1.96) | -3.9 | -0.3 (-0.19, 123) |
| diag24 | vwap_reversion | baseline | 660 | -2.0 (-1.07) | -1.6 | -3.1 (-0.98, 183) |
| diag24 | vwap_reversion | variant | 564 | -2.8 (-1.69) | -1.4 | -6.4 (-2.08, 163) |
| diag24 | vwap_trend | baseline | 534 | -5.3 (-2.59) | -8.6 | +2.8 (+1.01, 151) |
| diag24 | vwap_trend | variant | 410 | -5.9 (-2.72) | -10.1 | +4.0 (+1.99, 121) |
| ibr7 | ibr | ibr.baseline | 507 | -8.0 (-2.11) | -13.1 | +5.7 (+0.81, 138) |
| ibr7 | ibr | ibr.buffer.0bps | 552 | -7.6 (-2.13) | -12.5 | +5.6 (+0.85, 149) |
| ibr7 | ibr | ibr.buffer.10bps | 450 | -8.0 (-1.95) | -14.3 | +7.9 (+0.93, 129) |
| ibr7 | ibr | ibr.range.30 | 521 | -8.4 (-1.81) | -15.2 | +10.5 (+1.02, 139) |
| ibr7 | ibr | ibr.range.45 | 454 | -5.9 (-1.33) | -9.8 | +4.3 (+0.35, 126) |
| ibr7 | ibr | ibr.target.1_5r | 507 | -6.8 (-1.81) | -12.0 | +7.1 (+0.93, 138) |
| ibr7 | ibr | ibr.target.3r | 507 | -8.0 (-2.07) | -13.4 | +6.4 (+0.91, 138) |
| mech_v1 | opening_range_breakout | opening_continuation#0 | 677 | -4.6 (-2.10) | -5.6 | -1.9 (-0.38, 182) |
| mech_v1 | opening_range_breakout | opening_continuation#1 | 671 | -4.7 (-2.18) | -6.1 | -1.0 (-0.19, 182) |
| mech_v1 | opening_range_breakout | opening_continuation#2 | 644 | -5.0 (-3.19) | -5.1 | -4.6 (-1.14, 176) |
| mech_v1 | opening_range_breakout | opening_continuation#3 | 671 | -4.5 (-2.77) | -4.7 | -3.7 (-0.83, 182) |
| mech_v1 | trend_pullback | trend_pullback#0 | 720 | -4.3 (-2.39) | -5.9 | -0.1 (-0.03, 192) |
| mech_v1 | trend_pullback | trend_pullback#1 | 623 | -4.5 (-2.90) | -6.1 | -0.3 (-0.08, 173) |
| mech_v1 | trend_pullback | trend_pullback#2 | 495 | -5.1 (-3.57) | -6.3 | -2.3 (-0.68, 139) |
| mech_v1 | trend_pullback | trend_pullback#3 | 495 | -4.7 (-3.51) | -5.6 | -2.3 (-0.77, 139) |
| mech_v1 | vwap_reversion | fair_value_reversion#0 | 680 | -3.1 (-2.27) | -2.8 | -3.8 (-1.34, 186) |
| mech_v1 | vwap_reversion | fair_value_reversion#1 | 677 | -1.7 (-1.12) | -2.1 | -0.9 (-0.24, 186) |
| mech_v1 | vwap_reversion | fair_value_reversion#2 | 627 | -1.8 (-1.60) | -2.5 | +0.1 (+0.09, 178) |
| mech_v1 | vwap_reversion | fair_value_reversion#3 | 627 | -2.3 (-2.75) | -2.9 | -0.7 (-0.51, 178) |
| mech_v2 | cross_sectional_residual | cross_sectional_residual#0 | 540 | -2.3 (-1.15) | -2.9 | -0.6 (-0.16, 144) |
| mech_v2 | mean_reversion | mean_reversion#0 | 220 | -2.0 (-2.51) | -2.0 | -2.1 (-0.98, 59) |
| mech_v2 | momentum_continuation | momentum_continuation#0 | 640 | -2.5 (-1.96) | -3.1 | -1.1 (-0.41, 177) |
| mech_v2 | opening_drive | opening_drive#0 | 238 | -3.4 (-1.07) | -4.7 | -0.8 (-0.09, 78) |
| mech_v2 | opening_range_breakout | opening_range_breakout#0 | 660 | -4.0 (-2.42) | -4.1 | -3.7 (-0.86, 177) |
| mech_v2 | opening_range_fade | opening_range_fade#0 | 21 | -5.0 (-1.90) | -4.7 | -5.2 (-1.09, 10) |
| mech_v2 | range_expansion | range_expansion#0 | 627 | -3.0 (-2.43) | -3.7 | -1.1 (-0.64, 173) |
| mech_v2 | trend_pullback | trend_pullback#0 | 701 | -3.1 (-2.22) | -3.3 | -2.5 (-0.63, 189) |
| mech_v2 | volatility_breakout | volatility_breakout#0 | 409 | -0.8 (-0.53) | -1.5 | +0.8 (+0.20, 122) |
| mech_v2 | volume_breakout | volume_breakout#0 | 338 | -1.5 (-0.81) | -2.8 | +0.9 (+0.28, 115) |
| mech_v2 | vwap_reversion | vwap_reversion#0 | 0 | n/a (n/a) | n/a | n/a (n/a, 0) |
| mech_v2 | vwap_trend | vwap_trend#0 | 689 | -0.9 (-0.48) | -0.9 | -0.7 (-0.17, 185) |
| refresh_v1 | cross_sectional_residual | cross_sectional_residual#0 | 537 | -2.0 (-0.84) | -1.5 | -3.5 (-0.52, 142) |
| refresh_v1 | mean_reversion | mean_reversion#0 | 718 | -1.6 (-0.99) | -0.7 | -3.8 (-0.74, 192) |
| refresh_v1 | momentum_continuation | momentum_continuation#0 | 660 | -5.0 (-2.74) | -4.6 | -5.9 (-1.07, 183) |
| refresh_v1 | opening_drive | opening_drive#0 | 235 | -2.0 (-0.48) | -5.3 | +4.7 (+0.42, 78) |
| refresh_v1 | opening_range_breakout | opening_range_breakout#0 | 654 | -3.7 (-1.86) | -4.0 | -2.9 (-0.43, 175) |
| refresh_v1 | opening_range_fade | opening_range_fade#0 | 33 | +3.1 (+0.68) | -0.1 | +9.6 (+0.99, 11) |
| refresh_v1 | range_expansion | range_expansion#0 | 620 | -2.8 (-2.12) | -4.0 | +0.4 (+0.14, 167) |
| refresh_v1 | trend_pullback | trend_pullback#0 | 667 | -2.0 (-1.44) | -1.5 | -3.4 (-0.84, 183) |
| refresh_v1 | volatility_breakout | volatility_breakout#0 | 38 | -6.6 (-1.09) | -7.0 | -5.9 (-0.42, 14) |
| refresh_v1 | volume_breakout | volume_breakout#0 | 269 | -1.6 (-0.68) | -1.8 | -1.1 (-0.26, 84) |
| refresh_v1 | vwap_reversion | vwap_reversion#0 | 385 | -2.7 (-2.54) | -2.2 | -3.7 (-2.01, 123) |
| refresh_v1 | vwap_trend | vwap_trend#0 | 697 | -3.4 (-1.80) | -3.6 | -3.0 (-0.52, 186) |

## The corrected cohort `intraday-mechanisms.v3` (28 October)

Each v3 arm is its v2 parent with the review's signal corrections applied. Same 30 sessions and costs.

| family | v2 trades | v2 net bps (t) | v3 trades | v3 net bps (t) | v3 fresh 8 sessions | main v3 exit |
| --- | --- | --- | --- | --- | --- | --- |
| opening-range breakout | 660 | -3.97 (-2.42) | 660 | -4.51 (-2.26) | -5.01 | time 91% |
| opening-range fade | 21 | -4.97 (-1.90) | 109 | -0.33 (-0.13) | -1.28 | target 58% |
| momentum continuation | 640 | -2.52 (-1.96) | 518 | -2.91 (-1.62) | -3.78 | time 89% |
| mean reversion | 220 | -2.03 (-2.51) | 720 | -1.43 (-1.05) | -4.23 | target 66% |
| trend pullback | 701 | -3.06 (-2.22) | 391 | -2.08 (-0.99) | +1.52 | time 86% |
| volatility breakout | 409 | -0.85 (-0.53) | 409 | -1.88 (-1.09) | -1.39 | time 89% |
| volume breakout | 338 | -1.52 (-0.81) | 415 | -1.87 (-0.95) | +0.51 | time 85% |
| VWAP reversion | 0 | (never traded) | 562 | -2.91 (-3.31) | -2.92 | time 82% |
| VWAP trend | 689 | -0.88 (-0.48) | 689 | -0.95 (-0.46) | -0.07 | time 93% |
| range expansion | 627 | -2.99 (-2.43) | 627 | -2.64 (-2.03) | -0.56 | time 87% |
| opening drive | 238 | -3.42 (-1.07) | 238 | -2.33 (-0.57) | +3.25 | time 90% |
| cross-sectional residual | 540 | -2.29 (-1.15) | 450 | -4.96 (-1.76) | -1.60 | time 92% |

No v3 arm is positive over 30 sessions; three are significantly negative.
