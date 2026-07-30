# Phase1-v2 deterministic edge backtest (commit 6528626)

## Scope

- Period: 2025-01-21T00:45:00+00:00 to 2026-07-01T00:00:00+00:00 UTC
- Instruments: AAVE/USDT:USDT, BTC/USDT:USDT, DOGE/USDT:USDT, ETH/USDT:USDT, SOL/USDT:USDT, XRP/USDT:USDT
- Completed 15m bars per instrument: 52,416
- Deterministic qualifying candidates: 44,881
- Entry: next 15m bar open; maximum hold: 24 hours.
- Same-bar SL+TP ambiguity: stop assumed first.
- Funding-squeeze setup: not tested because historical open interest is unavailable.
- LLM selector and LLM early closes: not replayed.

## Non-overlapping setup event study

| Setup | Exit | Costs | Trades | Win % | Exp R | 95% CI | PF |
|---|---|---:|---:|---:|---:|---:|---:|
| trend_continuation | fixed_rr | frictionless | 3036 | 38.90 | 0.011 | [-0.040, 0.065] | 0.974 |
| trend_continuation | fixed_rr | base | 3053 | 36.65 | -0.096 | [-0.150, -0.039] | 0.790 |
| trend_continuation | fixed_rr | entry_cap | 3499 | 27.67 | -0.256 | [-0.312, -0.198] | 0.566 |
| trend_continuation | extended_rr | frictionless | 2780 | 35.72 | 0.017 | [-0.046, 0.083] | 0.975 |
| trend_continuation | extended_rr | base | 2805 | 33.30 | -0.096 | [-0.160, -0.026] | 0.784 |
| trend_continuation | extended_rr | entry_cap | 3217 | 24.68 | -0.260 | [-0.320, -0.196] | 0.560 |
| trend_continuation | structure_target | frictionless | 3027 | 38.69 | 0.010 | [-0.042, 0.065] | 0.972 |
| trend_continuation | structure_target | base | 3044 | 36.43 | -0.097 | [-0.152, -0.039] | 0.788 |
| trend_continuation | structure_target | entry_cap | 3494 | 27.56 | -0.255 | [-0.312, -0.196] | 0.568 |
| range_breakout | fixed_rr | frictionless | 2531 | 40.93 | 0.004 | [-0.047, 0.055] | 0.962 |
| range_breakout | fixed_rr | base | 2544 | 38.33 | -0.089 | [-0.141, -0.037] | 0.782 |
| range_breakout | fixed_rr | entry_cap | 2715 | 32.23 | -0.198 | [-0.247, -0.147] | 0.608 |
| range_breakout | extended_rr | frictionless | 2475 | 39.72 | 0.016 | [-0.046, 0.080] | 0.980 |
| range_breakout | extended_rr | base | 2491 | 37.13 | -0.076 | [-0.140, -0.011] | 0.802 |
| range_breakout | extended_rr | entry_cap | 2679 | 31.02 | -0.189 | [-0.247, -0.129] | 0.618 |
| range_breakout | structure_target | frictionless | 2531 | 40.93 | 0.004 | [-0.047, 0.055] | 0.962 |
| range_breakout | structure_target | base | 2544 | 38.33 | -0.089 | [-0.141, -0.037] | 0.782 |
| range_breakout | structure_target | entry_cap | 2715 | 32.23 | -0.198 | [-0.247, -0.147] | 0.608 |
| combined | fixed_rr | frictionless | 4001 | 39.69 | 0.012 | [-0.033, 0.059] | 0.990 |
| combined | fixed_rr | base | 4027 | 37.37 | -0.088 | [-0.136, -0.039] | 0.803 |
| combined | fixed_rr | entry_cap | 4559 | 29.72 | -0.229 | [-0.278, -0.179] | 0.591 |
| combined | extended_rr | frictionless | 3734 | 36.77 | 0.012 | [-0.044, 0.070] | 0.972 |
| combined | extended_rr | base | 3767 | 34.67 | -0.090 | [-0.149, -0.030] | 0.790 |
| combined | extended_rr | entry_cap | 4263 | 27.47 | -0.227 | [-0.279, -0.172] | 0.599 |
| combined | structure_target | frictionless | 3991 | 39.51 | 0.011 | [-0.034, 0.058] | 0.987 |
| combined | structure_target | base | 4017 | 37.19 | -0.089 | [-0.138, -0.041] | 0.801 |
| combined | structure_target | entry_cap | 4554 | 29.64 | -0.229 | [-0.277, -0.178] | 0.593 |

## Executable portfolio proxy (fixed 2R)

| Costs | Trades | Return % | CAGR % | Max DD % | Exp R | PF | Sharpe | Killed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| frictionless | 33 | -14.95 | -10.63 | 15.21 | -0.354 | 0.188 | -1.70 | True |
| base | 29 | -14.83 | -10.55 | 15.06 | -0.464 | 0.167 | -1.95 | True |
| entry_cap | 26 | -15.03 | -10.70 | 15.18 | -0.601 | 0.048 | -1.81 | True |

## Full-period diagnostic with drawdown kill disabled

This is not executable agent behavior; it shows whether the early kill hid a later recovery.

| Costs | Trades | Return % | Max DD % | Exp R | PF | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| frictionless | 2044 | -5.74 | 28.60 | 0.015 | 0.989 | 0.13 |
| base | 2048 | -86.58 | 87.08 | -0.103 | 0.762 | -3.45 |
| entry_cap | 2321 | -98.99 | 99.03 | -0.237 | 0.573 | -7.72 |

## Verdict

**NO RELIABLE EDGE DEMONSTRATED**

The ordinary-cost executable proxy did not satisfy positive expectancy, profit factor, uncertainty and drawdown criteria. The safety controls can limit losses, but they do not create a profitable trading edge by themselves.

## Important limitations

1. This tests deterministic admissibility rules, not the complete agentic strategy. The LLM can accept, reject or close trades.
2. The six-instrument fixed universe is not the live hourly top-10 volume universe, so universe-selection effects are absent.
3. Historical order-book spread/depth and open interest are absent. Execution is tested through declared slippage scenarios instead.
4. A current LLM deciding old data may know later history from training, so historical LLM replay would not be clean evidence.
5. This is retrospective research, not proof of future returns.
