# Main repository review plan

This is the current plan derived from the review dated **July 30, 2026**.
It replaces the dated review as the working status document. The dated source
is no longer the place to update implementation status.

## Current pipeline

The repository has two evidence paths:

1. **Authoritative journal path:** recorded snapshots → replay/G2 → paired
   forward analysis → family correction → qualification event → reviewed T3
   packet.
2. **Exploratory tournament path:** approved OHLCV corpus → pre-registered
   strategy settings → bounded scoring → exploratory report.

The tournament can withhold capital and identify candidates, but it cannot
raise a strategy above `T2_CANDIDATE`. The authoritative path is the only path
that can support a promotion review. A qualification event does not edit the
strategy registry or switch live capital.

## R1 — find, document, and lock an edge

| Item | Current status | What remains |
| --- | --- | --- |
| R1-01 | **Fixed** | Registered experiment identity is immutable; changed claims require a new variant ID. |
| R1-02 | **Fixed** | Findings indexing preserves audit documents instead of orphaning them. |
| R1-03 | **Fixed** | Every registered momentum variant has a committed scorecard checked against the registry. |
| R1-04 | **Partially complete** | Packet references have the required hash shape. The remaining hardening is resolving the hash in the packet store and checking that it belongs to the cited strategy. |

## R2 — test without overfitting

| Item | Current status | What remains |
| --- | --- | --- |
| R2-01 | **Fixed** | `forward-qualify` applies the persisted family correction across evaluated axes and rechecks it at qualification/T3 packet time. |
| R2-02 | **Partially complete** | High-risk `edge_lab` and hypothesis paths have focused coverage. The remaining exploratory studies still need targeted fixtures or an explicit historical-only label. |
| R2-03 | **Complete** | Exploratory evidence may set a shipped default. When it does, the baseline is documented as a fitted point and its fit window, selection rule, provenance, and code/config fingerprints are recorded beside it. This may not raise a tier. |

## R3 — keep the system small

| Item | Current status | What remains |
| --- | --- | --- |
| R3-01 | **Deferred recommendation** | Distinguish permanent research infrastructure from one-shot studies when those files are next touched. |
| R3-02 | **Deferred recommendation** | Split the large engine at a natural execution/reconciliation seam; do not refactor working trading code just for size. |
| R3-03 | **Deferred, low priority** | Keep the minimum standalone facts in README and SETUP because their current checks intentionally make both documents usable alone. |

## R4 — hypotheses and variants

| Item | Current status | What remains |
| --- | --- | --- |
| R4-01 | **Code complete; evidence pending** | The six backtestable research strategies declare settings and the tournament scores each setting. Run the approved VM corpus, review the per-setting reports, and do not reuse historical single-point tiers as current multi-setting results. |
| R4-02 | **Complete** | Runtime hypotheses use `contract_params` and registered settings through the existing agent/registry mechanism. Adaptive proposals produce first-class variants with exact values, lock state, reasoning, and history. |
| R4-03 | **Observation; no action** | Entry-threshold axes are not required for the current runtime variant work; retain this as an explicit observation. |

## Minimal action order

1. Keep the VM recorder and nightly workflow running. Do not move the VM export
   into the repository's runtime location.
2. Export a corpus with its manifest, window, code/config fingerprints, and
   findings database backup.
3. Run the tournament once against that approved corpus with all declared
   settings; preserve the report as exploratory evidence.
4. Let shadow collect the authoritative decision ledger for the runtime
   hypotheses and their adaptive variants. A cycle uses one LLM decision set;
   variants do not create extra model calls.
5. Run `forward-qualify` only after G2 and the common evidence window are valid.
   It must produce the forward edge qualification event before any PAPER
   stage can begin.
6. Generate and review the T3 packet. The remaining capital-control boundary
   is an explicit reviewed registry/config approval; evidence never silently
   changes live strategy selection.

## What is fully automated today

- bounded LLM numeric proposals for registered runtime hypothesis settings;
- rejection of unknown, duplicate, out-of-range, or insufficiently reasoned
  proposals;
- proposal locks and historical values in `findings.db`;
- first-class variant identity and exact parameter persistence;
- bounded parallel shadow evaluation with independent account state;
- one-setting-per-strategy scheduling in a cycle;
- decision-ledger capture, forward qualification, family correction, and an
  immutable qualification event;
- content-addressed T3 packet generation and nightly orchestration.

## What is not fully automated

- resolving a T3 packet hash to a registry strategy and applying the reviewed
  registry/config change;
- making a tournament result authoritative; it remains exploratory by policy;
- supplying the VM's live corpus to this checkout; the handoff still needs an
  export/manifest operation;
- proving exchange-only fill races, liquidation state, or an exchange/SQLite
  transaction as one atomic event.

The first item is the only remaining manual step in the edge-to-strategy lock.
It is a deliberate capital-control boundary, not a missing research
calculation. The future minimal addition is an explicit approval command that
consumes a reviewed packet and writes an immutable registry/config revision
without starting live capital implicitly.

## Definition of done for the remaining work

R4-01 is done operationally when the approved corpus produces a reproducible
report for every declared setting, with the benchmark/null included and the
setting count recorded. R2-02 is done when the high-risk exploratory modules
used for that report have deterministic fixture coverage or are clearly marked
historical-only. R1-04 is done when the cited packet hash is resolved and
strategy ownership is checked. No remaining R3 recommendation blocks evidence
collection or safe demo operation.

