# Review handoff: F3b edge ledger, proof mixin and store (coordinator review)

Method: done directly by the coordinator, with targeted reading only (not line by line).

- `research/edge_ledger.py` (994 lines): function inventory; read in full the drift constants, champion selection, held-out reference, SPRT, paper history and paper guard (lines 24 to 35 and 493 to 760), and paper performance (905 to 960).
- `research/edge_ledger_proof.py` (1,254 lines): function inventory, plus eligibility and live-shadow authorisation (882 to 940).
- `research/edge_ledger_store.py` (328 lines): constants.
- The drift test and the rolling warning were checked by Monte Carlo simulation, using the ledger's exact formulas. The script is `sims/sprt_sim.py` in the review scratchpad, copied to `docs/review-2026-10-15/research/scripts/sprt_sim.py`.

## Findings

### F3b-01 The held-out drift SPRT is mathematically correct (positive, VERIFIED by derivation and simulation)

For normally distributed R with reference mean `mu0` and standard deviation `sd`, the per-trade log-likelihood ratio of H1 (mean 0) against H0 (mean `mu0`) is `(mu0 / sd^2) * (mu0 / 2 - x)`. That is exactly `scale * (mean0 / 2.0 - value)` (`research/edge_ledger.py:620-624`).

By Wald's inequality, the probability that a healthy edge ever crosses the 4.0-nat boundary is at most `exp(-4) = 1.8%`.

Simulation, using bracket-like R outcomes (stop -1R, target +1.5R, 30% time exits; per-trade standard deviation 1.06R) with a reference mean of 0.25R and a standard deviation of 1.1R:

| true edge per trade | retired within 1,000 trades | median trades to retirement |
| --- | --- | --- |
| +0.25R (healthy) | 1.5% | n/a |
| +0.10R | 79% | 267 |
| 0 | 99.9% | 125 |
| -0.10R | 100% | 77 |

The only lifecycle-authoritative guard is this SPRT (`:705-722`).

### F3b-02 The drift test never resets, so an edge that dies after a good run is detected much later (MEDIUM, VERIFIED by simulation)

The statistic is a cumulative sum over every post-proof outcome (`:621-624`). Under H0 it drifts negative by about `mu0^2 / (2 sd^2)` per trade, so a long healthy history builds a buffer that a later decay must first erase.

| good trades before the edge dies | median trades after death until retirement |
| --- | --- |
| 0 | 129 |
| 100 | 234 |
| 300 | 429 |

Suggested fix: use Page's CUSUM form, `S_n = max(0, S_(n-1) + llr_n)`, with the same threshold. Its worst-case detection delay is optimal for a change at an unknown time (Lorden 1971). Calibrate the threshold for the target average run length under H0.

### F3b-03 The advisory rolling-R warning is almost always on for healthy edges (LOW, VERIFIED by simulation)

The warning fires when the last 20 outcomes sum to -2R or less (`:716-719`, `:917-946`). It is labelled `warning_only` and `authoritative: False`, so it changes no lifecycle state.

In 250 trades it fires at least once for:

- 94.8% of healthy +0.25R edges;
- 99.9% of +0.10R edges;
- 100% of dead edges.

The sum of 20 trades has a standard deviation of about 4.7R, so a -2R floor sits well inside normal noise. An operator learns to ignore it.

Suggested fix: remove it, or derive the floor from the reference distribution (for example, the 1% quantile of a 20-trade sum).

### F3b-04 Champion selection and live authorisation are conservative and correctly scoped (positive, VERIFIED)

A candidate must be `validated`, with a re-verified passing shadow gate on the current replay-engine epoch (`REPLAY_ENGINE_EPOCH = 7`, `research/edge_ledger_store.py:68`).

The shadow-ingest source must also declare `independent_confirmatory`, disjoint selection and confirmatory session lists, and unique, non-empty sessions (`research/edge_ledger_proof.py:906-940`).

Candidates are ranked by:

1. the held-out lower confidence bound, which must be greater than 0;
2. then drawdown;
3. then sample size.

Confidence is taken as `1 - q` from the global q-value, at a minimum of 0.95 (`research/edge_ledger.py:493-559`). Ranking on the lower bound rather than the point estimate is the right defence against the winner's curse.

Given D2-04 (the gate chain has almost no power for realistic edges), this path is correct but practically unreachable.

### F3b-05 One 519-line validation method (LOW, over-engineering, VERIFIED)

- `_gate_envelope_error` runs from `research/edge_ledger_proof.py:312` to `:830` as a single method.
- The module also defines nine façade wrappers (`:47-87`) that forward calls to the store and gates modules to avoid import cycles.

Both are signs that the proof format has grown by accretion. Each new rule is another branch in one function.

## Summary

- The ledger's arithmetic and authorisation are correct and conservative.
- The design gaps are about monitoring rather than correctness. The authoritative SPRT does not reset, and the visible warning is miscalibrated.
- Both matter only after something reaches paper deployment, which the gate chain currently prevents.
