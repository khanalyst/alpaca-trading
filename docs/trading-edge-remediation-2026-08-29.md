# Trading-edge remediation — 2026-08-30

## Scope and provenance

This note records the verified remediation state on Sunday, 2026-08-30. The
original P0–P3 work was based on `main` at `a68593c`; this document also includes
the subsequent sparse-cost follow-up. Deployment is performed separately through
the supported runbook after verification.

The implementation is fail-closed and diagnostic until the required evidence
exists. This code change does not manufacture a corpus result, enable live
trading, or claim a positive edge. A fresh positive-edge conclusion still
requires exact-feed recorded data, chronological validation, qualification,
and parity-matched shadow evidence.

## Implemented controls

### P0 — fail-closed defaults and stale evidence

- Shipped configuration explicitly keeps stressed-cost calibration disabled
  and its artifact path empty. Missing, malformed, stale, or mismatched evidence
  therefore resolves to the conservative configured fallback rather than
  silently changing admission.
- Signal-quality measurement and compact handoff contracts are both versioned
  at v2. Pre-v2 measurements or screens are reported as stale/unknown, cannot
  suppress a fresh replay, and cannot contribute promotion evidence.
- Documentation distinguishes expected execution cost from the independent
  stressed-cost admission gate; neither a lower measured spread nor a single
  calibrated rung is described as proof of positive expectancy.

### P1 — measured-cost evidence and causal replay

- Quote schedules require explicit single-provider and single-feed provenance,
  reject missing identities, carry order-independent quote/session hashes, and
  reject a missing or tampered schedule hash before use.
- The measured arm resolves symbol, half-hour cell, and displayed-depth impact
  inside account simulation before admission and fills. The previous post-hoc
  repricing path was removed, so configured-versus-measured results use the same
  causal replay boundary.
- The causal measured resolver requires an explicitly requested symbol/time
  bucket to meet the schedule's `min_quotes_per_cell` quote-count floor. Missing
  or under-covered cells fail the complete configured-versus-measured rerun
  instead of silently falling back to a symbol-wide or universe aggregate,
  preventing missing-cell fallback/selection from producing a misleading partial
  comparison.
- The immutable diagnostic bundle binds corpus, configuration, specs, schedules,
  provider/feed identity, and chronological fit/validation splits. Reports add
  per-family/symbol/time-bucket opportunities, refusals, executions, gross/net
  P&L, R decomposition, win rate, profit factor, drawdown, exits, uncertainty,
  and cost-model provenance. Existing paths cannot be overwritten, and supplied
  stale content hashes are rejected.

### P2 — cross-sectional semantic containment

- The grammar's twelfth family,
  `cross_sectional_residual`, keeps its compatibility identifier but implements
  a shares-only, single-leg SPY-relative directional-momentum signal, not a
  beta-neutral or hedged spread. It requires synchronized one-minute context
  and fails closed for SPY self-reference and non-comparable symbols.
- A bounded default comparable-equity ETF set is enforced. An authored
  `eligible_symbols` list can narrow that set but cannot expand it; rates,
  credit, metals, commodities, and unknown symbols cannot be treated as SPY
  residuals by stale or externally supplied events.
- Signal-quality and fit diagnostics report eligibility by symbol separately
  from market-context availability, so intentional structural exclusions do
  not look like missing data.

### P3 — shadow calibration and readiness

- Empirical stress calibration can be enabled only in the shadow lane through a
  dedicated operator switch and artifact path. A path alone is inert; invalid
  flags, artifacts, provider identities, or feed identities fail closed. The
  candidate/runtime configuration is not mutated.
- Shadow telemetry reports effective scenario/source/hash plus generated-signal
  opportunities, admissions, refusals, rates, and bounded refusal reasons.
  Recorder health reports liveness separately from data readiness, including
  quote/bar symbol counts, watermarks, provenance, and realized cadence gaps.
- Sessions that currently contribute matched gate evidence are reloaded through
  a bounded timestamp-indexed query and reevaluated even after the forward event
  floor advances. A changed replay can therefore revoke stale candidate rows
  while its paired root control remains independently evaluated; no broad WAL
  rescan is introduced.

### P4 — stressed-cost calibration

- The per-symbol/session stress ladder is 9, 15, 25, and 50 bps. The shipped
  25 bps scenario is the default/fallback. Calibration is disabled by default
  and requires an explicit operator-controlled activation path with a valid
  artifact; an enable flag alone is insufficient and it does not self-authorize.
- An activation-ready artifact must bind the exact provider/feed, content hash,
  sufficient disjoint chronological held-out sessions, and one artifact-wide
  effective-after boundary. Missing or unusable cells resolve to the configured
  scalar fallback.
- A scheduled calibration-only pass can produce this diagnostic artifact, but
  blocked or insufficient reports remain non-authorizing until the operator path
  and all evidence checks succeed.

### P4 acceptance from the infeasible-window analysis

The attached “infeasible window” trader analysis was reviewed and accepted as
the P4 workstream. Its accepted remedies are: gross-versus-drag measurement and
the fit-only pre-screen; empirical per-symbol/session equity stress calibration;
target/hold reachability telemetry; the shares-only SPY
`cross_sectional_residual` family; and the fixed 30-second recorder cadence with
paired quote/completed-bar readiness watermarks. These remedies remain bounded,
diagnostic, and evidence-gated as described above.

Deliberate non-actions remain explicit: there is no blind universe expansion,
and no trailing or partial exit was added before path evidence. Those choices
are not claimed as completed remedies; any future change requires the stated
evidence and operator review.

These controls do not assert that the software creates profit. They preserve the
current evidence boundary until a complete recorder corpus, exact-feed replay,
calibration, qualification, and parity-matched shadow proof are available.
