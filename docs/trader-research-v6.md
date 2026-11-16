# Trader research projection

This filename is retained for existing links. It is a current workflow guide,
not an archive of a dated experiment or a performance report.

## Scope

The trader projection is a bounded, diagnostic-only view of the recorder
corpus. It preserves every non-quote event (including revisions and gaps) and
the quote observations needed to reproduce replay boundaries: the first and
last quote in each market minute and the latest quote visible at each recorded
bar-availability cutoff. Quote-cost schedules are measured from the unthinned
stream before compaction.

The projection verifies source files, calendar/source sidecars, provider/feed
identity, row and byte budgets, and source stability during the run. It writes a
registration manifest only after a final identity check. A failed run may leave
a failure receipt or temporary files; it never produces a successful manifest.
There is no promotion or broker-facing path.

Diagnostic projection rows cannot authorize an entry or a proof. They may
explain missing coverage, quote availability, discontinuities, or modeled cost,
but they do not establish actual fills, buying power, shortability, or runtime
readiness. Fresh source-gated parity evidence and actual execution/operational
controls remain independent requirements.

## Command

Run from the repository root with an explicit new output path:

```bash
.venv/bin/python deploy/trader_projection.py \
  --corpus runtime/research/recorded \
  --config config.yaml \
  --output /absolute/path/to/new/projection
```

The required flags are `--corpus`, `--config`, and `--output`. The optional
flags are:

* `--session market-YYYY-MM-DD.csv` (repeatable) to select exact sessions;
* `--csv-mode {recorder,external}` for the source CSV contract;
* `--max-retained-rows N` to bound retained rows;
* `--max-output-bytes N` to bound output size; and
* `--disk-floor-bytes N` to require free space before publication.

The corpus may be a recorder root, its `sessions` directory, or one CSV. The
runtime config supplies the exact calendar and provenance policy. The command
does not download data, open a broker connection, mutate the source corpus, or
change runtime configuration.

## Downstream use

Validate the resulting normalized view with the source preflight before replay:

```bash
.venv/bin/python research.py validate-data /absolute/path/to/new/projection/compact.jsonl \
  --provider alpaca --feed iex
```

Directory output writes `compact.jsonl` inside the chosen directory. An explicit
`--output` ending in `.jsonl` selects that file instead. Use
`--diagnostic-only` for historical/mixed rows. Historical backfill,
delayed-SIP, missing quotes, or bar fallback remain explicitly diagnostic and
cannot be promoted into an authorizing ledger. The projection is a data-shape
and availability tool; it is not a source-mode upgrade.

For the wider contract, see [`research/protocol.md`](../research/protocol.md)
and [`docs/edge-diagnostics.md`](edge-diagnostics.md).
