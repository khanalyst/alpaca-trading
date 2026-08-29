# Edge-discovery upgrade — 2026-08-29

This change set improves the information produced by the research factory
without weakening live or authorizing controls. It is based on the completed
11-family × 4-variant diagnostic report and the subsequent trader review.

## Validated premises

- Configured equity cost is 17 bps round trip for symmetric bar references:
  two 8 bps execution legs plus 1 bp of both-side fees.
- Executable bid/ask references already contain spread, so their configured
  round trip is 13 bps: two 6 bps slippage legs plus 1 bp of fees.
- The 25 bps stressed-cost scenario at a 0.30 maximum cost/risk ratio implies
  an 83.33 bps minimum static equity stop. This is an admission gate, not an
  expected-cost estimate and not an instruction to widen stops.
- A 0.5% risk budget combined with a 25% position-notional cap means the cap
  binds for stop distances below roughly 2% of price. Tight-stop variants can
  therefore deploy only a fraction of their configured risk budget.
- SIP is not currently an authorizing research/runtime feed. IEX provenance,
  expected costs, stress limits, and live sizing remain unchanged until a SIP
  entitlement and a separately audited exact-feed replay exist.

## Implemented changes

### 1. Conditional forward-return screen

Every variant now receives a fit-only `signal_quality` diagnostic at 5, 15,
30, 60, 120, and 390 minutes. It reports:

- directional forward return from the completed signal close;
- a deterministic random-entry control in the same symbol and session;
- candidate-minus-control return;
- the configured bar-reference cost hurdle and return after that hurdle;
- censored horizons and exact reasons such as insufficient future bars or an
  internal gap;
- session, symbol, and session-symbol counts;
- signal time-of-day buckets.

The screen uses the first causal signal per symbol/session to match the
executable replay's exposure policy. It is explicitly conditional-forward-
return analysis, not canonical cross-sectional IC, and cannot authorize or
promote a candidate.

### 2. Predicate funnel

The runtime and research evaluator now share one staged implementation. A fit
prefix can identify the exact terminal stage:

1. minimum history;
2. positive price;
3. family predicate;
4. allowed side;
5. each configured confirmation;
6. ATR availability;
7. volatility band;
8. timestamp;
9. entry window;
10. emitted signal.

The diagnostic report aggregates tested, passed, failed, and pass rate per
stage. This separates a bad economic hypothesis from an inactive threshold,
a confirmation bottleneck, or a data-availability problem.

### 3. Correct gap classification

A session with at least one mature, contiguous prefix and no signal is now a
valid no-signal observation even if another unrelated prefix is gapped. The
factory returns `no_contiguous_feature_window` only when no mature prefix can
be evaluated. Opening-anchored families retain their session-open continuity
requirement so a missing opening minute cannot silently redefine the opening
range.

### 4. Fill and sizing telemetry

Per-variant diagnostics now distinguish:

- planned entry pricing from realized entry/exit fill sources;
- quote→quote, quote→bar, bar→quote, and bar→bar paths;
- quote ages, feeds, providers, and P&L by fill-source pair;
- intentionally bar-priced intrabar stop/target exits from boundary quote
  fills;
- risk-sized quantity, notional-cap quantity, cap-binding rate, planned risk,
  realized risk, and planned/configured risk utilization;
- 17 bps expected bar cost, 13 bps expected quote cost, and the separate
  25 bps stress scenario.

### 5. Better bounded search

- Deterministic hypotheses now state an economic mechanism and a falsification
  tied to forward-return/null-control evidence as well as held-out net results.
- The pre-registered deterministic coordinate ladder is no longer deleted by
  a broad model near-duplicate threshold. Exact executable aliases are still
  removed; discovery/replacement and novel model proposals retain semantic
  duplicate suppression.
- Compact predicate, signal-quality, cost, and sizing aggregates are available
  to model-assisted ordering. Raw market rows, held-out evidence, proof fields,
  and long-horizon prompt bulk remain excluded.

## Controls deliberately unchanged

- IEX remains the authorizing equity feed.
- Expected spread/slippage/fee configuration is unchanged.
- The stressed-cost scenario and maximum stressed-cost/risk ratio are
  unchanged.
- One executable trade per symbol/session remains the exposure contract.
- Gate, FDR, held-out, proof, and paper-promotion schemas are unchanged.
- Quote imbalance, microprice, time-of-day volume normalization, benchmark
  residualization, and new trailing exits are not added to the live grammar;
  each requires causal context plus factory/runtime/shadow parity first.

## Verification

Focused and integration suites cover signal parity, rule grammar, gap scope,
cost/stress arithmetic, fill causality, signal quality, fit diagnostics, model
context projection, deterministic search, strategy factory, and report
rendering. The end-to-end factory replay also passes with the new diagnostics.
