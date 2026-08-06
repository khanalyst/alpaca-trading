# momentum.stop.atr_1_5

Status: superseded
Hypothesis: A 1.0 ATR stop sits inside ordinary noise, so a materially wider floor converts stop-outs into trades that were right but too early.
Overrides: strategy.min_stop_atr_multiple = 1.5

## Sample

Registered but never run. No sample, and therefore no result to report.

## Findings log

- 2026-08-05: Retired 2026-08-05 on live demo evidence: the momentum order path closed 35 trades for 4 wins (11.4%) at -0.974% per trade, taking the demo account from 71,099 to 64,720, so this arm would re-measure the sizing of a contract whose directional edge is already negative.
