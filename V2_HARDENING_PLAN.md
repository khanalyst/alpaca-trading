# Current hardening roadmap

This file is retained as a short roadmap. The former batch-by-batch V2 plan is
stale; current operation is documented in [`README.md`](README.md),
[`SETUP.md`](SETUP.md), and [`OPERATIONS.md`](OPERATIONS.md).

## Implemented foundations

- journal replay and G2 fidelity gating;
- immutable decision-ledger rows, including vetoes as zero-return actions;
- isolated shadow portfolios and bounded workers;
- static and adaptive first-class variants;
- FindingsStore schema 8 with proposal/lock/history metadata;
- forward qualification, family correction, qualification events, and
  content-addressed T3 packets;
- exploratory tournament settings capped below live authority;
- VM recorder and nightly research workflow.

## Remaining work in priority order

1. **R1-04:** resolve a cited T3 packet hash in the packet store and verify
   strategy ownership before accepting a registry claim.
2. **R2-02:** add focused fixtures for the highest-risk exploratory modules, or
   explicitly keep a study historical-only.
3. **R4-01:** run and review every declared tournament setting against the
   approved VM corpus; do not treat the old single-point report as the new
   multi-setting result.
4. **Approval boundary:** add an explicit reviewed packet approval command that
   writes an immutable registry/config revision without silently starting live
   capital.
5. **R3 recommendations:** separate one-shot studies and split the large engine
   only when those files are naturally touched. Neither is a safety blocker.

## Operational boundary

The VM export under `vm-import/` is a test fixture, not a runtime default. Azure
resource provisioning is not implemented in this repository; `SETUP.md` is the
human deployment guide and `OPERATIONS.md` is the runbook.
