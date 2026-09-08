# Frozen strategy comparison and market-feed study — September 8, 2026

## Registered strategy comparison

Snapshot `sha256:646d170744e015416c6a06e26e3730ed0a76d57b32c500e39c01417482c0de64` contains 30 recorded sessions from June 30 through August 11, 2026. The selected cutoff was fixed before evaluation to keep the copied corpus below the host’s capacity budget. The snapshot copies 19,393,280 bytes. It yielded 86,998 regular-session IEX bars for eight symbols and **zero quotes**; 694 outside-session bars were excluded. All observations are historical backfill.

Registration occurred at `2026-09-08T06:00:01.433135+00:00` on build `dd71ae339fd2b0fff613a92656b5ba1c05351afd`. Cohort manifest: `13c2b58b22cd5f669d26d9f7611c4067304f6390b9e1666bf83821aa0393abe6`. Every one of the twelve predeclared arms was retained. Confirmation cannot start before `2026-09-09` and must use unseen forward evidence, unchanged proof/cost/FDR gates, and the registered controls. This run has no broker, network, proof-writing or winner-selection authority.

**Result: zero executed trades across all twelve arms.** This is an execution-blocked experiment, not a profitable flat strategy or twelve measured losing accounts. Net expectancy, realized win ratio, MFE/MAE and execution payoff are unavailable. The dominant refusal was the stressed cost/risk check. The configured 25 bps scenario divided by the 0.30 cost/risk limit requires at least 83.33 bps of stop distance; the grammar floor is 30 bps. These are configured assumptions, not a newly measured execution schedule. Widening stops just to pass this constraint would change the strategy and does not establish an edge.

| Family / arm | Variant suffix | First actionable signals | Cost/risk refusals | Incomplete feature windows | Executed trades |
| --- | --- | ---: | ---: | ---: | ---: |
| opening_range_breakout / 0 | `3578223ed48d29e5` | 203 | 203 | 18 | 0 |
| opening_range_breakout / 1 | `511ac0622f1dc0bc` | 201 | 201 | 22 | 0 |
| opening_range_breakout / 2 | `4d812cf5660e0c25` | 164 | 164 | 45 | 0 |
| opening_range_breakout / 3 | `026e91aa282aba27` | 201 | 201 | 22 | 0 |
| trend_pullback / 0 | `54a4944ddb2cc60e` | 216 | 216 | 23 | 0 |
| trend_pullback / 1 | `f7f19e02b4d866be` | 187 | 187 | 26 | 0 |
| trend_pullback / 2 | `1afa4bc3e16ea5dd` | 133 | 133 | 26 | 0 |
| trend_pullback / 3 | `8627ac15537e638f` | 133 | 133 | 26 | 0 |
| vwap_reversion / 0 | `918ecc939194518f` | 213 | 213 | 22 | 0 |
| vwap_reversion / 1 | `36845f38771a5266` | 210 | 210 | 25 | 0 |
| vwap_reversion / 2 | `348e64e69600cac7` | 192 | 192 | 25 | 0 |
| vwap_reversion / 3 | `e982e7a55f7d8ed1` | 192 | 192 | 25 | 0 |

The following table evaluates the entry signal before the execution veto. Each horizon is the arm’s authored maximum hold, fixed before inspecting results. Returns use the existing signed signal-close/horizon-close diagnostic with its next-bar lag and same-symbol, same-session-minute controls from other sessions. These are retrospective conditional-return diagnostics, not fill-based P&L. The cost hurdle is the configured 17 bps bar-reference assumption. Shared sessions and overlapping signals are correlated; counts are not independent portfolio trials. No multiple-testing-adjusted positive edge is claimed.

| Family / arm | Horizon (minutes) | Available observations | Gross mean (bps) | Minus matched control (bps) | Mean after assumed 17 bps (bps) |
| --- | ---: | ---: | ---: | ---: | ---: |
| opening_range_breakout / 0 | 60 | 163 | 4.33 | 5.08 | -12.67 |
| opening_range_breakout / 1 | 60 | 158 | 3.73 | 4.50 | -13.27 |
| opening_range_breakout / 2 | 60 | 124 | 2.01 | 1.17 | -14.99 |
| opening_range_breakout / 3 | 30 | 181 | 5.07 | 5.76 | -11.93 |
| trend_pullback / 0 | 60 | 165 | -1.00 | -1.06 | -18.00 |
| trend_pullback / 1 | 60 | 132 | 2.27 | 1.87 | -14.73 |
| trend_pullback / 2 | 60 | 89 | 7.43 | 7.20 | -9.57 |
| trend_pullback / 3 | 60 | 89 | 7.43 | 7.20 | -9.57 |
| vwap_reversion / 0 | 30 | 197 | -8.73 | -9.04 | -25.73 |
| vwap_reversion / 1 | 30 | 194 | -8.84 | -9.05 | -25.84 |
| vwap_reversion / 2 | 30 | 168 | -2.92 | -3.22 | -19.92 |
| vwap_reversion / 3 | 15 | 178 | -1.38 | -1.87 | -18.38 |

The fixed mechanisms are economically interpretable: opening continuation compares completed context and a shorter horizon; pullback compares structural reclaim and a trailing exit; reversion compares reversal confirmation, range context and a shorter horizon. They remain hypotheses. A gross conditional return smaller than the measured trading cost cannot support an investable edge. The current experiment cannot estimate the benefit of changing portfolio or signal-emission policy when the underlying entries do not clear execution viability.

## Paired IEX/SIP coverage

Both feeds were requested separately for SPY, QQQ, IWM, XLF, TLT and GLD over September 1–4. All requests succeeded. Each symbol had 1,560 expected regular-session minutes from the broker calendar. Raw responses and actual receipt times were retained as historical backfill. Volumes below compare only timestamps present on both feeds. The ranges are per-session statistics, not pooled quantiles.

| Symbol | IEX observed / expected | SIP observed / expected | IEX/SIP paired volume range | Absolute close difference, session p95 range (bps) |
| --- | ---: | ---: | ---: | ---: |
| GLD | 1296 / 1560 | 1560 / 1560 | 1.76%–2.25% | 3.50–5.83 |
| IWM | 1548 / 1560 | 1560 / 1560 | 5.81%–6.68% | 1.69–2.04 |
| QQQ | 1531 / 1560 | 1560 / 1560 | 1.84%–2.07% | 2.23–2.51 |
| SPY | 1559 / 1560 | 1560 / 1560 | 3.77%–4.60% | 0.65–1.04 |
| TLT | 1262 / 1560 | 1560 / 1560 | 4.63%–7.17% | 1.23–1.83 |
| XLF | 1536 / 1560 | 1560 / 1560 | 8.00%–9.83% | 2.56–3.13 |

This sample demonstrates that IEX volume is not consolidated market volume and that missing IEX bars can prevent completed 5/15-minute context for otherwise liquid instruments. It does not establish that switching feeds creates profit, or that the account has real-time SIP entitlement. A feed change needs a separate evidence identity and a fresh confirmation; historical SIP access alone is insufficient.

## Retained evidence

Raw registration, full arm results, normalized inputs and paired responses are retained locally under `outputs/trading-edge-completion-2026-09-08/` and on the host under `/app/runtime/research/experiments/`. They are not added to Git. The compact arm summary is suitable for the read-only dashboard diagnostics directory. The deployed runtime and completed CI are tracked in [the completion record](trading-edge-completion-2026-09-08.md).
