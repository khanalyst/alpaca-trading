# Phase1-v2 deterministic edge backtest

> **Historical result snapshot.** This legacy cross-check is preserved for
> reproducibility. It is not current runtime or promotion documentation.

## Scope

- Period: 2024-08-16T09:30:00+00:00 to 2026-07-27T09:00:00+00:00 UTC
- Instruments: AAVE/USDT:USDT, BTC/USDT:USDT, DOGE/USDT:USDT, ETH/USDT:USDT, SOL/USDT:USDT, XRP/USDT:USDT
- Completed 15m bars per instrument: 70,080
- Deterministic qualifying candidates: 61,305
- Entry: next 15m bar open; maximum hold: 24 hours.
- Same-bar SL+TP ambiguity: stop assumed first.
- Funding-squeeze setup: not tested because historical open interest is unavailable.
- LLM selector and LLM early closes: not replayed.

## Non-overlapping setup event study

| Setup | Exit | Costs | Trades | Win % | Exp R | 95% CI | PF |
|---|---|---:|---:|---:|---:|---:|---:|
| trend_continuation | fixed_rr | frictionless | 4115 | 38.91 | 0.014 | [-0.027, 0.057] | 0.985 |
| trend_continuation | fixed_rr | base | 4133 | 36.90 | -0.091 | [-0.135, -0.045] | 0.806 |
| trend_continuation | fixed_rr | entry_cap | 4735 | 28.13 | -0.248 | [-0.295, -0.200] | 0.589 |
| trend_continuation | extended_rr | frictionless | 3772 | 35.66 | 0.021 | [-0.030, 0.074] | 0.998 |
| trend_continuation | extended_rr | base | 3804 | 33.44 | -0.090 | [-0.144, -0.035] | 0.810 |
| trend_continuation | extended_rr | entry_cap | 4368 | 25.34 | -0.251 | [-0.303, -0.196] | 0.591 |
| trend_continuation | structure_target | frictionless | 4106 | 38.75 | 0.013 | [-0.028, 0.056] | 0.984 |
| trend_continuation | structure_target | base | 4123 | 36.72 | -0.092 | [-0.136, -0.045] | 0.805 |
| trend_continuation | structure_target | entry_cap | 4730 | 28.05 | -0.248 | [-0.294, -0.199] | 0.591 |
| range_breakout | fixed_rr | frictionless | 3452 | 41.08 | 0.010 | [-0.033, 0.054] | 0.966 |
| range_breakout | fixed_rr | base | 3472 | 38.80 | -0.081 | [-0.123, -0.038] | 0.794 |
| range_breakout | fixed_rr | entry_cap | 3694 | 32.40 | -0.192 | [-0.232, -0.152] | 0.616 |
| range_breakout | extended_rr | frictionless | 3372 | 39.86 | 0.022 | [-0.028, 0.075] | 0.983 |
| range_breakout | extended_rr | base | 3394 | 37.57 | -0.069 | [-0.120, -0.015] | 0.812 |
| range_breakout | extended_rr | entry_cap | 3645 | 31.19 | -0.185 | [-0.233, -0.138] | 0.623 |
| range_breakout | structure_target | frictionless | 3452 | 41.08 | 0.010 | [-0.033, 0.054] | 0.966 |
| range_breakout | structure_target | base | 3472 | 38.80 | -0.081 | [-0.123, -0.038] | 0.794 |
| range_breakout | structure_target | entry_cap | 3694 | 32.40 | -0.192 | [-0.232, -0.152] | 0.616 |
| combined | fixed_rr | frictionless | 5428 | 39.81 | 0.014 | [-0.021, 0.049] | 0.986 |
| combined | fixed_rr | base | 5473 | 37.68 | -0.084 | [-0.123, -0.047] | 0.808 |
| combined | fixed_rr | entry_cap | 6205 | 29.90 | -0.226 | [-0.266, -0.186] | 0.602 |
| combined | extended_rr | frictionless | 5074 | 37.05 | 0.021 | [-0.024, 0.065] | 0.991 |
| combined | extended_rr | base | 5126 | 34.94 | -0.082 | [-0.129, -0.037] | 0.808 |
| combined | extended_rr | entry_cap | 5821 | 27.71 | -0.221 | [-0.265, -0.177] | 0.613 |
| combined | structure_target | frictionless | 5418 | 39.68 | 0.013 | [-0.022, 0.048] | 0.984 |
| combined | structure_target | base | 5464 | 37.55 | -0.085 | [-0.123, -0.047] | 0.807 |
| combined | structure_target | entry_cap | 6200 | 29.84 | -0.225 | [-0.265, -0.185] | 0.603 |

## Executable portfolio proxy (fixed 2R)

| Costs | Trades | Return % | CAGR % | Max DD % | Exp R | PF | Sharpe | Killed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| frictionless | 162 | -14.75 | -7.88 | 15.01 | -0.107 | 0.671 | -1.38 | True |
| base | 49 | -14.94 | -7.99 | 15.14 | -0.416 | 0.254 | -1.76 | True |
| entry_cap | 39 | -15.06 | -8.05 | 15.06 | -0.519 | 0.156 | -1.86 | True |

## Full-period diagnostic with drawdown kill disabled

This is not executable agent behavior; it shows whether the early kill hid a later recovery.

| Costs | Trades | Return % | Max DD % | Exp R | PF | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| frictionless | 393 | 1.87 | 16.03 | -0.006 | 1.052 | 0.14 |
| base | 392 | -25.09 | 29.16 | -0.116 | 0.810 | -1.07 |
| entry_cap | 487 | -57.19 | 57.72 | -0.237 | 0.623 | -2.87 |

## Verdict

**NO RELIABLE EDGE DEMONSTRATED**

The ordinary-cost executable proxy did not satisfy positive expectancy, profit factor, uncertainty and drawdown criteria. The safety controls can limit losses, but they do not create a profitable trading edge by themselves.

## Important limitations

1. This tests deterministic admissibility rules, not the complete agentic strategy. The LLM can accept, reject or close trades.
2. The six-instrument fixed universe is not the live hourly top-10 volume universe, so universe-selection effects are absent.
3. Historical order-book spread/depth and open interest are absent. Execution is tested through declared slippage scenarios instead.
4. A current LLM deciding old data may know later history from training, so historical LLM replay would not be clean evidence.
5. This is retrospective research, not proof of future returns.
