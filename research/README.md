# Alpaca intraday research

This directory contains offline, deterministic research utilities for US
equities, ETFs, and listed options.  Data adapters normalize their payloads
through `research.market_data` before any analysis.  Every normalized event
records its provider, feed, schema, session date/timezone, observation time,
and the as-of timestamp used by the analysis.  Naive timestamps, future
as-of values, invalid quotes, and malformed OHLC values fail closed.

## Normalized data

`UnderlyingBar` is a one-minute OHLCV record. `QuoteSnapshot` is a timestamped
bid/ask record. `OptionContract` and `OptionSnapshot` carry the contract
identity and multiplier separately from the quote.  Use
`normalize_underlying_bar`, `normalize_quote`, and
`normalize_option_snapshot` at provider boundaries; replay code does not
accept raw provider dictionaries.

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
   and target.
5. Apply spread, adverse slippage, and per-side fees to both executions.
6. Force-flat at the configured pre-close boundary; no position crosses a
   session boundary.

Equity and option vehicles are independent result books. Call
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

## Evidence and provenance

Research artifacts should retain the normalized input digest, provider/feed
identity, configuration, and code version alongside results.  Any feature or
label must be computed from events at or before its as-of timestamp.  A
completed-bar fixture and a no-look-ahead test are required for every new
replay path.  Walk-forward, paired-baseline, placebo, and acceptance-floor
checks remain useful gates, but are applied per vehicle and per session rather
than to a pooled equity/options series.
