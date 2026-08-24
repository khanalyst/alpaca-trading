# LLM edge research engineering handoff — 2026-08-24

## Scope and safety boundary

This work repairs recorder/corpus availability, hardens diagnostic edge
discovery, runs a fresh `gpt-5.6-terra` research epoch, measures one frozen
cost-policy counterfactual, and adds an isolated paper-primary/shadow control
plane. Research remains non-authorizing unless it completes the existing
held-out and live-shadow protocol. Paper execution success has zero alpha and
cannot promote an edge. No order was placed during this work.

## Source and deployment state

- Branch: `main`
- Local and GitHub commit: `3a64ffcb328eba612f8a1f46e45fb3c47d1ba4fc`
- Commit subject: `Repair edge research diagnostics and paper epochs`
- VM repository: `/opt/alpaca-agent-trading`, fast-forwarded to the same commit
- VM image: `sha256:b25518b0724ab05103ec0f289628cb166d261b94bcec6116e0db0389cfab7a00`
- Trader: healthy, `operator_pause=false`, state `PAUSED`, reason
  `validated_edge_required`; broker and local position/order snapshots were
  flat before resume. The watchdog is healthy. No order was submitted.

## Completed engineering changes

1. Recorder memory and provenance
   - Default forward-observation lag is 15 minutes.
   - Late-seen data is durably marked historical before append.
   - Provenance repair is streaming and idempotent.
   - Recent dedup keys moved from an unbounded JSON object to bounded SQLite.
2. Corpus preprocessing
   - Added an immutable, content-addressed cache with source/config/code/context
     identities, locks, hashes, quarantine, atomic publication, and writable
     materialization on lookup.
3. Research diagnostics
   - Execution outcomes now distinguish `executed`, explicit `refused`, and
     `no_signal`, with durable reason/stage/detail fields and an invariant that
     prevents silent unclassified no-trades.
   - The tuning schema admits exact aggregate refusal counts but never raw
     event rows.
   - Diagnostic-only Terra epochs can use model discovery/tuning without
     opening or updating proofs, ledgers, FDR wealth, shadow evidence, or
     promotion state.
4. Statistical review fixes
   - Falsification reuses the preregistered held-out paired sign-flip p-value,
     while preserving independent positive-mean, degeneracy, zero-null, and
     null-ratio guards.
   - Exact fit-evidence behavioral aliases are canonicalized before held-out
     replay and BH. Missing/zero/non-fit probes fail open and exclusions are
     audited.
   - Production `max_stressed_cost_to_risk_ratio` remains `0.30`; `0.60` exists
     only as a preregistered, non-authorizing counterfactual arm.
   - No LORD alpha floor was added because that would invalidate online FDR.
5. Paper-primary/shadow epochs
   - Added an isolated append-only epoch store and CLI with one frozen primary,
     frozen siblings, separate stream/config/code/cost/risk identities, LLM
     adaptation disabled, paired outcomes, operational stop rules, zero
     promotion authority, next-epoch-only lessons, unseen-data restart, and an
     audit hash chain.
   - Runtime broker activation is intentionally unavailable until a separate
     Alpaca paper-research account/credential and separate outcome store are
     provisioned.

## Verification completed

- Full local suite: `1234` tests in `916.250s`, `OK`.
- Static compilation, shell compilation, Compose rendering, and
  `git diff --check`: passed.
- Focused paper-epoch tests: 12 passed; existing paper-performance tests: 12
  passed.
- Safe statistical changes: 41 gate/fit tests and 3 targeted factory tests
  passed.
- Python 3.14 emitted ignored SQLite finalizer `ResourceWarning`s only; this is
  cleanup debt, not a test failure.
- Repaired frozen corpus:
  `/app/runtime/research/recorded-iex-rth-derived-v2-20260824`
  - 19,415,521 rows scanned
  - 126 partitions; 125 historical markers
  - source identity
    `sha256:feeb94a979823f511b75e4ed7593d479615ea443f83782082692865fbdeda08c`
  - 4,242,376,555 verified source bytes
- Immutable preprocessing cache entry published:
  `d6d9f16461f76702b30548cb1e5b23529d1a25dd6672b71c1885894dcf5567a7`
  - normalized/validated artifact: 7,889,042,323 bytes
  - quotes: 5,833,903,351 bytes
  - replay: 153,727,820 bytes
  - bars: 114,345,510 bytes
  - 19,386,686 rows retained; 28,835 legacy option rows quarantined for
    `as_of` inversion
  - full validation completed successfully with 364,861 bars, 18,967,486
    quotes, zero options, and no errors
  - readiness remains diagnostic: 1 forward-observed session recorded and 209
    sessions remaining across the full offline + shadow protocol

## Expert-review findings reproduced

- The held-out paired sign-flip and falsification p-tests use the same matched
  deltas/null: correlation `0.999968`, identical verdicts `60/60`, maximum
  absolute p difference `0.011826`. The shared p-test is now computed once;
  independent falsification guards remain.
- On 346,772 real 15-bar windows, the extreme volatility- and volume-breakout
  settings agreed in direction `99.8711%` of the time (447 divergences). This
  is a severe behavioral alias, not a mathematical identity.
- The cost geometry is restrictive but not empty. At ratio `.30`, the minimum
  stop is 83.33 bps; 84.60% of measured ATR windows are at or below 8 bps and
  only 13.92% are geometrically admissible even at the maximum stop-ATR value.
  At `.60`, 52.08% are admissible. The measured full-session range is 150.02
  bps mean / 133.71 bps median, so a 166.67 bps 2R target is 1.11x mean / 1.25x
  median—not 1.45x. The claim that the constraint set has no interior is false.
- The current confirmation burden is approximately 600 candidate trades over
  five windows, not 450 over four; permitted hold caps span 1–390 bars.

## In progress and remaining

1. **Active recorder maintenance (running on the VM).** The compact SQLite index is
   healthy, but a deployed 30-minute catch-up window caused repeated 768 MiB
   cgroup OOM kills. The recorder is intentionally stopped while streaming
   provenance repair runs in the 10 GiB research lane as container
   `alpaca-recorder-provenance-repair-active`. When it completes,
   recreate the recorder with `ALPACA_RECORDER_FETCH_WINDOW_MINUTES=1`, confirm
   health, marker count, stable memory, and an advancing watermark.
2. **Fresh Terra cycle (running on the VM).** Container
   `alpaca-terra-fresh-3a64ffc` is running directly against the immutable
   cached validated artifact with `--diagnostic-only`, 11 hypotheses, 4
   variants, 2 workers, 5 generations, and 3 confirmatory attempts. Its report
   target is
   `/app/runtime/research/diagnostics/terra-20260824-fresh-3a64ffc-equity.json`.
   Persist a compact summary beside it after exit. Exit code `2` is the expected
   non-authorizing result, not a failure. A prior equivalent Terra workload ran
   for about seven hours, so do not terminate this fresh process merely for
   being long-running.
3. **Frozen cost counterfactual.** Reuse exactly the Terra cohort and cached
   data; compare `.30` with `.60`. Only the stressed-cost ratio may differ.
   Persist the report. Do not modify production policy from this one cycle.
4. **Paper runtime activation.** Do not activate until a distinct paper-research
   broker credential/account and outcome store exist. Then freeze a primary and
   siblings for one epoch, keep LLM adaptation off, and pair the same real-time
   stream. Operational failure may stop the epoch; paper success cannot promote.

## Confirmation checklist

Run these checks on the VM after the maintenance/experiments finish:

```sh
cd /opt/alpaca-agent-trading
git rev-parse HEAD
docker inspect --format '{{.Image}}|{{.RestartCount}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}' alpaca-recorder
docker inspect --format '{{.Image}}|{{.RestartCount}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}' alpaca-trader
docker stats --no-stream alpaca-recorder
docker exec alpaca-recorder sh -c 'find /app/runtime/research/recorded/sessions -maxdepth 1 -name "market-*.historical.json" | wc -l'
docker exec alpaca-recorder sh -c 'grep -o '"'"'"watermark"'"'"':[[:space:]]*"[^"]*"' runtime/research/recorded/.recorder-index.json'
```

Expected invariants:

- VM and GitHub/local resolve to `3a64ffcb328eba612f8a1f46e45fb3c47d1ba4fc`.
- Recorder restart count stops increasing; memory remains below 768 MiB; health
  becomes `healthy`; fetch-window environment equals `1`; marker count is
  non-zero and the watermark advances.
- Trader remains healthy and paused for `validated_edge_required`; broker/local
  positions and orders remain flat.
- Terra report says `diagnostic_only=true`, `authorizing=false`, has no proofs,
  and does not mutate the authorizing edge/shadow databases.
- Cost report says `diagnostic_only=true`, `authorizing=false`,
  `promotion_allowed=false`, and `only_changed_field` is exactly
  `risk.max_stressed_cost_to_risk_ratio`.
- A paper epoch cannot start with shared/missing identities, LLM adaptation, an
  unfrozen cohort, or a lesson from the current epoch; no paper outcome can set
  promotion authority or alpha evidence.
