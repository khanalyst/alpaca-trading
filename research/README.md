# Historical research

Two independent research paths live here. Both replay the deterministic
strategy contracts and the repository's `RiskEngine` without placing orders or
calling an LLM.

| Script | Purpose |
| --- | --- |
| `download_okx_history.py` | Fetch OKX candles, funding and open-interest history from public endpoints |
| `edge_lab.py` | Vectorized, independently written feature/signal/simulation engine |
| `validate_features.py` | Prove `edge_lab` reproduces the **live** `agent.market` / `agent.strategy` output |
| `edge_report.py` | The full edge investigation: nulls, conditionals, walk-forward sweep, selector headroom, power |
| `portfolio_sim.py` | Whole-account simulation through the real `RiskEngine` and circuit breakers |
| `phase1_v2_backtest.py` | The original event-study + portfolio proxy backtest |
| `signal_lab.py` | Point-in-time panel builder and a library of candidate predictors |
| `find_edge.py` | Pass 1: linear quantile spreads across every feature and horizon, FDR-controlled |
| `deep_edge.py` | Passes 2-4: extreme tails, regime/session conditioning, implementable top-N |
| `validate_candidate.py` | Adversarial tests on a surviving candidate, including a placebo control |
| `unbiased_recheck.py` | Timestamp-paired re-test that removes pooled-threshold time-selection bias |
| `make_legacy_dataset.py` | Adapt an `edge_lab` dataset into the layout `phase1_v2_backtest.py` expects |
| `fetch_flow_data.py` | Pull OKX taker-volume and long/short-ratio history (~30 day retention) |
| `analyse_flow.py` | Screen that data for predictive content, starting with a sign-convention check |
| `record_flow.py` | **Long-running recorder** for order-book depth and the short-retention series |

## Getting data

```bash
python research/download_okx_history.py --out runtime/research/data \
  --days 730 --min-volume-usd 30000000 --max-symbols 26
```

This writes:

```text
manifest.json
swap/<SYMBOL>.csv     timestamp_ms,open,high,low,close,volume,quote_volume
spot/<SYMBOL>.csv     (omit with --no-spot; only perp/index basis needs it)
funding/<SYMBOL>.csv  timestamp_ms,funding_rate
oi/<SYMBOL>.csv       timestamp_ms,oi_usd
```

Symbols in filenames replace `/` and `:` with `_`.

**Data-availability limits you must account for**, discovered while building
this and easy to get silently wrong:

- **Funding history is ~97 days.** OKX's public `funding-rate-history` does not
  reach back two years. A dataset missing older funding rows makes
  `phase1_v2_backtest.funding_return_pct` silently return `0.0`, so a run can
  report "funding included" while charging nothing for most of the period.
  `edge_lab` handles this explicitly: outside the measured window it charges
  each symbol's own median observed rate (`Costs.funding_model`). Measured
  rates are ~0.002%/8h on majors, so funding is second-order either way.
- **Open-interest history is ~60 days**, so the `funding_squeeze` contract
  cannot be evaluated over a long window at all.
- **Symbol discovery uses the current instrument list**, so instruments
  delisted before the download date are absent. That is a survivorship bias
  and must be stated in any result derived from the dataset.

## Validating before believing

A backtest is only evidence if it computes what the running agent computes.
`validate_features.py` drives the real `agent.market.symbol_snapshot` through
a fake exchange that replays the CSVs with a frozen clock, and compares every
field against the research engine.

```bash
python research/validate_features.py --data runtime/research/data --samples 30
```

It exits non-zero on any mismatch. The committed audit ran it clean:
0 disagreements on the evidence contract over 240 sampled bars, and
0 snapshot mismatches over 290 sampled bars across 10 symbols.

## Running the investigation

```bash
python research/edge_report.py --data runtime/research/data --stage all
```

Stages: `forward` (raw directional information, no exit overlay), `baseline`
(shipped config vs random-timing / random-direction / inverted-signal nulls),
`latency`, `conditional`, `sweep` (purged walk-forward), `breakeven`,
`oracle` (selector headroom), `power` (how long a demo must run to prove
anything), `portfolio`.

## Recording data that OKX deletes

The candle search failed structurally: 15m OHLCV cannot contain order-book
state or aggressor direction. Those series are either never served
historically (order book) or deleted within 30-97 days. Start the recorder and
the constraint stops getting worse:

```bash
nohup python research/record_flow.py --out runtime/research/recorded &
```

Roughly 20 MB/month, day-partitioned, deduplicated, restart-safe.

Generated CSV/JSON goes under `runtime/research/` and stays ignored by Git.
Only deliberately reviewed compact reports belong under `research/results/`.

## Committed results

### `results/edge-audit-2024-2026/` — the current, most thorough answer

28 instruments, 24 months, point-in-time top-10 universe, 115,929 signals.

**No edge found.** The entry contract is negative before costs
(-0.046% per trade, PF 0.970) and significantly negative after them
(-0.096 R, 95% CI [-0.135, -0.056]). Directional hit rate is 45.6-47.3% —
below a coin flip — at every horizon from 15 minutes to 24 hours. Random
entry timing and inverting the signal both beat following it at zero cost.
None of 79 walk-forward parameter variants was positive out-of-sample. The
whole-account simulation self-kills on drawdown 50 days in; with the breaker
disabled it returns -58% frictionless and -95% at ordinary costs.

Read `results/edge-audit-2024-2026/REPORT.md` first.

### `results/edge-search-2024-2026/` — the search for a replacement signal

~2,250 hypotheses across eight signal families at 15m-48h horizons.
**No edge found.** One candidate survived FDR control and out-of-sample
confirmation, then failed its own placebo: a randomly shuffled signal scored
+0.455% at 48h with t=2.37, about 41% of the "real" result, because decile
thresholds pooled across time were selecting bullish moments for the long leg
and bearish ones for the short leg. Re-tested with per-timestamp ranking, the
placebo beats the real signal in most cells and reaches t=2.60 on pure noise.

That last number is the most useful output here: on this data, at these
sample sizes and with overlapping windows, **t>2 arises routinely from
nothing**. Any future candidate must clear that bar.

What did survive is a parameter result: a 48h maximum hold beat 24h in 8/8
matched walk-forward cells, and a 3R target beat 2R in 4/4 at that hold.
Both are applied in `config.yaml`.

### `results/edge-discovery-method/` — how to recognise an edge, and where to look

The methodology write-up. Six tests any candidate must pass, the measured
noise floor (**t>2 arises routinely from nothing on this data**), and a ranked
list of where an edge could plausibly live: execution cost first, then data
that OHLCV cannot contain, then breadth, then the LLM.

Also records two findings from screening OKX's flow data: taker-volume is
**rejected** (correlation with the same hour's return is +0.006, so it does
not measure aggressor direction), while retail long/short ratio is a live
hypothesis (+1.11% at 48h, t=2.72, per-instrument demeaned) that needs
forward data before it means anything.

### `results/phase1-v2-backtest-2025-2026/` — the earlier result

Six instruments, Jan 2025 - Jun 2026, tested commit `6528626`. Same verdict:
approximately flat before costs, negative after. The 2024-2026 audit re-ran
this script unmodified on fresh data over a different window and reproduced it
to within 0.012 R, which is why the conclusion is treated as robust rather
than as an artifact of one implementation or one period.

## What these results do and do not establish

They measure the deterministic admissibility rules and the risk engine, not
the complete agentic strategy: the LLM can still accept, reject or close
trades. That contribution cannot be measured by replaying a current model on
historical data, because the model may carry training knowledge of the
period.

What the audit *can* do is bound it. Section 5 of the audit report shows that
a selector keeping half the signals needs ~11% of perfect foresight merely to
break even, and that across 64 threshold rules on every field the model
actually receives, the correlation between in-sample and out-of-sample
expectancy is **-0.155**. The observable features carry no persistent
information for the LLM to exploit.

This is retrospective research, not a prediction of future returns.
