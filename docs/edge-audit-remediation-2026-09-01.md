# Edge-audit remediation — 2026-09-01

This note records the disposition of the current research/execution audit. It
is an operational clarification, not a proof, ledger entry, FDR decision, or
promotion record.

## Disposition matrix

| Audit area | Disposition | Verified position and boundary |
| --- | --- | --- |
| Active online FDR scope | Accepted | New confirmatory tails use `shadow-confirmation-v6` with LORD++ `W0=alpha/2`; v5 rows retain `W0=alpha` and are historical/audit-only, isolated from v6. |
| Readiness arithmetic | Accepted | Readiness is 150 offline + `shadow_selection_sessions=30` + `shadow_confirmation_sessions=30` = **210 sessions**. The compatibility 60-session shadow count is those two disjoint tails; it is not 180 sessions and not one undivided shadow tail. |
| Resting-bracket evidence | Accepted | A non-gap equity stop/target observed on an exact-feed bar is the deliberate resting-bracket exception to trigger-time quotes, but it remains conservatively adverse-cost charged. Entry evidence is still quote-backed. |
| Gap/time/deadline exits | Accepted | Gap, time, and `exit-before` exits require a fresh executable quote; bar-only or stale evidence remains diagnostic and cannot authorize. |
| Effective equity stop geometry | Accepted | The stop floor is `max(30 bps, active stressed-cost scenario / max-cost-to-risk ratio)`. When binding, fixed-R targets are recomputed and authored/effective geometry, scenario, ratio, and binding are retained as telemetry. |
| v4 exit modes | Accepted | v4 adds frozen session VWAP and rolling-mean targets, a monotone trailing stop, and an `exit-before` deadline. Breakeven is not a v4 invention: it was already the v3 equity extension. |
| Automatic cost comparison | Accepted | Deploy research cycles automatically attempt the configured-vs-measured comparison when bars, quotes, and a factory report exist; status, artifact path, and delta are persisted. Failure or missing inputs are non-authorizing and non-fatal. |
| Separate research verdict | Accepted | Effect estimate/interval/power/MDE telemetry is descriptive and non-authorizing; it must not mutate the ledger, FDR, proof, promotion, or LLM adaptive-evidence path. |
| Cross-sectional residual family | Rejected (audit error) | The shares-only synchronized `cross_sectional_residual` family already exists in the bounded grammar. It is not a missing-family remediation or a request to add a second family. |
| Multi-symbol expansion | Data-dependent | Deferred until a known-positive end-to-end reproduction with explicit symbol coverage and a new identity/proof; it is not an opportunistic tuning arm. |
| Partial exits | Partial | Not implemented. The capability remains deferred because broker lifecycle and position reconciliation risks are unresolved; no audit claim should treat partial exits as shipped. |

The matrix deliberately separates accepted safety/evidence behavior from
diagnostic-only research telemetry. A diagnostic result can explain a refusal or
estimate an effect, but cannot authorize an edge or spend cumulative alpha.
