# Completion of the trading-edge handover

Continuation of [the audit handover](trading-edge-handover-2026-09-08.md).
This record distinguishes implementation, observed operation and evidence that
still requires future market sessions. No positive edge is claimed.

## Implemented in this continuation

- Sealed snapshots copy a bounded session window under the recorder/backfill
  lock, include provenance/calendar/index bytes, fsync and atomically publish a
  content manifest. Verification rejects missing, changed or unexpected files.
  Recorder writes into a sealed snapshot are refused. Snapshot creation has
  date, byte and free-space bounds; growing recorder paths do not receive
  automatic cache identities.
- The recorder retains observed OHLCV corrections in a separate receipt-time
  SQLite store. Point-in-time queries choose the revision actually received by
  the cutoff. The original CSV remains its first-observation replay contract.
  Bar request boundaries include the complete overlap minute. This records
  polling observations, not every exchange event or websocket correction.
- Order journals retain local decision/request/response timing and explicit
  broker submission/fill times. Reconciliation preserves initial timing.
  Calibration reports timing samples and adverse cost by mode, vehicle,
  session, symbol and size. Missing broker fill times remain unknown.
- Candidate and original proof-run identities are carried into new fill rows.
  The read-only workbench filters source, symbol, feed, candidate, variant,
  proof epoch and dates. It shows complete 1/5/15-minute and daily candles,
  prior complete-session levels, completed parent economics and event/exit
  counts. Large inputs are bounded and missing data stays explicit.
- A registered cohort runner verifies a snapshot, fixes all twelve arms and
  costs before evaluation, retains full results and writes a compact dashboard
  summary. It records an unseen confirmation start and never creates proofs,
  changes trading eligibility or calls a strategy LLM.
- Separate diagnostics measure prior recorded-session levels/gaps, same-clock
  volume against earlier sessions, configured-universe participation and
  synchronized SPY beta. They do not change an entry predicate or allocation.
- Independent execution fault tests survived the worker interruption: partial
  close/retry costs, restart idempotency, network-failure pause, lock release,
  and real-child termination before recovery all pass.
- CI now partitions the complete discovered suite into four disjoint jobs.
  The previous serial run reached the 90-minute limit; its container build was
  skipped, so that run is not a pass.
- Paired-feed diagnostics request IEX and SIP separately on identical completed
  broker-calendar sessions. They retain raw responses, receipt times, missing
  minutes and paired price/volume differences. An unavailable feed remains an
  unavailable comparison; no fallback is permitted.
- Compose limits research input to 1 GiB by default and checks estimated
  temporary expansion plus a 2 GiB reserve before preprocessing. The expansion
  multiplier is a capacity estimate, not a hard disk quota. Larger jobs need
  explicit capacity and `ALPACA_RESEARCH_MAX_SOURCE_BYTES` configuration.
- Recorder catch-up commits at most two fetch windows per Compose cycle,
  releasing the shared corpus lock between batches. This prevents a multi-hour
  catch-up from starving a snapshot reader.

## Verification in progress

- Commit `dd71ae339fd2b0fff613a92656b5ba1c05351afd` was pushed to main and
  built successfully on the deployment host. GitHub's container build also
  passed in [run 34191767877](https://github.com/khanalyst/alpaca-trading/actions/runs/34191767877).
  Its four test shards were still running at this checkpoint.
- New mechanism, snapshot, workbench, context, timing and fault contracts:
  26 tests passed. The overlapping execution/accounting review package passed
  221 tests. These are software results, not evidence of trading profitability.
- Browser verification on synthetic data showed 30 complete one-minute bars,
  six five-minute bars, two fifteen-minute bars, no fabricated incomplete daily
  candle, and prior complete-session levels. Candidate/variant/proof filters
  retained one completed parent with gross $16, fees $1, net $15 from two close
  fills; POST returned 405.
- At `2026-09-08T05:49:29Z`, an authenticated read found zero paper positions
  and open orders, with operator pause true. The old stalled diagnostic process
  was stopped with its container and artifacts preserved. Recording and shadow
  services were restarted; historical catch-up is distinct from forward evidence.

## Reproduction

```sh
python deploy/research_snapshot.py create \
  --recorded-root runtime/research/recorded \
  --snapshot-root runtime/research/snapshots/example \
  --session-window 30 --end-session 2026-08-11 \
  --max-bytes 1000000000 --min-free-bytes 5000000000
python deploy/cohort_experiment.py \
  --snapshot runtime/research/snapshots/example \
  --output runtime/research/experiments/example
python deploy/test_suite.py --list
```

For a research cycle, set `ALPACA_RESEARCH_SNAPSHOT_ROOT` to a verified sealed
snapshot; its identity then supplies the preprocessing cache key. Creation is
opt-in with `ALPACA_RESEARCH_SNAPSHOT_CREATE=1` and a positive
`ALPACA_RESEARCH_SESSION_WINDOW`. Existing destinations are never overwritten.

## Operational and economic limits

At the authenticated predeployment check, the paper account had zero positions
and zero open orders, with operator pause set. This is a dated observation,
not a continuing assertion about the account.

Deployment, the final CI result and the real-input cohort result will be
recorded below after verification. Collecting unseen sessions, measuring live
execution effects and establishing a positive net edge require actual evidence.
Alpaca's [paper-trading specification](https://docs.alpaca.markets/us/docs/paper-trading)
excludes market impact, queue position and latency slippage from its simulation.
The [stock stream documentation](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data)
also describes late bar corrections and quote sizes in round lots. Unspecified
lot units must not be treated as measured share capacity.
