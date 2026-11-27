# Research protocol

This is the normative contract for the research modules. Code and registered
manifests are authoritative; this document names their current boundaries and
versioned behavior. A new result must carry its source, configuration, code,
replay, gate, and (where applicable) instrument identities.

## 1. Source and time

Provider payloads are normalized through `research.market_data` before feature
calculation. A bar, quote, or option snapshot retains provider/feed, market
timestamp, `as_of`, `observed_at`, calendar/session metadata, and `source_mode`.
Required records are available no earlier than
`max(timestamp, as_of, observed_at)`. `as_of <= observed_at` is mandatory. A
late recorder event may signal only when it is observed; it may not recreate an
earlier entry.

`forward_observed` is the sole authorizing source mode. `historical_backfill`,
mixed or implicit modes, delayed feeds, missing provenance, and partial feeds
are diagnostic-only. `research.source_validation.validate_source` is run before
opening an authorizing ledger. Diagnostic mode is an explicit policy, not a
weaker parser: malformed records, unsupported modes, future observations, feed
contradictions, and source mutation between passes still fail closed.

Equity evidence must match the configured provider and exact `iex` or `sip`
feed. Option evidence must match `opra`; option and equity books are separate.
The exact Alpaca calendar is required for every session, including early-close
metadata. A missing calendar boundary is a refusal. Session grouping uses
`America/New_York`.

## 2. Causal replay invariants

Every replay implementation must preserve these invariants:

1. Signals use completed bars and the exact causal maturity/continuity window
   of the normalized rule. Session-anchored families use the observed session
   prefix; bounded families require adjacency only over the bars their feature
   actually reads.
2. A missing immediate entry bar is no trade. A gap through a level fills at the
   gap open. An intrabar stop/target tie resolves to the stop. A signal bar does
   not retrospectively close a newly opened position.
3. Strict boundary fills require a fresh executable quote. A bar fallback is
   explicitly diagnostic and cannot enter an authorizing projection.
4. All lanes use `research.costs` for spread, slippage, fee, stress, and
   realized-risk arithmetic. An entry slippage cap is shared by runtime,
   factory, IBR, and randomized-null quote entries; malformed or over-cap
   quotes fail closed with their stable refusal reasons.
5. Equity legs require exact IEX/SIP quote provenance; option legs require OPRA.
   Provider, feed, quote timestamp/age, and fill source are persisted per leg.
   A missing/stale/bar-only leg is diagnostic.
6. Positions use the calendar-derived configured force-flat timestamp, normally
   ten minutes before the exact close. Strict equity lanes refuse a missing
   exit quote rather than manufacture P&L. The rule deadline
   contract orders equal timestamps as session force-flat, thesis deadline, then
   maximum hold.
7. Research and runtime may retain operational aliases, but every exit row also
   carries the canonical cause: `stop`, `target`, `max_hold`,
   `thesis_deadline`, `session_force_flat`, `data_discontinuity`, or `unknown`.
8. No lane pools equity with option P&L, independent arms with one another, or
   diagnostic observations with authorizing observations.

The v3–v7 exit and rule grammar extensions are validated by
`agent/contracts/rule.py`. V4 targets are frozen from the completed signal
prefix; breakeven/trailing amendments apply only to later bars; V5/V6/V7
context, mechanism, geometry, and correction fields are opt-in and
family-scoped. See [`docs/rule_strategy_v3_exit_protocol.md`](../docs/rule_strategy_v3_exit_protocol.md).

## 3. Costs and risk geometry

The compatibility cost model (`static_v1`) is 4 bps spread, 6 bps adverse
slippage, and 0.5 bps per-side notional fee, with a 0.65 currency-unit option
fee per contract per side. The stress ladder is 9/15/25/50 bps, each an all-in
round-trip charge against entry notional. `per_symbol_v2` is a separate
order-type-aware resolver used only when explicitly selected and provenance is
present. A measured quote schedule is frozen by feed/provider, coverage
policy, percentiles, quote floor, and content hash; missing cells fail closed.

Runtime spread and slippage values are rejection limits, not expected costs.
The stressed cost/risk check rejects an authored stop below the configured
geometry requirement before sizing or fill. It never widens the stop or moves a
fixed-R target. Equity prices are rounded toward the entry to valid broker
ticks before sizing; a rounded bracket that no longer straddles entry is
rejected.

## 4. Evidence lanes and authority

The lanes are deliberately ordered:

```text
source preflight
    -> causal fit replay and diagnostics
    -> chronological held-out controls
    -> sealed qualification
    -> parity-matched live shadow
    -> verified proof and durable FDR
    -> explicit paper/runtime selection
```

The current gate constants in `research.gates` require, by lane, 100 trades,
30 sessions, and 30 clusters for backtest and qualification; the parity-matched
shadow tail requires 150 trades and 30 sessions/clusters. These are evidence
floors, not tuning knobs. If a corpus cannot support a floor, the result is
underpowered or unavailable, not a negative expectancy claim.

Fit diagnostics, signal-quality studies, mechanism comparisons, historical
backfills, candidate leaderboards, and paper-trial verdicts are non-authorizing.
They may record isolated candidate or trial history and identify or falsify a
hypothesis, but they cannot by themselves produce a verified proof, consume a
confirmatory FDR allocation, select an entry, or override a runtime pause. The
source-gated factory/qualification/shadow path is the path that may populate
authorizing EdgeLedger and FDR evidence, and only after the current replay,
gate, provenance, and operational checks pass. The paper-incumbent exception
keeps its separate account and identity controls and deliberately has no
normal EdgeLedger proof authority. Only fresh source-gated parity evidence plus
actual execution/operational controls can advance deployment authority.

## 5. Versioned gate and replay contracts

`research.gates` writes `verified-research-gate.v3` envelopes. The envelope's
`gate_protocol_version` is currently 2; version 1 remains supported solely to
re-verify stored legacy envelopes under its original semantics. Protocol 2
binds expected round-trip cost, actual/null controls, qualification evidence,
finite multiple-test inputs, and durable online-FDR metadata.

The current replay generation is `REPLAY_ENGINE_EPOCH = 7`. Stored runs from
older epochs remain readable for audit but do not validate, champion, or
authorize until recomputed under epoch 7. The active confirmatory FDR scope is
`shadow-confirmation-v6` and uses the v6 durable method. v5 and legacy scopes
remain separate; v7 constants are versioned for explicit future adoption but
are not activated by documentation.

Proof verification recomputes stored source rows and gate decisions, not merely
hashes supplied by a caller. It checks source/config/code/replay epochs,
matched controls, raw p-values and corrections, qualification session sets,
quote provenance, cost stress, randomized-null evidence, and the durable FDR
allocation. A caller-supplied scalar q-value, p-value, alpha, or allocation is
not authority.

The current lifecycle is monotonic: discovery can create a candidate,
backtest evidence can advance it to a backtest-passed state, and an eligible
parity-matched shadow proof can validate/champion it. Runtime refuses stale or
unverified identities. Explicit demotion is a safety action; there is no
backward rollback that bypasses evidence.

## 6. Search and mutation constraints

The strategy factory evaluates each variant in an isolated simulated account.
The model receives fit-period diagnostics and eligible completed fit lessons.
Holdout, sealed and later forward observations stay outside the prompt.
Discovery can propose a new hypothesis in an executable family. Tuning stays
within the current root's family and declares the bounded parameter or
structural change being tested. After a qualifying terminal negative, the
replacement path can propose a materially different hypothesis, including a
different registered family. The validator enforces the grammar, parameter
bounds, supported exits and declared changes before evaluation.

Multiple-test correction applies to every evaluated variant, including tuned
variants and dependence-cluster checks. Underpowered, execution-blocked, and
inconclusive outcomes do not become positive or negative lessons. Diagnostic
fit/power reports may describe why a search is blocked; they are not powered
negative edge conclusions.

## 7. Shadow, paper, and epoch isolation

The broker-free shadow reuses runtime signal/setup/risk/exit/cost code in
isolated virtual books. It persists a manifest, event cursor, source offsets,
health heartbeat, candidate rows, paired baseline/null rows, and acceptance
reports. A source/config/code/cohort identity change starts a new evidence epoch;
rows are not merged across epochs. Missing progress, stale symbols, unresolved
quarantine, incomplete calendar coverage, or an unchanged cursor prevents
session acceptance.

The paper-incumbent trial selects one exact fixed diagnostic arm and persists
its account/cohort/code/policy identity. It is paper-only and always carries
`authorizing: false` and `proof_authority: false`. The trial's sample/verdict
state is independent of EdgeLedger proof authority. A trial pause, operator
pause, account identity mismatch, or replacement blocker remains active even if
its point estimate is favorable.

`research.paper_epoch` is a separate append-only SQLite control-plane store for
an isolated paper primary and shadows. Its schema and lifecycle are managed by
`research.py paper-epoch`; it must not share an EdgeLedger database.

## 8. Reproducibility and preregistration

Preregistered hypotheses live in `research/preregistered.py`. A manifest hash
alone is not the full instrument: v2 also pins the measurement-instrument
identity, session-quality rules, source/feed, look records, anchors, and
append-only decision record. A manifest or instrument change is a new version;
existing frozen versions are never silently rewritten. A registered pass earns
entry to the existing proof pipeline; it never authorizes trading or changes a
runtime configuration.

Use the exact preregistration CLI:

```bash
.venv/bin/python -m research.preregistered \
  --data DATA.jsonl --out NEW_REPORT.json \
  --hypothesis vwap-reversion-control-adjusted.v2 \
  --look-records LOOK_RECORDS.jsonl \
  --as-of AS_OF_UTC
```

The output path must not already exist. V2 hypotheses require
`--look-records`; each look is decided once under an exclusive lock and later
runs reproduce the recorded session list, instrument digest, and result.
When supplied, `AS_OF_UTC` must be an ISO 8601 timestamp with a UTC offset.
Historical v1 manifests remain available for reproducibility but are not a
route to current confirmatory authority. See
[`docs/preregistered-hypotheses.md`](../docs/preregistered-hypotheses.md) for
the frozen manifest fields and ids.
