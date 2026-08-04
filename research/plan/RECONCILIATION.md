# Current evidence reconciliation policy

This is the short policy used by the current pipeline. It replaces the old
branch-by-branch reconciliation plan.

## Authority

- Real-time strategy assignments first produce deterministic `WORKED`,
  `FAILED`, or `INCONCLUSIVE` outcomes. `WORKED` is RESEARCH_ONLY and cannot
  change capital or configuration.
- Journal replay is authoritative only after a current G2 PASS.
- The OHLCV tournament is exploratory. It may reject, rank, or withhold a
  strategy, but it awards no tier above `T2_CANDIDATE` and cannot promote live
  capital.
- A qualification event is tied to one strategy/version, one declared axis,
  one common evidence window, exact variant values, and the immutable decision
  ledger.

## Configuration exception

Exploratory evidence may set a shipped default when the selection is explicitly
documented. When it does, the baseline is a fitted point, not an independent
null. The fit window, selection rule, corpus/provenance, and code/config
fingerprints are recorded beside the baseline. This exception changes a
configuration default; it never raises an evidence tier.

## VM handoff checklist

Export the corpus with its manifest and time window. Preserve the findings DB
backup, journal/research fingerprints, and the report generated from that
export. On the receiving Mac, run the tournament against the extracted data;
do not replace the repository runtime directory or treat the fixture as the VM
authority.

An ignored local `vm-import/` directory, if present, is optional read-only
historical data. It is not part of the clone, is not required for tests or
handoff, and is never a current runtime default.

See this directory for
status and [`../../OPERATIONS.md`](../../OPERATIONS.md) for commands.
