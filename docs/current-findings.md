# Pending work — September 17, 2026

This is the single active pending list. The [strategy review](strategy-variant-review.md)
covers all 43 registered arms and their research questions. The
[activation receipt](paper-activation-2026-09-15.json) records the completed
paper release; historical reports are not current performance results.

## Reset and retained state

The data reset completed at **2026-09-16 15:56:16 UTC**; the restart finished
at 15:56:18 UTC on the same `c7f996a` image. The environment corpus root is
`/app/runtime/research/recorded-forward-2026-09-16`; code, profile, account,
and risk thresholds were unchanged.

The off-host complete-volume archive is
`outputs/research-reset-2026-09-16/research-volumes-20260916.tar.zst`,
14,474,715,570 bytes, SHA-256
`a9ec37b4bfd4fcaa814d5c349e3cf6f5a4a4bb31b7fca7445e6ef61186e1f80e`.
All 626 payload-file hashes and all five restored SQLite integrity checks
passed before deletion. The reset then removed 462 files totaling
76,326,550,333 bytes, including the old `recorded/` and
`recorded-forward-2026-09-08/` corpora, old shadow working files, the
temporary quote index, and two generated reports. The 164 preserved files
were rehashed. Canonical EdgeLedger, negative experiments, calibrations,
paper journal, state, risk, and account binding were preserved; old shadow
history remains recoverable in the archive.

`operator_pause=true` was retained. The active frozen paper trial remains
`running` but paused, with **0 accepted sessions, 0 closed outcomes, and no
new orders**. A read-only authenticated broker check at **2026-09-16
19:34 UTC** found zero positions and zero open orders on the paper endpoint;
`live=false`. There is no residual paper risk. The fresh shadow activation
identity is `77f6cece47a97b5507ef2f34fc3e84581fc77fea0bda75029dd63de02c009546`;
its warmup date is 2026-09-16.

## Latest runtime evidence

- At **2026-09-16 16:01 UTC**, a short ten-sample check saw fresh recorder
  cycles of roughly 6–9 seconds at about 30-second cadence. Shadow polls were
  about 0.95 seconds with about 28 seconds of source lag and all 31 arms ready.
  This was not a sustained benchmark.
- At **2026-09-17 05:37 UTC**, recorder health was healthy with the market
  closed and all bar/quote symbols seen. Shadow was **UNHEALTHY** with
  `ShadowError: shadow replay validation event bound 20000 exceeded`; the
  other five service checks were healthy. The paper trial was paused with no
  residual risk. Disk usage was 4.5 GB used, 121 GB free (4%).
- The 5-second idle disk sample (**0.38% busy**) and 0.062-second recorder
  cycle are market-closed observations, not latency proof. Keep this health
  snapshot separate from the later database diagnostic below.

## Replay and census blockers

A fresh census at **2026-09-16 19:33 UTC** found one new partition, zero
historical partitions, and zero accepted sessions of the 30 required. The
epoch was `INVALID`: current epoch activation metadata was unavailable because
the database file could not be opened. Reader UID `10001` had a read-only
mount; it could read WAL-mode `shadow.sqlite3`, but the `-wal`/`-shm` sidecars
were absent, producing `SQLITE_CANTOPEN`.

The full-session replay validation reuses ingestion's per-poll budget of
`max_events=20,000`; the diagnostic path has a separate 200,000-event budget.
In the authenticated SELECT-only diagnostic at **2026-09-17 07:51 UTC**, the
fresh shadow held **29,441** Sep 16 events (**3,908** `bar_1m` and **25,533**
quotes). The `gate_sessions` pairs were only the preserved-ledger authorizing
legacy IBR baseline candidate (`fd51e0b3031142c1a91cecc019bb52bf`) and its
root control (`shadow:null:fd51e0b3031142c1a91cecc019bb52bf`); the candidate
is strategy `ibr`, variant `ibr.baseline`, status `candidate`. Thus the cap
failure is on the gate-evidence replay path, not on the 31 diagnostic arms.
These matched replay rows are not proof of qualification or broker fills.
No cap was raised and no events were truncated.

The existing `deploy/dashboard.py` immutable fallback checks for an absent or
empty WAL before opening an immutable reader. A live writer can commit between
those operations, so copying this fallback into census is not an approved fix.
The proposed minimal writer-side long-lived, nontransactional SQLite
connection in `deploy/shadow.py` would preserve the WAL/SHM relationship, but
it still needs narrow tests and review. A replay-correctness change in
`research/live_shadow.py` changes evidence identity. The active running trial
rejects that identity change even with zero observations, and there is no
explicit abandon/retire API. Do not invent a terminal trial state.

## Pending priorities

1. Review and narrowly test the writer-side WAL/SHM fix and the bounded,
   complete-session replay fix. These are proposed changes only; no new code
   changes will be deployed under the data-reset approval.
2. Obtain direction for an audit-preserving trial transition before deploying
   any code-identity change. Preserve the frozen trial record and do not
   silently replace, abandon, or retire it.
3. Run a market-open latency benchmark and collect valid fresh sessions with
   IEX quote coverage, freshness, completed-bar publication, cadence, and all
   required symbols/arms. Closed-market health and local microbenchmarks are
   insufficient.
4. Measure executable paper costs, fills, slippage, rejection, and protection
   behavior; keep modeled shadow fills separate. The unchanged 25 bps stress /
   0.30 cost-risk ceiling requires about 83.33 bps of stop distance before
   tick geometry, so the 30 bps floor alone cannot pass. Zero entries can be a
   correct refusal; do not relax gates or widen stops to create trades.
5. Meet the evidence gates before judging an edge: the paper trial needs 20
   accepted sessions and 20 closed parent outcomes (60 accepted sessions is
   its review limit), while research readiness needs 30 accepted sessions and
   separate qualification/proof checks. A paper pass does not authorize live
   trading. No profitable edge is proven.
6. Use bounded immutable snapshots and predeclared controls for frozen
   comparisons on untouched data. Re-running examined history or adding
   variants does not substitute for forward confirmation.

## Historical work and separate research

The **September 15** latency baseline (99.47% persistent-disk busy and a
42.81-second recorder cycle) is historical. The local **303 targeted tests**
and local benchmarks remain accurate as unreleased verification; they do not
demonstrate a server latency reduction. Profiling/optimization changes remain
local and unreleased, and no journal mode, durability, freshness, risk,
strategy, feed, cohort, or cadence setting was changed.

Point-in-time catalyst/news/corporate-action inputs; factor, sector and
duration exposure; hedged residual execution; later-signal comparisons; and
executable markouts, MFE/MAE, matched controls, and signal-to-fill attribution
remain separate research work. The cohort, epoch-binding, recorder-status,
future-observation, and explicit-zero software repairs are already implemented
and tested; they are not pending fixes. The reset removed superseded runtime
artifacts only after verified archival; negative research history was retained.

The earlier report cleanup removed 145 superseded reports/logs, not the current
research-reset archive. Its local originals remain recoverable from
`outputs/findings-cleanup-2026-09-15/superseded-reports.tar.gz`; the four tracked
reports remain in Git commit `8068c73deb157d14fb84e49514c8f8eb8d0e9de5`.
The older `outputs/paper-activation-2026-09-14/rollback-volumes-c9368e2.tar.zst`
backup also remains untouched.
