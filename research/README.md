# Research guide

This directory contains the market-data normalizers, replay engines, bounded
strategy grammar, evidence gates, diagnostic shadow, and paper-only research
stores. The implementation is the authority. This guide describes the current
interfaces; it is not a results report.

## Authority boundary

Research has two deliberately separate lanes:

* An authorizing lane may create a content-addressed candidate proof only from
  source-gated, forward-observed evidence and the current replay/gate epochs.
* A diagnostic lane may inspect historical, sparse, delayed, bar-fallback, or
  otherwise non-authorizing data. Its rows and reports must carry
  `diagnostic_only: true` and `authorizing: false`.

Diagnostic studies, candidate rankings, fit measurements, and paper-trial
verdicts cannot authorize an entry, change sizing, promote a candidate, or
enable live trading. A current proof requires fresh source validation, parity
replay, the versioned gates, and the explicit deployment controls. A pause or a
trial/configuration identity mismatch is an independent operational blocker; it
is not repaired or overridden by a favorable statistic.

The finite rule grammar is implemented in `agent/contracts/rule.py`. Research
may propose bounded values in that grammar, but it cannot generate Python
source, add a signal family, or widen a schema implicitly. Variant ids are
content identities; semantic de-duplication is separate from storage identity.
The supported rule schemas are `rule-strategy.v1` through `v7`. Equity-only
exit extensions and their transition contract are described in
[`docs/rule_strategy_v3_exit_protocol.md`](../docs/rule_strategy_v3_exit_protocol.md).

## Input and provenance

Normalize provider payloads to the records in `research.market_data`: one-minute
underlying bars, executable quotes, and (when explicitly researching options)
option snapshots. Every row must retain its provider, feed, timestamps, and
`source_mode`.

`source_mode: forward_observed` is the only authorizing mode. A row is
actionable no earlier than the maximum of its market timestamp, `as_of`, and
`observed_at`; `as_of` may not be later than `observed_at`. Historical rows use
`source_mode: historical_backfill` and remain diagnostic even when their
calendar metadata is exact. Missing or implicit provenance is not silently
upgraded by a CLI flag.

For equity, authorizing data must use the configured exact `iex` or `sip` feed
and provider. `delayed_sip`, mixed feeds, bar-only boundary prices, stale
quotes, and partial feeds are diagnostic-only. Option authorization requires
`opra` and keeps an independent option result book. A feed, provider, universe,
calendar, cost schedule, or execution-policy change creates a new evidence
identity and must not be merged with prior evidence.

The exact Alpaca session calendar supplies session open/close and early-close
metadata. A missing calendar boundary is a refusal, not a fixed 16:00 fallback.
Sessions are grouped in `America/New_York`; daylight-saving transitions do not
change the grouping contract.

Validate a normalized corpus before replay:

```bash
.venv/bin/python research.py validate-data DATA.jsonl \
  --provider alpaca --feed iex
```

Use `--diagnostic-only` only when the purpose is explicitly non-authorizing:

```bash
.venv/bin/python research.py validate-data DATA.jsonl \
  --provider alpaca --feed delayed_sip --diagnostic-only
```

The diagnostic flag permits historical/mixed source modes for inspection; it
does not permit malformed rows, unsupported feeds, future observations, or a
mutable source that changes during validation.

## Replay and fill semantics

All replay paths share the completed-bar and cost contracts. A signal uses only
the completed prefix available at its causal decision time. Entries do not use
the signal bar's own close as a retrospective fill. A gap through a stop or
target fills at the gap open; an intrabar stop/target tie resolves to the stop.
The entry bar cannot retrospectively close a new position. A hold ending at an
internal data discontinuity is recorded as a discontinuity, not silently joined
to the next observed minute.

Strict lanes require fresh executable quotes at every boundary that needs one.
The historical IBR backtest may opt into the validated research-only bar
fallback; that fallback is labelled diagnostic and never authorizes a proof.
`research.costs.CostModel` owns spread, slippage, fees, stress arithmetic, and
leg provenance. Runtime spread/slippage limits are rejection caps, not expected
costs. Equity bracket prices are rounded to valid broker ticks before sizing;
invalid geometry or a stressed cost/risk violation refuses the signal without
widening its stop or recomputing its target.

Replay outputs preserve raw rows, refusal reasons, quote/bar fill source,
provider/feed/age metadata, realized risk, costs, and canonical exit causes.
The canonical causes are `stop`, `target`, `max_hold`, `thesis_deadline`,
`session_force_flat`, `data_discontinuity`, and `unknown`.

Run the standalone IBR path with the actual CLI flags:

```bash
.venv/bin/python research.py backtest-ibr BARS.jsonl \
  --vehicle equity --provider alpaca --feed iex \
  --quotes QUOTES.jsonl --strict-market-data
```

For an explicit historical diagnostic, add `--diagnostic-only`. The other
available flags are `--symbol`, `--quotes-from-mixed`, `--range-minutes`,
`--stop-pct`, `--target-pct`, `--spread-bps`, `--slippage-bps`, `--fee-bps`,
`--force-flat`, and `--vehicle {equity,option,both}`. `validate-data` and
`backtest-ibr` are offline; neither downloads data nor places orders.

## Cost-model versions

`static_v1` is the compatibility model used by `CostModel` when no alternate
resolver is selected: 4 bps spread, 6 bps adverse slippage, and 0.5 bps per
side notional fee; the option fee is 0.65 currency units per contract per side.
The all-in stress ladder is 9, 15, 25, and 50 bps of entry notional for a
round trip. These values are code contracts, not claims about realized fills.

`per_symbol_v2` is the order-type-aware, per-symbol expected-cost model used by
the cost rerun and explicit resolver callers. It requires its own measured or
reference spread/depth context and records the model version in leg
provenance. A measured-quote schedule is only usable when its feed/provider,
coverage policy, percentiles, quote-count floor, and content hash verify. A
missing or under-covered cell fails closed; it does not fall back to a broad
average.

Fit or publish a diagnostic calibration artifact with:

```bash
.venv/bin/python -m research.cost_rerun \
  --calibration-only --corpus QUOTES.jsonl --config config.yaml \
  --min-quotes-per-cell 500 --out calibration.json
```

`--publish-latest PATH` archives an immutable content-addressed artifact and
refreshes a non-activating latest view. Publication never activates a runtime
cost schedule. The full CLI also accepts `--specs`, `--percentile`,
`--starting-cash`, and `--schedule-out`; inspect `--help` before using them.

## Evidence lifecycle and gates

The authorizing lifecycle is source validation → causal replay → fit/held-out
controls → qualification → parity-matched live shadow → verified proof → an
explicit deployment selection. The current constants are defined in
`research/gates.py`; they include 100 trades/30 sessions/30 clusters for
backtest and qualification, and 150 trades/30 sessions/30 clusters for the
parity-matched shadow tail. These are minimum evidence floors, not tuning
knobs. A corpus that cannot support a required floor is underpowered, not a
negative edge.

The current gate envelope is `verified-research-gate.v3` with gate protocol 2;
protocol 1 remains readable under its original semantics for stored evidence.
Protocol 2 adds the versioned cost-stress, control, qualification, and durable
FDR checks. The current replay generation is `REPLAY_ENGINE_EPOCH = 7` in
`research/edge_ledger_store.py`; older proofs remain audit-readable but cannot
authorize until re-derived under the current epoch.

Gate checks include absolute after-cost economics, matched controls, a seeded
cluster-level falsification test, walk-forward stability, qualification rows,
multiple-test correction, and the durable confirmatory online-FDR allocation.
The active new confirmatory scope is `shadow-confirmation-v6` with its v6
method. The v7 FDR constants are versioned and parseable, but the proof
verifier's active scope remains v6; changing that scope is an explicit code and
proof-verifier change, not a documentation choice.

Use the ledger commands only for their named lifecycle operation:

```bash
.venv/bin/python research.py edge init
.venv/bin/python research.py edge discover --data DATA.jsonl \
  --vehicle equity --lane backtest --agent-config config.yaml
.venv/bin/python research.py edge-ingest-shadow CANDIDATE_ID \
  --db runtime/research/edge_lab.sqlite3 \
  --shadow-db runtime/research/shadow.sqlite3
.venv/bin/python research.py edge-trials --dry-run
.venv/bin/python research.py edge-promotable --format json
```

`edge discover --diagnostic-only` inspects historical/mixed inputs without
opening an authorizing ledger. `edge-ingest-shadow` requires the prior
qualification, current source/config/code/provenance/replay/gate identities,
fresh complete parity-matched rows, and the durable shadow/FDR checks. A
candidate report or a diagnostic candidate list is not a promotion.

The factory has the same boundary and isolated accounts per variant:

```bash
.venv/bin/python research.py factory-run \
  --data DATA.jsonl --agent-config config.yaml --vehicle equity \
  --db runtime/research/factory.sqlite3 --workers 2
```

Use `--diagnostic-only` for non-authorizing sources. `--cohort
intraday-mechanisms.v1` is a frozen 12-arm diagnostic comparison and requires
that flag. The exact options include `--worker-data`, `--strategies`,
`--variants`, `--starting-cash`, `--min-trades`, `--min-sessions`, `--alpha`,
`--max-generations`, `--max-confirmatory-attempts`, and `--config`.
Fit diagnostics and LLM proposals can explain or bound search; they never read
held-out/sealed evidence, mutate the runtime config, emit a proof, or promote
an entry.

## Diagnostic inventory and shadow

`research.diagnostic_suite` resolves existing identities; it does not invent or
tune strategies, open a ledger, emit proofs, or turn unavailable observations
into negative returns. The current inventory is exactly 43 unique equity arms:
the diagnostic-shadow rule cohort, the registered mechanism cohort, and the
equity IBR registry. Each arm has an isolated simulated account and the report
sets `eligible: []`, `proofs: []`, and `authorizing: false`.

Run it with a new output path:

```bash
.venv/bin/python -m research.diagnostic_suite \
  --data /absolute/path/to/frozen/market.jsonl \
  --agent-config config.yaml \
  --out /absolute/path/to/new/inventory.json \
  --diagnostic-only --workers 2
```

The command also accepts `--full-fit-diagnostics` and `--ibr-max-events`.
The latter is a hard event budget: input over the budget becomes unavailable,
never silently truncated. The CLI returns exit code 2 for a completed,
non-authorizing diagnostic report.

The broker-free live shadow (`deploy/shadow.py`) uses the same runtime signal,
setup, risk, exit, and cost code in virtual books. Direct CLI defaults to the
24-rule cohort; `ALPACA_SHADOW_INCLUDE_IBR=1` or `--diagnostic-include-ibr`
adds the seven registered IBR arms. The environment value is strict `0` or `1`.
The shadow has no broker-secret or order path. Session acceptance requires
stable deployment/code/cohort identities, exact calendar coverage, current
source observations, and genuine event/cursor progress. An unchanged heartbeat
is not a completed session.

`deploy/trader_projection.py` is a bounded, diagnostic-only projection of the
recorder corpus. It preserves non-quote events and replay-boundary quotes,
measures costs before compaction, and writes a manifest only after source
identity rechecks. It has no promotion or broker-facing path.

## Paper-only incumbent and isolated epoch stores

`research.paper_trial` selects exactly one predeclared diagnostic catalog arm in
the paper runtime. It freezes the rule, policy, feed/provider, cohort, code
identity, and report root. It cannot use `auto`, arbitrary specs, runtime LLM
selection, options, delayed feeds, or live mode. `config.yaml` keeps the block
disabled; an operator must explicitly enable a profile and pass flat-book and
account-bound activation checks. `research.require_validated_variant` must stay
true as a configuration guard, but the explicit paper-trial path deliberately
does not require an ordinary EdgeLedger proof; it runs the one frozen arm with
separate account and identity controls. All paper-trial outcomes and verdicts
are persisted with `authorizing: false` and `proof_authority: false`.

The separate `research.paper_epoch` store is an append-only operational
primary/shadow epoch. It must use a separate SQLite path and immutable identity
bindings. Its operator CLI is:

```bash
.venv/bin/python research.py paper-epoch status --db PATH
.venv/bin/python research.py paper-epoch verify --db PATH
.venv/bin/python research.py paper-epoch export \
  --db PATH --epoch EPOCH_ID --output-root research/results/paper-epochs
```

Lifecycle writes (`create`, `start`, `record`, `stop`, `complete`, `seal`)
require their bounded JSON input schemas and are independent of the authorizing
EdgeLedger. Paper trial details, replacement safety, and shadow session
acceptance are in [`docs/paper-shadow-trials.md`](../docs/paper-shadow-trials.md).

## Provider preflight

`llm-preflight` makes one bounded, non-authorizing provider request. It does not
load market data or touch trading state. Authentication/configuration failures
and deployment-not-found responses stop the scheduled cycle; bounded timeout,
rate-limit, and server errors are recorded as degraded provider evidence and
the deterministic path may continue. The provider response is never a signal,
an edge proof, or an entry authorization.

```bash
.venv/bin/python research.py llm-preflight --agent-config config.yaml
```

The scheduled wrapper persists a redacted bounded result in its cycle status and
operational history. The provider model/deployment is metadata; the finite rule
grammar and its validators remain the source of executable behavior.

## Related contracts

* [`research/protocol.md`](protocol.md) — the normative replay, evidence, and
  version/authority contract.
* [`docs/edge-diagnostics.md`](../docs/edge-diagnostics.md) — non-authorizing
  inventory, quote calibration, and shadow diagnostics.
* [`docs/preregistered-hypotheses.md`](../docs/preregistered-hypotheses.md) —
  frozen preregistration manifests and their exact CLI.
* [`docs/paper-shadow-trials.md`](../docs/paper-shadow-trials.md) — paper-only
  incumbent and isolated shadow workflows.
