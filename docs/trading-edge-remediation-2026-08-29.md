# Trading-edge remediation — 2026-08-29

## Scope and provenance

This note records the verified remediation state on Saturday, 2026-08-29. The
work is on `codex/trading-edge-remediation`, branched from
`claude/signal-quality-null-control` at `a42348d`; `main` is `e1a4a93`. No
deployment was performed.

The implementation is fail-closed and diagnostic until the required evidence
exists. The local edge database currently contains seven rows, all in
`candidate` state, with zero `validated` or `champion` rows. Equity and option
calibration reports are blocked with `insufficient_data`. No recorder corpus is
present locally, so a fresh real-market result cannot be produced here. No
positive trading edge is currently validated.

## Implemented controls

### P0 — source, calendar, and liveness truth

- Recorder partitions carry exact Alpaca calendar/source metadata, including
  explicit holiday closure and early-close boundaries. Deployment provenance is
  retained with the partition/source identity.
- Recorder sampling uses a fixed 30-second cadence. Per-symbol quote and
  completed-bar watermarks are durable, and readiness requires both to be no
  older than 30 seconds for every required symbol.
- Scheduler/service liveness is reported separately from research evidence and
  readiness. A live scheduler or a market-closed heartbeat is not evidence of a
  ready corpus or a validated edge.

### P1 — execution accounting and fail-closed gates

- Reports and runtime state retain gross, net, fees, slippage, planned risk,
  delivered risk, and provider/feed provenance for each leg.
- Marketable-limit checks and the account-wide gross-exposure cap are enforced
  before submission.
- Null/control, coverage, lifecycle, and execution-rejection checks are
  stricter and distinguish sparse or underpowered data from an explicitly
  `execution_blocked` fit.

### P2 — bounded research and path evidence

- The pre-replay screen is fit-only. It emits control/report rows, uses `p=1`
  placeholders when a comparison is unavailable, and records a terminal
  current-hypothesis no-edge outcome. A changed corpus reseeds that hypothesis.
- Target/hold path telemetry measures reachability from actual entry through the
  bounded hold. Lower-target and hold proposals are restricted to a finite,
  bounded ladder and are non-authorizing.
- The grammar now has twelve families. The twelfth,
  `cross_sectional_residual`, is shares-only, compares residuals against SPY,
  and requires synchronized one-minute context. The universe remains the
  shipped 24-ETF basket; future family or universe changes must be supported by
  screen and cross-sectional evidence, not an arbitrary replacement.

### P3 — feed and shadow authorization

- Authorizing equity evidence must use the exact IEX or SIP feed. The
  `delayed_sip` feed is diagnostic only. Option authorization remains exact OPRA
  evidence, with strict quote-age and leg provenance checks.
- Concurrent shadow workers use content-addressed manifests. Readers verify
  the manifest digest before use; missing, unreadable, or mismatched manifests
  are quarantined and cannot advance a shadow watermark or FDR boundary.

### P4 — stressed-cost calibration

- The per-symbol/session stress ladder is 9, 15, 25, and 50 bps. Calibration is
  disabled by default and requires an explicit operator-controlled activation
  path; it does not self-authorize.
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
