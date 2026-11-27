# Edge diagnostics without trading authorization

This page documents the non-authorizing diagnostic surfaces. They are useful
for coverage, arithmetic, replay parity, and operational diagnosis. They do
not open a broker account, create an authorizing proof, promote a candidate, or
change runtime risk configuration.

Diagnostic studies and candidate results cannot authorize entries. Only fresh,
source-gated, parity-matched evidence and the explicit runtime controls can do
that. A pause, trial identity mismatch, stale source, or missing operational
acceptance is an independent blocker; it is not resolved by statistical counts
or a positive point estimate.

## Frozen inventory survey

`research.diagnostic_suite` resolves existing identities and replays them in
isolated accounts. It does not generate or tune hypotheses. The current
inventory contract is exactly 43 unique equity arms: the diagnostic-shadow
rule cohort, the registered mechanism cohort, and the equity IBR registry. The
report is explicitly non-authorizing (`eligible: []`, `proofs: []`,
`authorizing: false`).

The fixed rule cohort evaluates the same baseline and one registered change
for each family on every eligible replay or shadow update. These definitions
are versioned in the catalog. The daily research schedule does not generate
new versions of them.

| Rule family | Parameter | Baseline | Comparison |
| --- | --- | ---: | ---: |
| Opening-range breakout | Breakout threshold (bp) | 5 | 8 |
| Opening-range fade | Threshold (bp) | 8 | 12 |
| Momentum continuation | Threshold (bp) | 18 | 24 |
| Mean reversion | Z-score | 1.5 | 1.75 |
| Trend pullback | Threshold (bp) | 3 | 5 |
| Volatility breakout | Compression (bp) | 8 | 12 |
| Volume breakout | Volume multiplier | 1.5 | 1.75 |
| VWAP reversion | Threshold (bp) | 25 | 35 |
| VWAP trend | Threshold (bp) | 8 | 12 |
| Range expansion | Volume multiplier | 2 | 2.5 |
| Opening drive | Threshold (bp) | 30 | 40 |
| Cross-sectional residual | Threshold (bp) | 12 | 16 |

Compose also enables seven initial-balance breakout (IBR) arms by default.
They compare the baseline with a 30- or 45-minute range, a 1.5R or 3R profit
target, or a 0 or 10 bp breakout buffer. This gives 31 shadow accounts. The
43-arm replay adds four research-only comparisons for each of opening
continuation, trend pullback and fair-value reversion. The factory's separate
discovery and tuning cycle is described in the research guide.
[Rule catalog](../research/diagnostic_shadow.py) · [IBR registry](../research/variants.yaml) ·
[Research-only comparisons](../research/mechanism_cohort.py) ·
[Factory cadence](../research/README.md#when-the-model-proposes-variants)

Run it against a frozen normalized JSONL corpus, always using a new output path:

```bash
.venv/bin/python -m research.diagnostic_suite \
  --data /absolute/path/to/frozen/market.jsonl \
  --agent-config config.yaml \
  --out /absolute/path/to/new/inventory.json \
  --diagnostic-only --workers 2
```

The command also accepts:

* `--full-fit-diagnostics` for the deliberately budgeted all-prefix fit scan;
  omission is marked unavailable, not negative evidence.
* `--ibr-max-events N` for a hard bounded forward-IBR input budget. Input over
  the bound is unavailable; it is never silently truncated.

The CLI returns exit code 2 for a completed diagnostic report. The report and
its manifest bind source and code identities; mutation during the run fails
closed. `execution_blocked`, `missing_data`, `no_signal`, and `underpowered`
are availability classifications, not negative expectancy.

The preflight section of the report is a fixed-configuration geometry check.
It does not measure realized costs, widen authored stops, or change a plan.

## IBR runtime diagnostic

The seven registered equity IBR arms use the shared runtime signal/setup/risk
adapter and isolated diagnostic accounts. Runtime filter parity is represented
by `IBR_UNMAPPED_FILTERS = ()`; the separate `legacy_comparison` is a fixed-
share research comparison and is not runtime evidence. Runtime output keeps
`outcome_counts` and legacy comparison counts separate.

The runtime lane requires forward-observed bars and contemporaneous quotes with
matching provider/feed and valid calendar metadata. Historical, mixed, missing-
quote, or invalid-calendar inputs are unavailable for this lane. Incomplete or
unpriced opportunities remain explicit missing-data rows; only closed,
executable modeled positions contribute realized P&L. Synthetic fixtures verify
arithmetic, not economic edge.

The event budget defaults to 100,000 source events and is frozen into the
manifest. Raise it only for a deliberately sized, immutable input:

```bash
.venv/bin/python -m research.diagnostic_suite \
  --data /absolute/path/to/frozen/market.jsonl \
  --agent-config config.yaml \
  --out /absolute/path/to/new/inventory.json \
  --diagnostic-only --ibr-max-events 250000
```

## Quote-cost calibration

Quote calibration measures a supplied quote corpus; it does not claim broker
fills or market impact and does not activate a cost schedule. The canonical
calibration-only command is:

```bash
.venv/bin/python -m research.cost_rerun \
  --calibration-only --corpus /absolute/path/to/frozen/quotes.jsonl \
  --config config.yaml --min-quotes-per-cell 500 \
  --out /absolute/path/to/calibration.json
```

Use `--schedule-out` to write the fitted schedule and
`--publish-latest PATH` to archive an immutable content-addressed artifact and
refresh a non-activating view. Canonical quote validation, source/feed checks,
and held-out coverage remain mandatory. Missing quotes or insufficient cells
are unavailable, not zero-cost observations.

For an equity stop, the maximum tolerable stress in basis points equals the
stop distance in basis points times the configured cost-to-risk fraction.
Headroom subtracts the trial's stress scenario from that ceiling. A negative
value identifies how much the scenario exceeds the stop's allowance. Estimate
executable costs from quotes, depth, fees and separate validation sessions.

## Broker-free live shadow

`deploy/shadow.py` and `research.live_shadow` run the same signal, setup, risk,
exit, and cost code in virtual books. No shadow path submits orders or needs
broker secrets. Each diagnostic arm has an independent modeled account; do not
pool balances, positions, or returns.

The direct CLI defaults to the 24-rule diagnostic cohort. Set
`ALPACA_SHADOW_INCLUDE_IBR=1` or pass `--diagnostic-include-ibr` to add the
seven registered IBR arms; the environment must be exactly `0` or `1`.
Relevant exact flags include `--corpus`, `--edge-db`, `--shadow-db`, `--config`,
`--diagnostic`, `--once`, `--interval`, `--max-candidates`, `--max-events`,
`--diagnostic-session-max-events`, `--replay-session-max-events`,
`--max-decisions`, `--max-workers`, and `--retention-days`.

```bash
ALPACA_SHADOW_INCLUDE_IBR=1 .venv/bin/python deploy/shadow.py \
  --corpus runtime/research/recorded/market.csv \
  --edge-db runtime/research/diagnostic-edge-lab.sqlite3 \
  --shadow-db runtime/research/diagnostic-shadow.sqlite3 \
  --config config.yaml \
  --health-file runtime/research/diagnostic-shadow-health.json \
  --acceptance-root runtime/research/diagnostic-shadow-acceptance \
  --acceptance-recorder-root runtime/research/recorded \
  --diagnostic --diagnostic-include-ibr --once
```

The paths above are isolated for a one-off diagnostic. Stop any existing
shadow writer before retrying with a database, health file, or acceptance root
it already owns; otherwise choose new isolated paths rather than running two
writers against the same shadow store.

The loop writes a health heartbeat and session-acceptance records. Acceptance
requires stable deployment/code/cohort/activation identities, exact calendar
coverage, current source observations, all active arms, and genuine event/cursor
progress. An unchanged heartbeat or cursor is not a completed session. Source
lag and quote age remain bounded; re-fetching an old bar cannot make its
completion timestamp current.

### Latency rules and measurements

| Consumer | Clock and default policy |
| --- | --- |
| Paper trader | The default cycle starts on a 60-second wall-clock grid. The collector filters session bars to those completed by its cutoff and requests quotes in the configured freshness window. The collector checks quote age from the market timestamp. The rule checks the latest bar's age from its close. Both use the broker clock sampled for the cycle, with a default limit of 30 seconds. |
| Recorder | Compose targets 30 seconds between polls. A successful forward cycle with a completed fetch window can start the next poll immediately after an overrun. Full-tick capture also pays for provider pagination, durable file writes and the exact-key index commit. |
| Shadow and session acceptance | Compose polls shadow every 30 seconds. Acceptance checks quotes, recorder/shadow freshness, source lag, progress and full-session coverage. For completed minute bars it checks when the *next* completed bar was due, with at most 30 seconds of publication tolerance. At a 30-second shadow interval the allowed gap between acceptance samples is 35 seconds. |

To diagnose a slow session, compare provider publication time, request time and
age at actual receipt. For the recorder, measure successful in-session fetch
and commit phases separately from idle polls and failed retries. For shadow,
compare the time spent processing a batch with the age of the source it read.
For orders that reach submission, use the decision, send, response and broker
timestamps. The entry snapshot freshness check alone cannot establish age
at send or time to fill. [Trader collection](../agent/market_entry_risk.py) ·
[Decision loop](../agent/engine_cycle.py) · [Recorder](../deploy/recorder.py) ·
[Acceptance](../deploy/session_acceptance.py)

## Recorder projection and pre-acceptance diagnostics

`deploy/trader_projection.py` creates a bounded, diagnostic-only replay view.
It retains non-quote events (including revisions and gaps) and the quote events
needed at replay boundaries. Cost schedules are measured before quote thinning;
the manifest is written only after source identity rechecks. It has no promotion
or broker-facing path.

```bash
.venv/bin/python deploy/trader_projection.py \
  --corpus runtime/research/recorded \
  --config config.yaml \
  --output /absolute/path/to/new/projection
```

Optional flags are repeatable `--session`, `--csv-mode {recorder,external}`,
`--max-retained-rows`, `--max-output-bytes`, and `--disk-floor-bytes`.

An operator may run the bounded pre-acceptance mode of
`deploy/research-cycle.sh` with `ALPACA_RESEARCH_PREACCEPTANCE_DIAGNOSTIC_ONCE=1`,
an explicit sealed `ALPACA_RESEARCH_SNAPSHOT_ROOT`,
`ALPACA_RESEARCH_ACCEPTANCE_ROOT`, a positive `ALPACA_RESEARCH_SESSION_WINDOW`,
a positive `ALPACA_RESEARCH_MAX_SOURCE_BYTES`, and a new
`ALPACA_RESEARCH_DIAGNOSTIC_OUTPUT` outside the snapshot. It skips authorizing
research and preserves the scheduler status `waiting_for_forward_sessions`;
it does not manufacture
accepted sessions.

## Interpretation boundary

The diagnostic outputs are evidence about data availability, implementation
parity, and modeled arithmetic. They do not establish broker shortability,
buying power, pending orders, actual fills, or execution capacity. They cannot
authorize an entry, lower a gate floor, consume online-FDR wealth, or override
an operator/runtime pause. For authorizing workflow and current versioned
contracts, read [`research/protocol.md`](../research/protocol.md).
