# momentum.cond.vol_regime

Status: candidate
Hypothesis: Momentum expectancy has opposite signs either side of the volatility regime, so the pooled figure is the average of two populations and lands near zero. Partition executed trades on realised_vol_ratio_8_96 into the compression, neutral and expansion buckets pre-registered in research/sweeps/regime_conditioning.yaml. Mechanism: the stop is a fixed ATR multiple measured at entry, so out of a compressed base the move that follows is large against that distance, while when volatility is already elevated the same distance sits inside ordinary noise and the trade exits on movement that carries no directional information. Falsified if compression and expansion expectancy differ by less than the family-corrected MDE at 100 round trips per bucket.
Overrides: none (this is the comparison floor)

## Sample

Registered but never run. No sample, and therefore no result to report.

## Findings log

No findings recorded yet.
