# Current findings — September 19, 2026

This is the single active pending list. The [strategy review](strategy-variant-review.md)
covers all 43 registered arms and their research questions. The
[activation receipt](paper-activation-2026-09-15.json) records the completed
paper release; historical reports are not current performance results. The
source correctness fixes are now released and deployed, but they did not
qualify an edge or activate the frozen paper trial.

## Verified release and runtime snapshot

The verified source fix is commit `79c48c90fd6449fd46f5c762384f9f42264cbb43`,
present on local, GitHub, and VM `main`. Full GitHub CI succeeded (all four
test shards and the container) in [run 35360972940](https://github.com/khanalyst/alpaca-trading/actions/runs/35360972940).
The earlier local verification of all **2,173 tests** remains unchanged. The
running VM image is pinned to validated source commit `79c48c9`, image
`alpaca-agent-trading:signal-fix-79c48c9`, digest
`sha256:da939d12584c9a193fb92001697d14af100c4efc28749fba7375089fa96b4067`.
A later documentation-only commit does not require changing this image or
proof identity.

Services restarted at **2026-09-18 18:32 UTC**. Postverification at
**2026-09-19 10:05:00.898393 UTC** found trader, watchdog, recorder, shadow,
research, and dashboard healthy/running with zero restarts; each reported the
expected image commit/digest. Shadow reported 31 arms across 13 families, 31
cursors, `candidate_error_count=0`, `candidate_errors_clear=true`, no
`last_error`, an active cohort
`shadow:diagnostic:cohort:1498a4db8c678c016c515f0e5005cc6a13b1098ba99e5172b51ee5cd2830d08c`,
and activation
`shadow:diagnostic:activation:3ea19e6096c1530f267f441695e65da28417afd79d42abd467b4f00c386cfc48`
with warmup **2026-09-18**. Its code identity is
`cbcf60629069166d708d7dbde17930081c7dd6d9b770fdb562253d648dd4bbf1`.

The prior 20,000-event errors are cleared without changing the 20,000 ingest
bound. Saturday's `source-data-stale`/`cursor-stale` flags are from a closed-
market snapshot and are not live freshness proof. There were 0 quoteable opens,
0 replay fills, and 0 actual fills; research is waiting for forward sessions,
with no fresh accepted count or new profitability report in postverification
(last pre-release state: 0/30).
The recorder root remains `recorded-forward-2026-09-16`; recorder health is
good but `gap_observed` persists.

The frozen paper state hash is exactly and remains unchanged:
`d7ea90b69d13bdcbaa806e44639dc6f5d76f6e5d119d179f00b9290678d7ec69`.
`operator_pause=true` remains set; the trial is still running but paused under
its old identity, with 0 accepted sessions and 0 outcomes before and after
postverification. A fresh GET-only account/positions/orders check at
**2026-09-19 10:05:03.366213 UTC** was active and correctly bound, with zero
positions and zero open orders. Config SHA-256
`1d8dd1a29f57ef6559ac6302ebc977a8c82f5be745027b07a3469ccbfdd43797` is
unchanged; no gate, cost, feed, or risk modification was made.

The VM backup `/var/tmp/alpaca-release-79c48c9-tnfqaxv6` contains `environment.before`,
paper state/journal, the shadow database tree, EdgeLedger, and a hash manifest;
no persistent data was deleted. The paused supervisor never constructs an Engine,
so cancellation was not required for this paused deployment and was not
performed. Engine-based authenticated check/status/resume/run operations with
the new code would reject the active old trial identity. Any future explicit
activation must auditably retire/replace that identity through the evidence and
risk gates; it must not bypass them.

## Retained state and historical reset

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

The **2026-09-16** retained-state snapshot recorded
`operator_pause=true`, a running-but-paused frozen paper trial, 0 accepted
sessions, 0 closed outcomes, no new orders, and zero positions/open orders on
the paper endpoint. Its shadow activation identity and warmup date are
historical; the current release identity is recorded above.

## Earlier dated runtime evidence

- At **2026-09-16 16:01 UTC**, a short ten-sample check saw fresh recorder
  cycles of roughly 6–9 seconds at about 30-second cadence. Shadow polls were
  about 0.95 seconds with about 28 seconds of source lag and all 31 arms ready.
  This was not a sustained benchmark.
- At **2026-09-17 05:37 UTC**, recorder health was healthy with the market
  closed and all bar/quote symbols seen. Shadow was **UNHEALTHY** with
  `ShadowError: shadow replay validation event bound 20000 exceeded`; the
  other five service checks were healthy. The paper trial was paused with no
  residual risk. Disk usage was 4.5 GB used, 121 GB free (4%).
- At **2026-09-17 15:21 UTC**, the server was still on image `c7f996a`; shadow
  replay failed at the 20,000-event bound, fresh accepted sessions were zero,
  quotes were stale during market open, and the paper trial was paused. This
  is a dated pre-release snapshot; the release evidence above supersedes its
  deployment status.
- The 5-second idle disk sample (**0.38% busy**) and 0.062-second recorder
  cycle are market-closed observations, not latency proof. Keep this health
  snapshot separate from the later database diagnostic below.

## Released correctness fixes

These September 18 corrections are now committed, CI-verified, and deployed
under the new code/evidence identity. They remain separate from the historical
`c7f996a` repairs described below. All frozen registered strategy IDs/specs,
risk limits, and cost assumptions remain unchanged; prior evidence and the old
trial identity cannot be carried over as an activation shortcut.

- Research-bar normalization now rejects booleans and nonpositive prices,
  invalid volume, conflicting row/override durations, and `bar_1m` intervals
  other than 60 seconds. Explicit duration metadata is preserved. Direct rule
  evaluation validates OHLCV and an explicit 60-second interval. Volume
  confirmation/breakout requires positive current volume and positive prior
  mean; VWAP requires explicit valid volume and a positive total while allowing
  a zero current bar.
- Now-aware runtime generation requires completed one-minute bars, aware and
  available timestamps, and symbol consistency; cross-sectional evaluation
  requires only the intended SPY context. Benchmark fit, signal quality, and
  replay now share completed point-in-time context at the actual subject
  decision time, so future-observed data cannot generate diagnostics.
  Historical backfill remains an explicit diagnostic opt-in.
- Live SDK bar validation is stricter. The recorder rejects an explicit row
  symbol/key mismatch and detects internal session gaps even in its first
  observation window. IBR replay/live evaluation shares the close-relative
  buffer predicate for the `close_confirmed` boundary; the separately hashed
  legacy wick mode is retained and was not retuned. Direct IBR validation now
  rejects malformed bars and range summaries in both signal and setup paths;
  decision-time checks exclude future observations and bound ATR history.
  Explicit minute intervals are validated. A supplied `max_ibr_width_pct`
  mapping value must be finite and nonnegative, preserving zero versus omission.
  Replay rejects a timezone configuration other than `America/New_York`;
  aware UTC market timestamps remain valid.

The earlier local verification passed all **2,173 discovered tests** across
disjoint shards (edge 61, factory 6, research 1,147, runtime 959), with zero
failures, errors, or skips. Compilation, diff checks, and independent synthetic
parity checks across all 24 registered rule arms passed; these checks are not
market-performance evidence. The verified runtime/replay code identity is
`cbcf60629069166d708d7dbde17930081c7dd6d9b770fdb562253d648dd4bbf1`.

## Historical pre-release blockers and resolved fixes

A fresh census at **2026-09-16 19:33 UTC** found one new partition, zero
historical partitions, and zero accepted sessions of the 30 required. The
epoch was `INVALID`: current epoch activation metadata was unavailable because
the database file could not be opened. Reader UID `10001` had a read-only
mount; it could read WAL-mode `shadow.sqlite3`, but the `-wal`/`-shm` sidecars
were absent, producing `SQLITE_CANTOPEN`. This was a dated pre-release
blocker, not a current deployment assertion.

Before release, full-session replay validation reused ingestion's per-poll
budget of `max_events=20,000`; the diagnostic path had a separate 200,000-event budget.
In the authenticated SELECT-only diagnostic at **2026-09-17 07:51 UTC**, the
fresh shadow held **29,441** Sep 16 events (**3,908** `bar_1m` and **25,533**
quotes). The `gate_sessions` pairs were only the preserved-ledger authorizing
legacy IBR baseline candidate (`fd51e0b3031142c1a91cecc019bb52bf`) and its
root control (`shadow:null:fd51e0b3031142c1a91cecc019bb52bf`); the candidate
is strategy `ibr`, variant `ibr.baseline`, status `candidate`. Thus the cap
failure is on the gate-evidence replay path, not on the 31 diagnostic arms.
These matched replay rows are not proof of qualification or broker fills.
No cap was raised and no events were truncated.

The release removes the dashboard's immutable fallback, which checked for an
absent or empty WAL before opening an immutable reader even though a live writer
could commit between those operations. An unavailable normal read-only
connection now reports unavailable rather than returning a potentially stale
snapshot. The writer-side long-lived, nontransactional SQLite connection in
`deploy/shadow.py` preserves the WAL/SHM relationship for normal read-only
census consumers.

The released replay fix gives each session an independent
200,000-event budget (configurable up to a hard maximum of 1,000,000), retains
the 20,000-event incremental bound, and keeps diagnostics at 200,000 events.
Quote-context compaction is O(n log n); context overflow fails closed without
truncating rows. The deployed trial remains running but paused. Its
operator-cancellation path is audit-preserving for this zero-evidence trial,
but was not invoked: the paused supervisor never constructs an Engine, so
cancellation was not required. The workbench selects the configured fresh
corpus and labels retained historical evidence. No cancellation or terminal
state has completed.

Historical source verification for the prior repair passed: the 84-test combined replay/ingestion suite, the
52-test operator/runtime/cohort/status suite, and the 190-test deployment/UI/
WAL suite. After removing the dashboard immutable fallback, four focused
dashboard reader tests passed, including visibility of new WAL commits and
refusal to write. Test counts overlap and should not be summed. Compilation,
diff checks, and the disposable synthetic recorder-key profiler also passed;
none of these checks establishes production latency or profitability.

## Completed release items

GitHub CI, the paused VM rollout, the writer WAL lifetime, independent
replay/context budgets, current-corpus workbench selection, and historical
evidence labels are deployed under the release identity above; the verified
runtime observations are recorded in the snapshot above.
The frozen paper trial was not resumed or activated. Cancellation is not a
completed release item: it was not required for the paused deployment and was
not performed.

## Pending priorities

1. Run a market-open latency benchmark and collect valid fresh sessions with
   IEX quote coverage, freshness, completed-bar publication, cadence, and all
   required symbols/arms. Closed-market health and local microbenchmarks are
   insufficient. `gate_sessions()` still returns all matched authorizing
   sessions, and every poll replays them again. Those replay updates refresh
   retention timestamps, so active historical work can keep growing. The
   complete-session budget/streaming fix resolves the immediate 20,000-row
   failure, not this scaling problem. Any future reuse must verify the exact
   code, configuration, calendar, source, and evidence identities; skipping
   validation or deleting negative sessions is not an acceptable optimization.
2. Measure executable paper costs, fills, slippage, rejection, and protection
   behavior; keep modeled shadow fills separate. The unchanged 25 bps stress /
   0.30 cost-risk ceiling requires about 83.33 bps of stop distance before
   tick geometry, so the 30 bps floor alone cannot pass. Zero entries can be a
   correct refusal; do not relax gates or widen stops to create trades.
3. Meet the evidence gates before judging an edge: the paper trial needs 20
   accepted sessions and 20 closed parent outcomes (60 accepted sessions is
   its review limit), while research readiness needs 30 accepted sessions and
   separate qualification/proof checks. A paper pass does not authorize live
   trading. No profitable edge is proven.
4. Complete new research and after-cost validation on untouched forward data,
   including later-signal, factor/sector/duration, markout, MFE/MAE, matched
   control, and signal-to-fill attribution work as applicable. Keep all 43
   frozen arm identities separate.
5. Transition or retire the frozen old trial identity through an audited,
   explicit gate-reviewed operation before any future activation. Do not use
   the new code to resume the old identity or claim paper activation.
6. Use bounded immutable snapshots and predeclared controls for frozen
   comparisons on untouched data. Re-running examined history or adding
   variants does not substitute for forward confirmation.

## Why negative results can still be visible

The deployed read-only API check at **2026-09-17 15:21 UTC** returned **no
research Markdown reports and no research-workbench records**, seven candidates,
zero proved edges and zero paper outcomes. The scheduler still reported zero
accepted sessions of 30 required, with one rejected completed partition. It
was blocked by the missing current shadow catalog after the replay error, not
by a newly measured loss. The recorder was running on the fresh September 16
root but its quotes were stale at this market-open observation. Latency is
therefore still an open operational issue.

Historical studies were retained deliberately. The archived September 12
inventory classified 43 arms as **36 execution-blocked, six negative net point
estimates, and one small positive estimate**. The positive `ibr.range.45`
result was only **+$1.055386 over 42 one-share hypothetical trades**, with
partial runtime parity and no quotes; it was not a qualified edge. Blocked
zero-trade arms have unknown expectancy, not negative expectancy.

The archived September 10 structured audit records 650 hypothetical variant
trades: **-$2,549.15 reference-price P&L**, **$25,599.69 modeled execution drag**,
**$1,599.98 modeled fees**, and **-$29,748.81 net** (rounding applies).
79.08% ended by time exit. Thus costs explain most of that historical modeled
loss, but the reference-price result was also weak. These are previously
examined, bar-filled simulations—not actual broker losses or fresh results.
The separate September 11 report says eight of twelve mechanisms were positive
before costs and all twelve negative afterward in a diagnostic counterfactual;
that report assertion was inspected, not independently rerun this turn.

The archived source members are `docs/edge-results-2026-09-12.json`,
`outputs/negative-performance-audit-2026-09-10/derived-findings.json`, and
`outputs/edge-evidence-fix-2026-09-11/RESULTS.md` in the verified report-cleanup
archive below. The gap is sufficient after-cost signal value and reliable
forward evidence, not simply too few variants. No strategy parameters or cost
assumptions were relaxed to manufacture a positive result.

## Historical work and separate research

The **September 15** latency baseline (99.47% persistent-disk busy and a
42.81-second recorder cycle) is historical. Profile/shadow-recorder
instrumentation and the report-cleanup archive are on local and GitHub `main`
at commit `83b72dc`; full CI succeeded in the
[GitHub Actions run](https://github.com/khanalyst/alpaca-trading/actions/runs/35208859786).
The local **303 targeted tests** and local benchmarks remain historical
pre-release verification; they do not demonstrate a server latency reduction.
The new fixes listed above are deployed under the validated image identity. No
journal mode, durability, freshness threshold, risk, strategy, feed, or
cadence setting was changed. A future trial transition establishes a new
trial/evidence identity and must be audited without resuming trading. No
strategy or risk-gate change is pending.

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
reports and cleanup archive are represented in GitHub `main` commit `83b72dc`.
The older `outputs/paper-activation-2026-09-14/rollback-volumes-c9368e2.tar.zst`
backup also remains untouched.
