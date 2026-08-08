# Audit aggregate provenance and limitations

These CSVs were moved on 2026-08-07 from the untracked root directory
`audit-data/`.  Their source files were last modified on 2026-08-05.  No raw
journal database, event payloads, export command, schema manifest, or code/data
fingerprint accompanied the files, so the archive records the bytes as found
without reconstructing their provenance.

## Contents

- `journal_event_counts.csv` — counts grouped by event kind.
- `paper_variant_summary.csv` — aggregate trade count, average R, and PnL by
  paper variant.

Recorded SHA-256 digests (for byte-level provenance) are
`05a05a1b449114b4c9dbabdcb61820f32b6f31f738f9a462915dcee4ca6a9a25` for
`journal_event_counts.csv` and
`547313f49f8cbbf0fc1802f75b79bf3a95e72dd81c0704ff9667d30b6ea77bbe` for
`paper_variant_summary.csv`.

## Limitations

These are descriptive snapshots, not replayable evidence.  They contain no
event timestamps, instrument/window boundaries, eligibility filters, funding
settlement proof, or per-trade rows.  The small trade counts and unknown
selection/cost context make them unsuitable for qualification, promotion, or
changing a frozen conclusion.  Use the active journal/evidence and recorded
data paths for any new analysis.
