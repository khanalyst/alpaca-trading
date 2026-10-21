# Liquid-ETF VWAP reversion study: smoke run (NOT EVIDENCE)

> **SMOKE RUN: NOT EVIDENCE. There are fewer sessions than one training block plus one test block, so no parameters were selected and nothing here was tested out of sample. It checks that the pipeline runs end to end on real bars; the numbers are far too few to support or reject the hypothesis.**

## Hypothesis

> On the five most liquid US equity ETFs (SPY, QQQ, IWM, DIA, VTI), on days classified as range days from the first 30 minutes alone, a fade of a volatility-scaled deviation from the session VWAP, entered after 10:00 and targeting the VWAP, earns positive expectancy net of realistic per-symbol costs.

## Data

- Source: public consolidated-tape chart bars as used by the 2026-10-15 review (diagnostic only, not Alpaca SIP).
- Input: `bars_5m.json` (1 file), 5-minute bars, fingerprint `5294ea75745f0897...` (SHA-256 of each file's name and content, so it differs from a plain checksum of the file).
- Sessions: 42 in total, 2026-08-17 to 2026-10-14; regular hours only (09:30 to 16:00 New York).
- Early closes supplied: 0 (sessions with an early close feed the history but are not traded).

| symbol | sessions | bars | min bars | max bars | short sessions | zero-volume bars | median close |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SPY | 42 | 3276 | 78 | 78 | 0 | 28 | 767.10 |
| QQQ | 42 | 3276 | 78 | 78 | 0 | 47 | 720.06 |
| IWM | 42 | 3276 | 78 | 78 | 0 | 9 | 286.35 |
| DIA | 42 | 3276 | 78 | 78 | 0 | 2 | 520.29 |
| VTI | 42 | 3276 | 78 | 78 | 0 | 3 | 378.05 |

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
| SPY | 767.10 | 0.1304 | 1.0000 | 0.2060 | 0.0025 | 1.3389 |
| QQQ | 720.06 | 0.1389 | 1.0000 | 0.2060 | 0.0027 | 1.3476 |
| IWM | 286.35 | 0.3492 | 1.0000 | 0.2060 | 0.0068 | 1.5620 |
| DIA | 520.29 | 0.1922 | 1.0000 | 0.2060 | 0.0037 | 1.4019 |
| VTI | 378.05 | 0.2645 | 1.0000 | 0.2060 | 0.0052 | 1.4757 |

## Parameters

No selection: one fixed grid point, the middle of the grid (k_drive 0.8, k_gap 1.5, k_entry 1.5, cost_multiple 1.5, stop_mult 1.5). Combinations tried: 1. A walk-forward needs 630 sessions; this input has 42, of which 31 have a known day type.

## Results (smoke sample, in sample, not evidence)

| measure | value |
| --- | --- |
| trades | 31 |
| sessions available / traded | 31 / 18 |
| trades per traded session | 1.72 |
| net bps per trade: mean / median | 0.40 / -3.58 |
| gross bps per trade (mean) | 1.83 |
| cost bps per trade (mean) | 1.43 |
| session-level net bps: mean / sd | 0.69 / 19.18 |
| session-clustered t | 0.152 |
| sign-flip p (one-sided, sessions, 10,000 draws, seed 20261019) | 0.4475 |
| win rate | 0.35 |
| sides | long 21, short 10 |
| exit mix | target 3, time 28 |
| refused entries (session consumed) | none |

### By symbol

| symbol | trades | sessions | net mean | gross mean | win rate | t |
| --- | --- | --- | --- | --- | --- | --- |
| DIA | 7 | 7 | -6.19 | -4.78 | 0.14 | -1.181 |
| IWM | 6 | 6 | 13.18 | 14.74 | 0.67 | 1.318 |
| QQQ | 4 | 4 | 6.29 | 7.63 | 0.50 | 0.638 |
| SPY | 7 | 7 | -3.01 | -1.67 | 0.29 | -0.722 |
| VTI | 7 | 7 | -3.92 | -2.44 | 0.29 | -1.056 |

### By year

| year | trades | sessions | net mean | gross mean | win rate | t |
| --- | --- | --- | --- | --- | --- | --- |
| 2026 | 31 | 18 | 0.40 | 1.83 | 0.35 | 0.152 |

### Cost stress

The same trades with every cost component multiplied by the stress factor.

| stress | net bps per trade | session mean | t | sign-flip p |
| --- | --- | --- | --- | --- |
| 1x | 0.40 | 0.69 | 0.152 | 0.4475 |
| 2x | -1.03 | -1.77 | -0.392 | 0.6545 |

### Power

Measured here: session-level sd 19.18 bps, 1.72 trades per traded session, 0.58 of available sessions traded. Assumed: one-sided alpha 0.05 and 80% power, normal approximation.

- Minimum detectable effect for the 18 traded sessions here: 6.53 bps per trade.
- Sessions needed to detect the observed 0.40 bps per trade: 4793 traded (8255 calendar) sessions.

Sessions needed for a given true effect per trade, at the sd and trade rate measured here:

| effect (bps per trade) | traded sessions | calendar sessions |
| --- | --- | --- |
| 0.50 | 3067 | 5283 |
| 1.00 | 767 | 1321 |
| 2.00 | 192 | 331 |
| 3.00 | 86 | 149 |

MDE with 2,500 calendar sessions (about ten years): 0.73 bps per trade (1.25 bps per traded session).

### Selection bias

Not computed: no selection took place in a smoke run.

## Preregistered decision rule

Deploy to paper only if the out-of-sample session-level mean net bps is above zero with sign-flip p < 0.05 AND the 2x cost stress stays above zero.

Not applicable: a smoke run is not an out-of-sample test. For reference only, the checks on this sample would read: session-level mean net bps above zero: yes; sign-flip p below 0.05: no; session-level mean above zero at 2x cost: no.
