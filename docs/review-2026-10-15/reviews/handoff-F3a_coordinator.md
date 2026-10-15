# Review handoff: F3a edge discovery core, edge lab, edge identity and research CLI (coordinator review)

Method: done directly by the coordinator, with targeted reading only (not line by line).

- `research/edge_discovery_core.py` (2,059 lines): function inventory, plus the null-control header and sampling loop (880 to 1010) and the discovery gate's structure (1637 to 1830, by pattern).
- `research/edge_lab.py` (918 lines) and `research/edge_identity.py` (145 lines): headers and imports.
- `research.py` (1,346 lines): the complete parser tree (1110 to 1346).

## Findings

### F3a-01 The randomised-entry null takes one random entry per opportunity, roughly doubling the variance of the comparison (MEDIUM, VERIFIED)

`null_control_account` draws a single entry with `rng.choice(admissible)` for each symbol-session (`research/edge_discovery_core.py:969`). The held-out candidate is then tested against that one draw with `matched_cluster_test` (`:1812-1815`).

When candidate and null outcomes are close to independent with similar variance, `Var(candidate - null)` is about `2 sigma^2`. Averaging K random entries per opportunity cuts it to `sigma^2 (1 + 1/K)`:

| K draws | variance relative to `sigma^2` |
| --- | --- |
| 1 | 2.0 |
| 10 | 1.1 |

So K = 1 needs about 1.8 times as many trades for the same t-statistic, on top of the other conjunctive hurdles (D2-04). The shared session move correlates the two outcomes a little, which shrinks the factor slightly, but a random entry time keeps that correlation modest.

Suggested fix: average 20 to 50 seeded random entries per opportunity, which is cheap because exits are replayed bar by bar.

### F3a-02 The null design itself is sound (positive, VERIFIED)

The null keeps the candidate's symbol, session, direction, stop geometry, exit rules and costs. Only the entry bar is random, chosen from admissible entries (`_null_admissible_entry_indices`, `:758`). It is seeded deterministically from the account, the specification and the session set (`:926-928`).

This asks the right question: did the timing beat chance, rather than merely beating the parent specification?

### F3a-03 Test-seam architecture shapes production code (LOW, over-engineering, VERIFIED)

- **Proxy wrappers.** `edge_discovery_core.py` defines about 20 proxy wrappers (`:60-156`) that resolve dependencies through the `edge_lab` facade at call time, "preserving the historical patch seams". This means production modules are structured around where tests monkeypatch.
- **Long single functions.**
  - `null_control_account` is 664 lines (`:880-1544`).
  - `_discover_gate` is 333 lines (`:1637-1970`).
- **Doubled CLI surface.** Every `edge` and `factory` subcommand is registered twice, in nested and flat forms ("so cron jobs can stay terse", `research.py:1289-1306`). That makes 24 parser entries for 12 commands.

### F3a-04 The candidate identity binds risk and cost configuration (INFO, VERIFIED)

`candidate_assumptions` hashes the `broker`, `data`, `session`, `universe`, `strategy`, `risk`, `execution`, `costs` and `llm` sections (`research/edge_identity.py:14-17`). Changing a sizing parameter therefore orphans every existing proof, even though R-denominated evidence does not depend on size.

This is strict by design. It will make the fix for B-06 (raising risk per trade) also reset all research state.

### F3a-05 The CLI and the preregistration use different alpha conventions (INFO, VERIFIED)

- `factory run` and `edge ingest-shadow` default to `--alpha 0.05` (`research.py:1209`, `:1153`), allocated through LORD++.
- The preregistered hypotheses use 0.025.

These are different test families, so this is not an error. But an operator comparing outputs needs to know which alpha governs which verdict.

## Summary

- The discovery logic asks the right questions (chance null, matched baseline, held-out split, placebo falsification).
- It spends statistical power carelessly: one null draw per opportunity, inside a conjunction of hurdles.
- Its structure (facade proxies, 300 to 600-line functions, duplicated CLI) carries a high maintenance cost.
