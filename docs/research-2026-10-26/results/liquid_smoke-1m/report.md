# Liquid-ETF VWAP reversion study: smoke run (NOT EVIDENCE)

> **SMOKE RUN: NOT EVIDENCE. There are fewer sessions than one training block plus one test block, so no parameters were selected and nothing here was tested out of sample. It checks that the pipeline runs end to end on real bars; the numbers are far too few to support or reject the hypothesis.**

## Hypothesis

> On the five most liquid US equity ETFs (SPY, QQQ, IWM, DIA, VTI), on days classified as range days from the first 30 minutes alone, a fade of a volatility-scaled deviation from the session VWAP, entered after 10:00 and targeting the VWAP, earns positive expectancy net of realistic per-symbol costs.

## Data

- Source: public consolidated-tape chart bars as used by the 2026-10-15 review (diagnostic only, not Alpaca SIP).
- Input: `bars_1m.json` (1 file), 1-minute bars, fingerprint `8ed9d157fe78158a...` (SHA-256 of each file's name and content, so it differs from a plain checksum of the file).
- Sessions: 30 in total, 2026-09-14 to 2026-10-23; regular hours only (09:30 to 16:00 New York).
- Early closes supplied: 0 (sessions with an early close feed the history but are not traded).
- Partial (provisional) month files used: not applicable.

| symbol | sessions | bars | min bars | max bars | short sessions | zero-volume bars | median close |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SPY | 30 | 11698 | 389 | 390 | 2 | 5 | 768.40 |
| QQQ | 30 | 11697 | 389 | 390 | 3 | 9 | 738.83 |
| IWM | 30 | 11698 | 389 | 390 | 2 | 12 | 279.05 |
| DIA | 30 | 11696 | 389 | 390 | 4 | 2 | 516.10 |
| VTI | 30 | 11698 | 389 | 390 | 2 | 2 | 377.55 |

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
| SPY | 768.40 | 0.1301 | 1.0000 | 0.2060 | 0.0025 | 1.3387 |
| QQQ | 738.83 | 0.1353 | 1.0000 | 0.2060 | 0.0026 | 1.3440 |
| IWM | 279.05 | 0.3584 | 1.0000 | 0.2060 | 0.0070 | 1.5713 |
| DIA | 516.10 | 0.1938 | 1.0000 | 0.2060 | 0.0038 | 1.4035 |
| VTI | 377.55 | 0.2649 | 1.0000 | 0.2060 | 0.0052 | 1.4760 |

## Parameters

No selection: one fixed grid point, the middle of the grid (k_drive 0.8, k_gap 1.5, k_entry 1.5, cost_multiple 1.5, stop_mult 1.5). Combinations tried: 1. A walk-forward needs 630 sessions; this input has 30, of which 19 have a known day type.

## Results (smoke sample, in sample, not evidence)

| measure | value |
| --- | --- |
| trades | 23 |
| sessions available / traded | 19 / 12 |
| trades per traded session | 1.92 |
| net bps per trade: mean / median | -10.26 / -2.76 |
| gross bps per trade (mean) | -8.83 |
| cost bps per trade (mean) | 1.42 |
| session-level net bps: mean / sd | -19.66 / 41.87 |
| session-clustered t | -1.712 |
| sign-flip p (one-sided, sessions, 10,000 draws, seed 20261019) | 0.9302 |
| win rate | 0.39 |
| sides | long 9, short 14 |
| exit mix | stop 4, stop_gap 1, target 1, time 17 |
| refused entries (session consumed) | none |

### By symbol

| symbol | trades | sessions | net mean | gross mean | win rate | t |
| --- | --- | --- | --- | --- | --- | --- |
| DIA | 5 | 5 | -5.56 | -4.15 | 0.60 | -0.547 |
| IWM | 4 | 4 | -13.89 | -12.31 | 0.25 | -1.168 |
| QQQ | 4 | 4 | -28.69 | -27.34 | 0.25 | -1.658 |
| SPY | 5 | 5 | -8.14 | -6.80 | 0.20 | -1.192 |
| VTI | 5 | 5 | 0.57 | 2.05 | 0.60 | 0.122 |

### By year

| year | trades | sessions | net mean | gross mean | win rate | t |
| --- | --- | --- | --- | --- | --- | --- |
| 2026 | 23 | 12 | -10.26 | -8.83 | 0.39 | -1.712 |

### Cost stress

The same trades with every cost component multiplied by the stress factor.

| stress | net bps per trade | session mean | t | sign-flip p |
| --- | --- | --- | --- | --- |
| 1x | -10.26 | -19.66 | -1.712 | 0.9302 |
| 2x | -11.68 | -22.39 | -1.949 | 0.9462 |

### Power

Measured here: session-level sd 41.87 bps, 1.92 trades per traded session, 0.63 of available sessions traded. Assumed: one-sided alpha 0.05 and 80% power, normal approximation.

- Minimum detectable effect for the 12 traded sessions here: 15.68 bps per trade.
- Observed effect -10.26 bps per trade: not positive, so no sample size would detect it.

Sessions needed for a given true effect per trade, at the sd and trade rate measured here:

| effect (bps per trade) | test only: traded sessions | test only: calendar | whole rule: traded sessions | whole rule: calendar |
| --- | --- | --- | --- | --- |
| 0.50 | 11802 | 18687 | never | never |
| 1.00 | 2951 | 4673 | never | never |
| 2.00 | 738 | 1169 | 1020 | 1615 |
| 3.00 | 328 | 520 | 328 | 520 |

MDE with 2,500 calendar sessions (about ten years): 1.37 bps per trade for the significance test alone (2.62 bps per traded session); 1.89 bps per trade for the whole rule. The rule's 2x-cost check needs the net effect per trade to exceed the mean cost per trade (1.42 bps), so a smaller true effect never passes, at any sample size.

### Selection bias

Not computed: no selection took place in a smoke run.

## Preregistered decision rule

Deploy to paper only if the out-of-sample session-level mean net bps is above zero with sign-flip p < 0.05 AND the 2x cost stress stays above zero.

Not applicable: a smoke run is not an out-of-sample test. For reference only, the checks on this sample would read: session-level mean net bps above zero: no; sign-flip p below 0.05: no; session-level mean above zero at 2x cost: no. **Decision: none (do not deploy).**
