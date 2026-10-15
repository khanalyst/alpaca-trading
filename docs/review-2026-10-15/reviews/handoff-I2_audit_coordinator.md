# Review handoff: I2 audit evidence (coordinator review)

Method: the coordinator did this directly.

- Read `docs/audit-2026-09-21/README.md`, `SIGNAL-VALUE.md`, `FORWARD-2026-09-23.md`, `docs/preregistered-hypotheses.md`, `docs/current-findings.md` and `docs/trader-research-v6.md` in full, and the audit scripts `fetch60.py`, `harness.py` (first 80 lines) and `forward_costs.py` (first 60 lines).
- Loaded the JSON results programmatically.
- Re-measured the same quantities on fresh, independent data (`docs/review-2026-10-15/research`).

## Numbers checked against the committed JSON (all match)

| claim (document) | JSON value |
| --- | --- |
| ORF baseline, bracket replay, realistic cost: "+10.2 bps per trade over 59 trades, clustered t +2.57" (preregistered-hypotheses.md, FORWARD) | `forward-costs-2026-09-23.json`, examined real n 59, mean 10.230, t 2.566 |
| ORF variant: +17.94 bps, 27 trades, t +2.99 (FORWARD) | n 27, mean 17.944, t 2.989 |
| VWAP reversion baseline: +0.92 bps, 363 trades, t +0.47 (FORWARD) | n 363, mean 0.920, t 0.465 |
| VWAP reversion forward: +0.02 bps over 44 trades | forward_real n 44, mean 0.023 |
| ORF forward: 8 trades, -5.2 bps | forward_real n 8, mean -5.219 |
| VWAP reversion 60 minutes: +9.44 bps, clustered t 4.34, df 17, sign-flip p 0.0010 (SIGNAL-VALUE) | `step6c-clustered-signal-quality.json`, n 350, delta 9.443, cluster_t 4.337, df 17, p 0.0010 |
| ORF 60 minutes: +19.51 bps, clustered t 2.95 | n 88, delta 19.512, cluster_t 2.951 |
| VWAP trend 60 minutes: -13.12 bps, clustered t -6.36 | n 214, delta -13.116, cluster_t -6.365 |

The documents report the evidence faithfully.

## Findings

### I2-01 The audit bracket replay skipped the first 60 minutes of every session; the motivating population is not the registered rule's (MEDIUM, VERIFIED)

- `docs/audit-2026-09-21/forward_costs.py:41` loops `for i in range(60, len(sr) - 1):`. `harness.py` uses `warm=60` the same way.
- The registered ORF subject `d5785d9e70b56def` has `entry_after_minutes 0` and a 20-minute range, so the runtime can fire from 09:50.
- In fresh data (22 sessions, runtime-faithful first trade per symbol-session):
  - 35% of ORF baseline first trades signal before 10:30, and perform about the same as later ones (+5.9 bps net, versus +5.7 after 10:30). The skip therefore did not materially bias ORF, but a third of the runtime population was never in the motivating sample.
  - For ORB, 68% of first signals fire before 10:30, and those lose more (-6.2 bps versus -3.1 bps). The skip therefore made the ORB mirror look less negative than the traded rule.
- Signal-quality evidence (`step6c`) uses `measure_signal_quality` without the skip, so it is unaffected.

### I2-02 The in-sample effects decayed sharply out of sample, as selection predicts (HIGH for decision-making, VERIFIED)

| quantity, 60 minutes, control-adjusted | in-sample (18 sessions, 26 Aug to 21 Sep) | sealed window, public data (14 to 16 sessions) | fresh 22 sessions (14 Sep to 13 Oct) |
| --- | --- | --- | --- |
| VWAP reversion | +9.44 bps (t 4.34) | +3.50 bps (t 0.92) | +3.24 bps (t 1.12) |
| ORF | +19.51 bps (t 2.95) | +2.04 bps (t 0.36) | +9.84 bps (t 1.42) |
| VWAP trend (mirror) | -13.12 bps (t -6.36) | -4.43 bps (t -1.23) | -5.62 bps (t -1.90) |

The signs held, but the magnitudes fell by 60 to 90%. SIGNAL-VALUE.md concluded "The grammar is not dead" from 11 hits across 24 arms x 5 horizons on 18 sessions in one regime. That was a reasonable *lead*, not evidence of a tradeable edge: as traded (bracket, realistic cost) the same VWAP subject loses 1.6 bps per trade in fresh data.

### I2-03 Bar-based spread estimates are not a cost source, as the audit says, but they were used to label results "real" (LOW, VERIFIED)

`forward_costs.py` sets the realistic spread as the maximum of one tick and the Corwin-Schultz high-low estimate (`spread-reference.json`, explicitly `authorizing: false`). This is reasonable as a diagnostic. The "real" column inherits this caveat, which the document states.

### I2-04 Stale or superseded statements in `docs/` (LOW, VERIFIED)

- `docs/current-findings.md:5` says "The exact image passed 719 tests across 49 modules". The current head discovers 2,359 tests in about 164 modules, and one fails (`tests/research/test_llm_strategy.py:156`).
- `docs/trader-research-v6.md:26` cites a "preserved 25 bps stressed admission scenario". Every shipped config uses 9 bps (`config.yaml:52`); 25 bps is the hard-coded proof-time stress in `research/gates.py:154` (D2-01).
- `docs/trader-research-v6.md` describes the v2 cohort without the 10:00 thesis deadline defect (A-01) or the `trailing_stop_r 1.5` on every arm.
- The audit README says the preregistered windows are sealed. Interim results on sealed sessions were committed (`prereg-interim-2026-09-22.json`, `-09-23.json`), and a decision rule was added after the first sealed interim (D1-01).

## Summary

- The audit's numbers are reproduced exactly from its JSON, and the documents report them honestly, including caveats and one self-correction (the clustered-t correction of 22 September).
- The conclusions drawn for action (preregister VWAP reversion and ORF, make ORF the paper incumbent) rested on in-sample effects that have since shrunk by 60 to 90% on fresh data and do not survive realistic costs as traded.
- The audit's bracket replay skipped the first hour. This is harmless for ORF, but it flattered ORB.
