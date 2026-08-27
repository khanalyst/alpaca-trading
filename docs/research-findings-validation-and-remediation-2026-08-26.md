# Research findings validation and bounded remediation — 2026-08-26

## Scope and safety boundary

This note records an independent review of the externally supplied
`RESEARCH_FINDINGS_AND_REMEDIATION.md` against the repository, focused tests,
and the preserved diagnostic counterfactual artifact. The supplied document was
evidence to assess, not an instruction source.

No conclusion in that document authorizes an edge, a production threshold, or
an expected-cost change. `config.yaml`, the shipped 25 bps stress scenario, the
`0.30` stressed-cost/risk limit, and the expected spread/slippage/fee schedule
remain unchanged by this remediation.

## Validation result

The central reachability finding is real, but several explanations and proposed
remedies were not supported as written.

### Confirmed

- Under the preserved diagnostic corpus, the `0.30` arm produced 1,775 signal
  opportunities, all refused by `stressed_cost_risk_limit`, and no trades.
- The `0.60` arm admitted 513 trades. Their descriptive pooled mean was about
  `-0.541550R`, with net P&L about `-$33,039.82` in that diagnostic replay.
- For an equity plan whose stop risk is 30 bps of entry notional, a 25 bps
  entry-notional stress is `25 / 30 = 0.833333...` of nominal risk. The shipped
  `0.30` limit therefore rejects the grammar-floor geometry. The minimum static
  equity stop distance that can clear those controls is `25 / 0.30 =
  83.333333...` bps.
- All 513 admitted entry and exit fills in that artifact were bar-derived, so
  the P&L is diagnostic rather than observed execution evidence.

### Corrected or unsupported

- The shipped expected equity cost is not 21 bps round trip. With 4 bps quoted
  spread, 6 bps adverse slippage, and 0.5 bps fee per side, the symmetric
  bar-reference model is 17 bps round trip. Executable quotes already contain
  spread, so the corresponding quote-reference model is 13 bps including fees.
- Expected fill costs and the stress veto are independent controls. Recalibrating
  expected spread/slippage/fees does not change the 25 bps scenario or the
  `0.30` ratio, so it cannot by itself make a 30 bps stop admissible.
- “Statistically indistinguishable from random” was not established. The cited
  simple-random result used another corpus, and the cost-arm replay did not run
  a same-corpus, candidate-preserving randomized null or an isolated
  opportunity-level effect estimate.
- The system is not absolutely untradeable. Wider-stop candidates can clear the
  shipped boundary, and the existing runtime/factory/null parity regression
  contains both an admitted wide-stop plan and a rejected floor plan.
- The reported integration-test gap was overstated. A cross-lane
  `simulate_account`/`RiskEngine`/randomized-null parity test already existed;
  the remaining weakness was that higher-level factory calls could omit the
  validated runtime configuration.
- The overlapping “last 20 outcomes total no more than -2R” rule was used as an
  automatic lifecycle stop without repeated-look calibration. A one-look
  `-z * sigma * sqrt(n)` replacement would not solve overlapping sequential
  looks either.
- A separate 177,504-bar audit and its derived percentages could not be
  reproduced from a committed script, immutable input binding, and persisted
  artifact. It is not accepted as repository evidence.

## Bounded remediation

### Reproducible counterfactual evidence

The stressed-cost counterfactual now persists a compact projection of every
signal-opportunity terminal row in each arm. The projection carries identity,
disposition/rejection, plan and stop geometry, exit reason, fill provenance,
gross/fee/net economics, and a descriptive reference-price cost decomposition
where all required inputs exist. Non-finite inputs remain explicitly
unavailable rather than entering JSON as `NaN` or infinity.

Rows are deterministically sorted and content-addressed individually and as a
collection. Aggregate summaries now expose gross P&L, exit-reason counts, stop
distance in basis points, and reference-gross/execution-drag/fee/total-drag
distributions. The artifact explicitly says its account replay is stateful and
path-dependent and that neither opportunity-isolated replay nor a randomized
null was run. These additions are diagnostic-only and do not alter proofs,
promotion, FDR, or runtime configuration.

### Pre-run execution geometry

Discovery receives a configuration-only execution-geometry brief before it
proposes a family or mutation. For equities, the brief separates:

- the expected cost schedule (17 bps symmetric bar-reference round trip and 13
  bps executable-quote round trip under shipped defaults); and
- the independent stress policy (25 bps of entry notional, `0.30` maximum
  cost/risk, 83.333... bps minimum static stop geometry, and a 30 bps grammar
  floor that does not clear it).

The brief is diagnostic and non-authorizing, contains configuration rather than
outcomes, and marks the analogous static option threshold unavailable because
option premium risk and per-contract fees make it plan-dependent.

### Rolling surveillance authority

The 20-outcome/-2R rolling calculation remains in ingestion, reports, the
dashboard, and compatibility constants, but it is advisory telemetry. Crossing
it does not change a candidate lifecycle. Immediate paper-outcome demotion is
reserved for the existing sequential held-out-drift test; the independent
configured trial review can still park an edge after its evidence window closes
below its floor. Pinned candidates retain their audit context on an authoritative
demotion.

This is a bounded safety correction, not a claim that the current sequential
model has been empirically calibrated for every return distribution. Its
operating characteristics should be checked against proof-epoch outcomes before
changing its threshold or distributional model.

## Evidence still required before any policy change

1. Run the existing read-only calibration against actual entry and exit fills,
   scoped by vehicle and execution stratum. Do not infer costs from IEX bars or
   diagnostic bar fills.
2. Rerun the frozen cost-arm study on the same immutable corpus with an
   opportunity-isolated measurement and a candidate-preserving randomized null.
   Persist exact quantity semantics, pair coverage, cluster/window assignments,
   and hashes.
3. Calibrate lifecycle surveillance under the exact repeated-look and proof-epoch
   process, including false-demotion probability and detection delay.
4. Only then review the expected cost schedule, stress scenario, ratio limit, or
   grammar floor. Any change is a separate, audited configuration decision and
   must rerun its affected evidence.

### Current local fill-calibration result

The read-only calibration was run against `runtime/paper/journal.db`, SHA-256
`7cea6b56a36d842f0e1626f9b6b15625417a25b01450e3c067fd061efbc8df4d`
(69,632 bytes; last modified `2026-08-21T13:51:12+0400`).

- Equity: 6 journal rows representing 3 unique orders, but 0 referenced fills,
  0 terminal orders, and no observed cost statistic. Verdict:
  `insufficient_data`; authorization exit code: 2.
- Options: no option fills. Verdict: `insufficient_data`; authorization exit
  code: 2.

The result supplies no empirical bias estimate and therefore supports leaving
the expected cost schedule unchanged. It is not evidence that the current
schedule is accurate; it is evidence that this journal cannot yet calibrate it.

## Verification

The remediated tree passed 270 selected tests:

- 14 counterfactual evidence tests;
- 57 edge-discovery/lifecycle tests;
- 30 paper-performance and trial tests;
- 30 slot-lifecycle and runtime/factory/null stressed-cost parity tests;
- 137 strategy-factory, discovery, and LLM-boundary tests; and
- 2 dashboard rolling-telemetry tests.

An independent review additionally exercised nearby cost/runtime/vehicle/option
suites and found the option stop-basis defect that this change then corrected
with a regression. `compileall` over `agent`, `research`, `deploy`, and `tests`,
plus `git diff --check`, passed. A full repository-suite rerun remains required
before release; the selected tests are not represented as that full gate.
