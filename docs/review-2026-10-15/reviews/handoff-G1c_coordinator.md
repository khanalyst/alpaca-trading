# Review handoff: G1c live-shadow ingestion (coordinator review)

Method: done directly by the coordinator, with targeted reading only (not line by line).

- `research/live_shadow_ingest.py` (1,473 lines): header and function inventory.
- Read in full: confirmatory iterations (60 to 72), the session split and window readiness (122 to 184), row collection and the session-continuity rule (336 to 384), and the main split, partition and readiness flow (845 to 960).
- The protocol floors in `research/gates.py:114-122`.

## Findings

### G1c-01 Shadow authorisation needs at least 60 consecutive complete sessions, and a single missing session halts it (MEDIUM, VERIFIED)

The ingest path stacks several fail-closed requirements:

1. **Two disjoint windows.** The complete chronological tail is split into a selection half and a confirmatory half (`_split_sessions`, `research/live_shadow_ingest.py:126-142`). Each half must hold at least `min_sessions` sessions, so the tail needs at least twice that.
2. **Every window meets the protocol floors.** In each window, the candidate, the baseline and the randomised null must each reach `min_trades` and `min_sessions` (`:913-960`). The protocol shadow floors are 150 trades and 30 sessions (`research/gates.py:117-118`).
3. **One gap blocks the whole tail.** If any session lacks parity-matched rows, the tail is blocked (`:336-350`, `:872-875`, "deliberately stricter than selecting only the currently matching subset"). Any calendar session missing from the shadow store also blocks it (`_session_continuity`, `:353-381`) "until the operator repairs/replays the source".

The minimum is therefore 60 consecutive, fully replayed sessions, with at least 300 candidate trades plus matching baseline and null coverage. That is about three months of uninterrupted operation, before multiple-testing control.

Combined with the zero-tolerance session acceptance (C2-02), the 100 versus 150 trade floor mismatch for the factory shadow lane (D2-02) and LORD++ starvation (D1-08), this is a further reason the deployment path has had no throughput (D2-04).

Suggested fix: allow a session to be excluded explicitly, with a recorded reason, instead of blocking the whole tail. Size the floors from a power calculation for the effect sizes that are actually plausible (2 to 10 bps).

### G1c-02 The selection and confirmation protocol is clean (positive, VERIFIED)

- The split is deterministic and is made before any statistic is computed. Any odd extra session goes to the confirmatory half, so the boundary stays at the newest consumed session (`:126-142`).
- Batch BY q-values (family and global) choose the candidate.
- Only the confirmatory window's raw p-value is submitted to the active LORD++ scope (module docstring, `:1-11`).

This is a correct separation of adaptive selection from confirmation, and is rarer in practice than it should be.

### G1c-03 Permutation iterations grow inversely with the online allocation (INFO, VERIFIED)

`_confirmatory_iterations` returns `max(20,000, ceil(batch_size / allocated_alpha))` (`:64-70`). This guarantees that the p-value resolution is finer than the allocation. But as LORD++ allocations decay (D1-08), the cost grows: an allocation of 1e-5 for a batch of 10 needs 1,000,000 permutation draws for each confirmatory gate.

## Summary

- The ingestion boundary is statistically careful and honest.
- The fail-closed rules compound, so the shadow-to-validated path needs a long, gap-free run that the current data plumbing has not produced.
