# Trader research after the October strategy review

The original 43-arm inventory and `intraday-mechanisms.v1` stay frozen. The
additive `intraday-mechanisms.v2` cohort contains twelve diagnostic V6 arms,
one per family. V6 cannot execute through the runtime vehicle gate. Its pure
replay rows remain explicitly non-authorizing. No winner is selected and no
paper-trial transition is performed.

| Family | New mechanism |
| --- | --- |
| Opening-range breakout | Two completed post-range closes beyond the range |
| Opening-range fade | Failed breakout and closeback, with a session-VWAP exit; **invalid, see below** |
| Momentum continuation | Volume confirmation and bounded entry/holding windows |
| Mean reversion | Sequenced reclaim and a rolling-mean exit; **invalid, see below** |
| Trend pullback | Reclaim within completed five-minute trend context |
| Volatility breakout | Compression relative to prior-bar ATR |
| Volume breakout | Same-clock volume against five prior exact-feed sessions |
| VWAP reversion | Reclaim, range context, and a session-VWAP exit; **invalid, see below** |
| VWAP trend | Two equal completed windows for a stable VWAP comparison |
| Range expansion | Directional candle body and close-location requirements |
| Opening drive | Three completed post-window closes holding displacement |
| Cross-sectional residual | Explicitly described as outright relative strength against SPY |

Missing prior-session volume samples fail closed. The relative-strength arm
does not establish a hedge or beta neutrality. Every arm also carries a 1.5R
completed-close trailing stop (`trailing_stop_r` 1.5), which the table above
does not show. The authored one-ATR stop, 30 bps stop floor, configured 9 bps
stressed admission scenario (`config.yaml:52`) and 30% cost-to-risk ceiling
(`config.yaml:53`) are not relaxed to manufacture trades. Under that
configuration the admission floor is 9 / 0.30 = 30 bps, the same as the stop
floor. A one-minute ATR is a few basis points on these ETFs, so the 30 bps
floor sets essentially every stop and the one-ATR multiple has no effect
(review finding A-02).

## Errata (October 2026 review)

**Three v2 arms are invalid (finding A-01).** `exit_before_minutes` counts
minutes before the 16:00 close, not after the open. The opening-range-fade,
mean-reversion and VWAP-reversion arms set 360, which is a 10:00 New York
thesis deadline against entry windows of 09:50 to 12:30, 09:45 to 12:30 and
10:00 to 14:30. Research refuses every entry at or after the deadline, so the
VWAP-reversion arm can never trade and the other two can only enter in the
minutes before 10:00. Their v2 results, and those of the same three arms in
`intraday-mechanisms.cost-compatible.v1` (which inherits the deadline), do not
test the stated thesis and must be read as invalid. The arms stay loadable
with their identities; each manifest now lists them under `errata`, outside
the hashed body, so no pinned hash changes.
`intraday-mechanisms.deadline-compatible.v1` sets 30 (15:30) and is the
valid version of these three arms.

**The stress scenario in the cohort labels (finding A-12).** The
`risk_policy` labels of the cost-compatible and deadline-compatible manifests
name a 25 bps stressed cost scenario. That value belongs to the paper profile
activated on 2026-09-15 (`docs/paper-activation-2026-09-15.json:119`). Every
shipped configuration (`config.yaml:52`, `deploy/paper-orb.config.json:79`,
`deploy/paper-orf.config.json:79`) uses 9 bps, so the configured admission
floor is 30 bps and the 100 bps "cost-compatible" floor (sized for
25 / 0.30 = 83 bps) is not required for admission. The labels are frozen
inside their manifest hashes; the correction is recorded in each manifest's
`errata`.

## Corrected cohort `intraday-mechanisms.v3`

`research/mechanism_cohort_v3.py` registers one diagnostic arm per family.
Each arm is its v2 parent on the V7 grammar with declared corrections, and
every other field (including the 1.5R trailing stop) is inherited:

| Finding | Correction | Arms |
| --- | --- | --- |
| A-01 | 15:30 thesis deadline; the builder rejects any window plus hold that passes it | fade, mean reversion, VWAP reversion |
| A-02 | `stop_mode: horizon_atr`: `stop_atr` x the symbol's own prior-bar ATR x sqrt(`max_hold_bars`), above the 30 bps floor | all but the fade |
| A-03 | Session-VWAP and rolling-mean targets (inherited from v2) | fade, mean reversion, VWAP reversion |
| A-04 | `signal_event: first_cross`: signal only on the bar at which the predicate turns true | opening-range breakout and fade, momentum, volatility and volume breakout, opening drive |
| A-05 | `stop_mode: structure`: one ATR beyond the failed-breakout extreme | fade |
| A-09 | `residual_mode: beta`: residual net of a causal OLS beta from the five prior sessions | cross-sectional residual |
| A-10 | Same-clock volume confirmation; volume breakout drops its current-feed confirmation | momentum, trend pullback, volume breakout |
| A-14 | A same-clock participation filter instead of none (a trend filter would repeat the predicate) | trend pullback |
| A-15 | Dedicated `range_multiplier` | range expansion |

These corrections are optional V7 fields with no default, so every existing
spec keeps its normalized form, hash and variant id;
`tests/research/test_rule_golden_equivalence.py` replays all 75 registered
rule arms and checks they evaluate exactly as before. Limits: V7 cannot
trade; `research.strategy_factory.run_factory` dispatches only the v1 and v2
cohort ids; and the factory replay passes the cross-sectional family only the
current session's SPY bars, so the beta-adjusted arm fails closed there until
the replay context supplies prior sessions.

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
