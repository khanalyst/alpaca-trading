# Historical research

`phase1_v2_backtest.py` replays the deterministic momentum setup contracts and
the repository's `RiskEngine` without placing orders or calling an LLM.

It expects a directory containing:

```text
manifest.json
swap/<SYMBOL>.csv
spot/<SYMBOL>.csv
funding/<SYMBOL>.csv
```

The candle CSVs must contain `timestamp_ms,open,high,low,close,volume`; funding
CSVs must contain `timestamp_ms,funding_rate`. Symbols in filenames replace
`/` and `:` with `_`.

From the repository root:

```bash
./.venv/bin/python research/phase1_v2_backtest.py \
  --data /absolute/path/to/historical/data
```

Generated trade and equity CSVs go under
`runtime/research/phase1-v2-backtest/` by default and remain ignored by Git.
Only deliberately reviewed compact reports belong under `research/results/`.

The replay is deliberately a proxy, not a historical claim about the complete
agent. It does not replay LLM selection or LLM-driven early closes, because a
current model can contain knowledge of the historical evaluation period.

The committed `phase1-v2-backtest-2025-2026` result tested commit `6528626`.
It found no reliable deterministic edge: the combined fixed-RR setup was
approximately flat before costs and had negative expectancy after fees,
funding and declared execution assumptions.
