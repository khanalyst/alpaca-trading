# Alpaca intraday research

This directory contains offline replay and bounded research utilities for
US-listed equities/ETFs and listed OCC options; crypto is outside scope.
Options are single-leg long calls or puts only; multi-leg, naked, and short
structures are not supported. Data adapters
normalize their payloads through `research.market_data` before any analysis.
Every normalized event
records its provider, feed, schema, session date/timezone, observation time,
and the as-of timestamp used by the analysis.  Naive timestamps, future
as-of values, invalid quotes, and malformed OHLC values fail closed.

## Normalized data

`UnderlyingBar` is a one-minute OHLCV record. `QuoteSnapshot` is a timestamped
bid/ask record. `OptionContract` and `OptionSnapshot` carry the contract
identity and multiplier separately from the quote. Use
`normalize_underlying_bar`, `normalize_quote`, and
`normalize_option_snapshot` at provider boundaries; replay code does not
accept raw provider dictionaries.

The deployment recorder appends bars, quotes, and option snapshots to one
mixed market dataset. `deploy/research-cycle.sh` validates that dataset and
routes the available bar/option events to the vehicle-local discovery lanes.
It also invokes `research.strategy_factory`, which evaluates seven distinct
rule families concurrently by default. Each generated variant owns an
isolated simulated account; no capital or P&L is shared between arms.
Paper runtime selection can then use `selection_mode: all_proved`, which keeps
one best proven variant per independent family under one global risk book.

The default session timezone is `America/New_York`. Session dates are derived
after timezone conversion, so the daylight-saving transitions in March and
November do not shift a bar into a neighboring session. A source `as_of`
timestamp may not be later than its observation/ingestion timestamp.

## IBR replay

`research.ibr.replay_ibr` implements the initial-balance-range path:

1. Require a complete, contiguous range of completed one-minute bars from the
   configured US session open.
2. Detect a breakout only after a range bar has closed.
3. Enter at the immediate next bar's open; a missing next bar yields no trade.
4. Apply gap-aware fills and stop-first ties when a candle touches both stop
   and target. A bar that opens beyond the stop or target fills at that open,
   on exit as well as on entry, not at a level the market never traded again.
5. Price a fill landing on a bar boundary from a recorded quote at that
   instant when one exists, and record on the trade whether the quote or the
   bar was used. A level triggered inside a bar has no such instant and keeps
   the level.
6. Apply spread, adverse slippage, and per-side fees to both executions
   through the one shared model in `research.costs`.
7. Force-flat at the configured pre-close boundary; no position crosses a
   session boundary.

Equity and single-leg long-option vehicles are independent result books. Call
`replay_ibr_vehicles` for a mapping of separate results; it intentionally has
no pooled P&L field. Option replay requires timestamped option snapshots and
uses their contract multiplier.

The command-line surface is intentionally small:

```bash
python research.py validate-data bars.jsonl --provider alpaca --feed sip
python research.py backtest-ibr bars.jsonl --vehicle equity
```

Both commands are offline and read JSONL only. They never download data, call
an exchange, place orders, or modify trading state.

## Costs and fill calibration

`research.costs.CostModel` is the single expected-cost model: every lane --
IBR replay, edge discovery, and the strategy factory -- prices its fills
through it, and none carries its own spread/slippage/fee numbers or its own
arithmetic. Its parameters come from one `costs` block
(`spread_bps`, `slippage_bps`, `fee_bps`); the defaults describe a normal
marketable fill in the configured liquid ETF universe.

The runtime's `execution.max_slippage_bps` and `max_spread_bps` are rejection
caps, not expectations. They are read into the same model and bound it: a
model expecting a cost the runtime would refuse to submit fails closed rather
than simulating fills that could never happen. Sourcing an expected slippage
from the cap is as wrong as ignoring the cap.

Quote-driven fills need the quote rows to reach the lane. `research.edge_lab`
and `research.strategy_factory` accept them from a mixed corpus, but
`deploy/research-cycle.sh` currently splits the recorded dataset into
bar-only and option-only files, so a scheduled cycle still prices every
equity fill from the bar and reports `bar` as the source.

```bash
python research.py calibrate runtime/paper/journal.db
```

`research.calibration` reads the runtime journal read-only and compares each
recorded entry fill against the plan price that priced it, recovered from the
plan's notional and submitted quantity. It reports the observed adverse cost
in basis points, the model's bias against it, how many fills landed past the
runtime's own slippage cap, and a verdict of `conservative`, `optimistic`, or
`insufficient_data`. It never adjusts the model; an optimistic model is a
finding, and the command exits non-zero so it cannot be missed.

## Evidence and provenance

Research artifacts should retain the normalized input digest, provider/feed
identity, configuration, and code version alongside results.  Any feature or
label must be computed from events at or before its as-of timestamp.  A
completed-bar fixture and a no-look-ahead test are required for every new
replay path.  Walk-forward, paired-baseline, placebo/falsification, and
acceptance-floor checks are mandatory gates, with fit/held-out structural
floors, family-level false-discovery correction, and a durable verified gate.
They are applied per vehicle and per session rather than to a pooled
equity/options series.

## Edge laboratory

`research.edge_lab` stores pre-registered candidates, immutable
backtest/shadow runs, trades, evidence, paper outcomes, and lifecycle events
in SQLite. Every run carries dataset, configuration, code, and provenance
SHA-256 hashes. The default database is `runtime/research/edge_lab.sqlite3`
(override with `ALPACA_EDGE_DB`).

The lifecycle is forward-only: an initial corpus backtest moves a candidate to
`backtest_passed`; a later corpus must contain sessions strictly after the
persisted boundary, and passing unseen shadow gates moves it through `shadow`
to `validated` and automatic champion selection. Paper outcomes are append-only
evidence and may demote a champion. Candidates are scored separately for
`equity` and `option` vehicles. Gates require chronological held-out data,
fit/held-out trade and session structural floors, matched controls,
cluster-aware randomisation, family-level FDR, and placebo/falsification.
Underpowered data is not failure. Retirement is allowed only after all intended
variants are adequately tested and fail; a valid bounded LLM replacement must
be registered first when that lane is enabled. Drawdown is measured and used
in conservative champion ranking. Normal operation needs no manual promotion;
the `edge promote` CLI is only for explicit, audited controls and remains
subject to lifecycle/evidence rules. Backward rollback is rejected; explicit
demotion is the operator safety action.

## Autonomous strategy factory

`research.strategy_factory` owns safe hypothesis generation. Its proposal
language is the finite grammar in `agent.contracts.rule`: opening-range
breakout/fade, momentum continuation, mean reversion, trend pullback,
volatility breakout, and volume breakout, with bounded confirmations and exit
parameters. It never generates or imports source code.

On a fresh corpus, each worker diagnoses its baseline only from the
chronological fit partition, creates bounded variants based on the observed
failure mode, and evaluates those variants on untouched held-out sessions.
Every variant has a separate simulated cash/equity account. A family is
retired only when all intended variants are adequately powered and fail; if
LLM replacement is enabled, a valid bounded proposal is registered first. A
missing or invalid LLM proposal leaves the family pending replacement, not
retired. Insufficient data is not treated as failure. Backtest winners must
still pass strictly later forward data before runtime can select them.

The checked config enables the bounded strategy-replacement adapter with
OpenAI `gpt-5`. It is optional and reads provider keys only from
`ALPACA_RESEARCH_LLM_SECRETS_FILE`, never from the broker secret file. Missing,
invalid, or rejected model output records a pending replacement; it cannot
retire a family prematurely. Successful proof produces a deterministic,
content-addressed finding. `research.proof.webhook_url` may send that finding
to an HTTPS webhook without changing the durable artifact.

The scheduled cycle reports `completed`, `completed_no_edge`, `no_data`, or
`failed`. `completed_no_edge` means the input was valid but no candidate passed
the gates; `no_data` means the input was unavailable or empty. Neither status
permits bypassing the runtime edge gate.

```bash
python research.py factory run --data market.jsonl --strategies 7 --variants 4 --workers 7
python research.py factory status
```

```bash
python research.py edge init
python research.py edge discover --data market.jsonl --vehicle equity --lane auto
python research.py edge status --vehicle equity
python research.py edge ingest CANDIDATE_ID paper-outcome.json
```
