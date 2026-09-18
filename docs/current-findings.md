# Pending work — September 18, 2026

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
`live=false`. No residual exposure was observed in that snapshot. The fresh shadow activation
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
- At **2026-09-17 15:21 UTC**, the deployed server was still on image
  `c7f996a`; shadow replay still failed at the 20,000-event bound, fresh
  accepted sessions remained at zero, quotes were stale during market open,
  and the paper trial remained paused.
- The 5-second idle disk sample (**0.38% busy**) and 0.062-second recorder
  cycle are market-closed observations, not latency proof. Keep this health
  snapshot separate from the later database diagnostic below.
- No fresh runtime or broker-state check was performed for the September 18
  local changes; the runtime and broker observations above remain dated
  snapshots, not current state.

## September 18 local correctness fixes (source-only)

These are local working-tree corrections, not committed, pushed, or deployed
this turn. They are separate from the historical `c7f996a` repairs described
below. They leave all frozen registered strategy IDs/specs, risk limits, and
cost assumptions unchanged, but change the code identity; prior evidence and
activation state therefore cannot be carried over as a shortcut.

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

Final local verification passed all **2,173 discovered tests** across disjoint
shards: edge 61, factory 6, research 1,147, and runtime 959, with zero failures,
errors, or skips. Compilation and diff checks passed. Independent synthetic
parity checks across all 24 registered rule arms found no output or ID changes
on the tested valid prefixes; these checks are not market-performance evidence.
The verified runtime and replay code identities both equal
`cbcf60629069166d708d7dbde17930081c7dd6d9b770fdb562253d648dd4bbf1`.
No new market-profitability run, orders, deployment, resume, cancellation, or
deletion occurred this turn.

## Pending validated rollout and diagnostic recompute

Complete the pending CI and a verified paused rollout of the local corrections
and the previously committed WAL/replay fixes under the new code/evidence identity
before using their outputs. Then recompute benchmark-fit, signal-quality, and
replay diagnostics on the unchanged frozen arms/specs and untouched forward
data, using the shared completed point-in-time context at each actual subject
decision time. Do not reuse prior evidence or activate from a recompute;
historical backfill remains diagnostic-only by explicit opt-in. The paused
trial's cancellation remains authorized-flat/audited only, and fresh forward
data plus market-open latency evidence remain required.

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

The deployed dashboard's immutable fallback checks for an absent or empty WAL
before opening an immutable reader. A live writer can commit between those
operations. The local correction removes that fallback: an unavailable normal
read-only connection reports unavailable instead of returning a potentially
stale snapshot. The writer-side long-lived, nontransactional SQLite connection
in `deploy/shadow.py` preserves the WAL/SHM relationship for normal read-only
census consumers. Both corrections are included in this source revision but
not yet deployed.

The implemented replay fix gives each session an independent
200,000-event budget (configurable up to a hard maximum of 1,000,000), retains
the 20,000-event incremental bound, and keeps diagnostics at 200,000 events.
Quote-context compaction is O(n log n); context overflow fails closed without
truncating rows. The deployed trial remains running but paused; the implemented
operator-cancellation path is audit-preserving for this
zero-evidence trial: GET-only broker flat check, journaled before-state, and
never resume. No cancellation or terminal state has completed on the deployed
server. The workbench now selects the configured fresh corpus and labels
retained historical evidence. These software changes are not yet deployed.

Historical source verification for the prior repair passed: the 84-test combined replay/ingestion suite, the
52-test operator/runtime/cohort/status suite, and the 190-test deployment/UI/
WAL suite. After removing the dashboard immutable fallback, four focused
dashboard reader tests passed, including visibility of new WAL commits and
refusal to write. Test counts overlap and should not be summed. Compilation,
diff checks, and the disposable synthetic recorder-key profiler also passed;
none of these checks establishes production latency or profitability.

## Pending priorities

1. Complete GitHub CI for this fix revision, then perform a verified paused
   rollout of the writer WAL lifetime, independent replay/context budgets,
   current-corpus workbench and historical labels, and audited zero-evidence
   trial cancellation. Do not report deployment until it is verified.
2. Execute the audited cancellation only after that test/push gate: perform a
   GET-only broker flat check, journal the before-state, and never resume the
   paused paper trial. Preserve its frozen record and do not claim a completed
   cancellation or terminal state before rollout evidence exists.
3. Run a market-open latency benchmark and collect valid fresh sessions with
   IEX quote coverage, freshness, completed-bar publication, cadence, and all
   required symbols/arms. Closed-market health and local microbenchmarks are
   insufficient. `gate_sessions()` still returns all matched authorizing
   sessions, and every poll replays them again. Those replay updates refresh
   retention timestamps, so active historical work can keep growing. The
   complete-session budget/streaming fix resolves the immediate 20,000-row
   failure, not this scaling problem. Any future reuse must verify the exact
   code, configuration, calendar, source, and evidence identities; skipping
   validation or deleting negative sessions is not an acceptable optimization.
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
The local **303 targeted tests** and local benchmarks remain accurate as
unreleased verification; they do not demonstrate a server latency reduction.
The new fixes listed above are source-verified but not deployed. No journal
mode, durability, freshness threshold, risk, strategy, feed, or cadence setting
was changed. A rollout changes the code/evidence identity and must establish
a new verified trial identity without resuming trading. No strategy or
risk-gate change is pending.

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
