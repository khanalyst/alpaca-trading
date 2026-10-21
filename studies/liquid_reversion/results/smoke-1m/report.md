# Liquid-ETF VWAP reversion study: smoke run (NOT EVIDENCE)

> **SMOKE RUN: NOT EVIDENCE. There are fewer sessions than one training block plus one test block, so no parameters were selected and nothing here was tested out of sample. It checks that the pipeline runs end to end on real bars; the numbers are far too few to support or reject the hypothesis.**

## Hypothesis

> On the five most liquid US equity ETFs (SPY, QQQ, IWM, DIA, VTI), on days classified as range days from the first 30 minutes alone, a fade of a volatility-scaled deviation from the session VWAP, entered after 10:00 and targeting the VWAP, earns positive expectancy net of realistic per-symbol costs.

## Data

- Source: public consolidated-tape chart bars as used by the 2026-10-15 review (diagnostic only, not Alpaca SIP).
- Input: `bars_1m.json` (1 file), 1-minute bars, fingerprint `f3eb83f5aea042d7...` (SHA-256 of each file's name and content, so it differs from a plain checksum of the file).
- Sessions: 22 in total, 2026-09-14 to 2026-10-13; regular hours only (09:30 to 16:00 New York).
- Early closes supplied: 0 (sessions with an early close feed the history but are not traded).
- Partial (provisional) month files used: not applicable.

| symbol | sessions | bars | min bars | max bars | short sessions | zero-volume bars | median close |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SPY | 22 | 8580 | 390 | 390 | 0 | 3 | 768.73 |
| QQQ | 22 | 8579 | 389 | 390 | 1 | 6 | 741.75 |
| IWM | 22 | 8580 | 390 | 390 | 0 | 9 | 281.56 |
| DIA | 22 | 8578 | 389 | 390 | 2 | 0 | 514.85 |
| VTI | 22 | 8580 | 390 | 390 | 0 | 0 | 378.15 |

Zero-volume bars add nothing to VWAP; a short session simply has fewer candidate bars.

## Rules (fixed, preregistered)

- Session VWAP: cumulative typical price x volume over volume from 09:30, completed bars only.
- Volatility: 1.4826 x median absolute bar log return over the current session's completed bars and the prior 5 sessions, in bps per minute (five-minute returns divided by the square root of 5).
- Score: z = (close - VWAP) / VWAP x 1e4 / (sigma x sqrt(60)).
- Day type at 10:00: drive and gap, each over its median for the previous 20 sessions (at least 10 needed); range day if both ratios are below k_drive and k_gap.
- Signal on a completed bar ending in [10:00, 15:00): range day, |z| >= k_entry, and the move to VWAP at least cost_multiple x round-trip cost. Short above VWAP, long below.
- One trade per symbol-session: the first qualifying signal consumes it, even if the entry is refused.
- Entry at the next bar's open, only if that bar starts before 15:00 and the open has not already crossed the frozen target.
- Target: VWAP frozen at the signal. Stop: entry +/- stop_mult x |entry - target|.
- Exit on bar highs and lows from the entry bar: stop first on a tie, a gap beyond a level fills at the open; time stop at the close of the bar ending 60 minutes after entry; flat at the bar ending 15:50.

## Costs

Round trip in bps of entry notional ($25,000 per trade, Alpaca charges no commission): one full quoted spread, max(one tick, measured spread; measured: none supplied, so one tick is used); slippage 0.5 bps per side; SEC Section 31 fee on the sell leg only ($20.60 per million); FINRA TAF $0.000195 per share sold, capped at $9.79 per trade. Cost stress multiplies the whole round trip.

| symbol | median close | spread | slippage | SEC fee | FINRA TAF | round trip (bps) |
| --- | --- | --- | --- | --- | --- | --- |
| SPY | 768.73 | 0.1301 | 1.0000 | 0.2060 | 0.0025 | 1.3386 |
| QQQ | 741.75 | 0.1348 | 1.0000 | 0.2060 | 0.0026 | 1.3434 |
| IWM | 281.56 | 0.3552 | 1.0000 | 0.2060 | 0.0069 | 1.5681 |
| DIA | 514.85 | 0.1942 | 1.0000 | 0.2060 | 0.0038 | 1.4040 |
| VTI | 378.15 | 0.2644 | 1.0000 | 0.2060 | 0.0052 | 1.4756 |

## Parameters

No selection: one fixed grid point, the middle of the grid (k_drive 0.8, k_gap 1.5, k_entry 1.5, cost_multiple 1.5, stop_mult 1.5). Combinations tried: 1. A walk-forward needs 630 sessions; this input has 22, of which 11 have a known day type.

## Results (smoke sample, in sample, not evidence)

| measure | value |
| --- | --- |
| trades | 15 |
| sessions available / traded | 11 / 7 |
| trades per traded session | 2.14 |
| net bps per trade: mean / median | -9.17 / -4.12 |
| gross bps per trade (mean) | -7.74 |
| cost bps per trade (mean) | 1.43 |
| session-level net bps: mean / sd | -19.65 / 42.94 |
| session-clustered t | -1.364 |
| sign-flip p (one-sided, sessions, 10,000 draws, seed 20261019) | 0.8227 |
| win rate | 0.33 |
| sides | long 8, short 7 |
| exit mix | stop 2, time 13 |
| refused entries (session consumed) | none |

### By symbol

| symbol | trades | sessions | net mean | gross mean | win rate | t |
| --- | --- | --- | --- | --- | --- | --- |
| DIA | 2 | 2 | -9.34 | -7.94 | 0.50 | -0.819 |
| IWM | 3 | 3 | -6.19 | -4.62 | 0.33 | -0.483 |
| QQQ | 2 | 2 | -30.89 | -29.54 | 0.00 | -1.765 |
| SPY | 4 | 4 | -9.49 | -8.15 | 0.25 | -1.097 |
| VTI | 4 | 4 | -0.14 | 1.33 | 0.50 | -0.023 |

### By year

| year | trades | sessions | net mean | gross mean | win rate | t |
| --- | --- | --- | --- | --- | --- | --- |
| 2026 | 15 | 7 | -9.17 | -7.74 | 0.33 | -1.364 |

### Cost stress

The same trades with every cost component multiplied by the stress factor.

| stress | net bps per trade | session mean | t | sign-flip p |
| --- | --- | --- | --- | --- |
| 1x | -9.17 | -19.65 | -1.364 | 0.8227 |
| 2x | -10.60 | -22.71 | -1.576 | 0.8717 |

### Power

Measured here: session-level sd 42.94 bps, 2.14 trades per traded session, 0.64 of available sessions traded. Assumed: one-sided alpha 0.05 and 80% power, normal approximation.

- Minimum detectable effect for the 7 traded sessions here: 18.83 bps per trade.
- Observed effect -9.17 bps per trade: not positive, so no sample size would detect it.

Sessions needed for a given true effect per trade, at the sd and trade rate measured here:

| effect (bps per trade) | test only: traded sessions | test only: calendar | whole rule: traded sessions | whole rule: calendar |
| --- | --- | --- | --- | --- |
| 0.50 | 9931 | 15606 | never | never |
| 1.00 | 2483 | 3902 | never | never |
| 2.00 | 621 | 976 | 878 | 1380 |
| 3.00 | 276 | 434 | 276 | 434 |

MDE with 2,500 calendar sessions (about ten years): 1.25 bps per trade for the significance test alone (2.68 bps per traded session); 1.85 bps per trade for the whole rule. The rule's 2x-cost check needs the net effect per trade to exceed the mean cost per trade (1.43 bps), so a smaller true effect never passes, at any sample size.

### Selection bias

Not computed: no selection took place in a smoke run.

## Preregistered decision rule

Deploy to paper only if the out-of-sample session-level mean net bps is above zero with sign-flip p < 0.05 AND the 2x cost stress stays above zero.

Not applicable: a smoke run is not an out-of-sample test. For reference only, the checks on this sample would read: session-level mean net bps above zero: no; sign-flip p below 0.05: no; session-level mean above zero at 2x cost: no. **Decision: none (do not deploy).**
