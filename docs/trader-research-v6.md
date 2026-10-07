# Trader research after the October strategy review

The original 43-arm inventory and `intraday-mechanisms.v1` stay frozen. The
additive `intraday-mechanisms.v2` cohort contains twelve diagnostic V6 arms,
one per family. V6 cannot execute through the runtime vehicle gate. Its pure
replay rows remain explicitly non-authorizing. No winner is selected and no
paper-trial transition is performed.

| Family | New mechanism |
| --- | --- |
| Opening-range breakout | Two completed post-range closes beyond the range |
| Opening-range fade | Failed breakout and closeback, with a session-VWAP exit |
| Momentum continuation | Volume confirmation and bounded entry/holding windows |
| Mean reversion | Sequenced reclaim and a rolling-mean exit |
| Trend pullback | Reclaim within completed five-minute trend context |
| Volatility breakout | Compression relative to prior-bar ATR |
| Volume breakout | Same-clock volume against five prior exact-feed sessions |
| VWAP reversion | Reclaim, range context, and a session-VWAP exit |
| VWAP trend | Two equal completed windows for a stable VWAP comparison |
| Range expansion | Directional candle body and close-location requirements |
| Opening drive | Three completed post-window closes holding displacement |
| Cross-sectional residual | Explicitly described as outright relative strength against SPY |

Missing prior-session volume samples fail closed. The relative-strength arm
does not establish a hedge or beta neutrality. The authored one-ATR stop,
30 bps stop floor, preserved 25 bps stressed admission scenario, and 30%
cost-to-risk ceiling are not relaxed to manufacture trades.

## Bounded evidence preparation

`deploy/trader_projection.py` streams selected recorder CSV sessions with their
exact calendar and source provenance. It preserves equity bar observations,
revisions, gaps, timestamps and first-observation boundaries. Original CSVs
remain untouched. Quote spread/depth schedules use the unthinned quote stream
with a chronological 70/30 session split. Calibration retains the 500-quote
and five-session cell floors.

The replay projection retains first/last quotes per market minute and the
latest visible quote at recorded bar availability boundaries. It supports
retrospective bar-boundary diagnostics; it is not a full tick path or broker
equivalence proof. Default guards limit retained rows to one million, output
to 1 GB, and require 5 GB free disk space. Failures leave an explicit partial
receipt without a completed manifest. Choose a fresh output directory for
every run and select sealed sessions rather than the currently changing day.

```sh
python deploy/trader_projection.py \
  --corpus /app/runtime/research/recorded-forward-2026-09-16 \
  --config /app/config.yaml --output /work/projection \
  --session market-2026-09-16.csv --session market-2026-09-17.csv

python -m research.trader_research \
  --data /work/projection/compact.jsonl \
  --projection-manifest /work/projection/manifest.json \
  --config /app/config.yaml --output /work/research
```

Supply every intended sealed session explicitly. The second command verifies
the projection, configuration, compact-data hash and raw-cost artifact hashes
before reusing the unthinned measurement. It registers all 55 arms before
evaluation: 43 unchanged controls plus 12 new mechanisms. The seven IBR arms
retain their separate strict forward-observation runtime measurements and
historical comparison. Historical or incomplete source evidence remains
unavailable for the stricter lane.

Per-arm checkpoint files remain partial until the final source/code checks
pass. The completed result retains every replay row, censored exit, refusal,
legacy comparison and unavailable measurement. Signal diagnostics use
5/15/30/60-minute horizons and clock-matched controls. Trader diagnostics
decompose economics by exit, symbol, side, session time and session; gross
P&L is at modeled fill prices, followed by explicit fees. Executed or explicit
signal opportunities count toward overlap; non-signals remain coverage data.
Correlation requires at least thirty shared sessions with explicit complete
and eligible coverage. Missing sessions are not imputed as zero returns.

## Operational changes and limits

Recorder failure retries retain the fixed cadence anchor. Bar and quote
observations retain their respective API response receipt boundaries. These
changes do not alter event timestamps, manufacture freshness or eliminate
historical gaps. Actual live-load freshness still requires measurement.

Shadow ingestion updates the corpus cursor once per transaction while keeping
conflict rollback and offsets durable. Whole-poll phase timings expose work
outside diagnostic evaluation. A process-local decoded-event cache binds
source, calendar, candidate, configuration, code, quarantine and replay/book
state; decision, replay, quarantine and coverage checks continue to run.

Operator pause, the frozen trial, order history and protected runtime policy
are preserved through deployment. Rejected historical sessions remain
rejected. Research qualification still requires 100 trades/30 sessions; the
live-shadow gate requires 150 trades/30 complete sessions plus untouched
validation, confidence/power, multiple-testing and broker-equivalence checks.
The separate 20-session/20-trade paper trial remains diagnostic.
