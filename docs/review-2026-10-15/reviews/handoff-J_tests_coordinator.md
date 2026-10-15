# Review handoff: J tests (coordinator review)

Method: these groups could not be reviewed by subagents within the usage limits, so the coordinator reviewed them directly. Three passes were used:

1. A complete AST inventory of every test function in all 162 test modules (`tools/test_inventory.py`, output not committed), counting tests, assertion types, hash literals, documentation references, wall-clock, network, subprocess and repo-path usage.
2. Keyword coverage of the trading-critical scenarios raised by the code reviewers.
3. Targeted reading of the trading-critical runtime modules (`tests/test_risk.py`, `tests/test_broker_protection.py`, `tests/test_alpaca_runtime.py`, `tests/test_exit_contract.py`), including every test name in them, plus the specific tests cited below.

This is not a line-by-line reading of all 62,306 lines; the inventory covers every line mechanically.

## Facts

- **Size:** 162 modules, 2,354 test functions (2,359 discovered by unittest), 62,306 lines and 9,484 assertions. By assertion type: 5,164 `assertEqual`, 1,674 `assertTrue/False`, 682 `assertIn`, 581 `assertRaises` and 257 `assertAlmostEqual`.
- **Full-suite result on the current head** (Python 3.13, locked requirements): 2,358 pass and 1 fails, `tests/research/test_llm_strategy.py:156`. The equity provider grammar now offers rule-strategy v6 and v7 (code changed, test stale). Note that v6 and v7 cannot execute at runtime (`agent/market_entry_risk.py:570-575`), so the code is the more questionable side (finding A-13).
- **Wall time per CI shard:** runtime 487 s (994 tests), research 1,756 s (1,298), edge 2,301 s (61 tests, 38 s each) and factory 1,915 s (6 tests, 5.3 minutes each). Shards run in parallel in CI, so the edge shard sets the wall time at about 38 minutes against the 90-minute CI timeout.
- **Hidden dependencies:**
  - No network calls.
  - 16 modules spawn subprocesses.
  - 42 wall-clock references.
  - `research/edge_ledger_store.py:20-22` defaults the ledger to `<repo>/runtime/research/edge_lab.sqlite3` unless `ALPACA_EDGE_DB` is set, and tests that build a default ledger create that file in the working tree (observed: an empty schema-only database with 1 `ledger_meta` row). LOW, because `runtime/` is git-ignored.
- **Hash pins:** only 8 SHA-256 literals in 6 modules (for example the preregistration manifest hashes) and 22 16-hex variant ids. Hash pinning is not pervasive.

## Trading-critical behaviour with NO test

| scenario | evidence |
| --- | --- |
| A flatten (15:50, fail-closed, run exit, startup, watchdog) books its close: journal close row, outcome, empty `active_trades` (C1-01) | no test matches; `tests/test_broker_protection.py:821-856` asserts only the call order (`kinds.index("cancel") == 0`, close before `cancel_all`) |
| Two exposures on one symbol on different days exiting by the same reason (client id reuse, B-01) | `tests/test_runtime_safety.py:1057-1069` asserts the id is deterministic; nothing exercises a second exposure |
| One rejected entry must not flatten other positions (B-04) | none |
| HTTP 429, rate limiting, timeouts on Alpaca calls (B-10) | none |
| Short sale on a non-easy-to-borrow ETF | none (`easy_to_borrow` is not referenced anywhere in tests) |
| DST transitions in the runtime | only 2 research modules reference DST |
| Halts and LULD pauses | only 2 research modules mention halts |
| Paused trader still force-flats before the close (C2-01) | none |
| Session acceptance on a realistic IEX day with an empty minute (C2-02) | none |
| A preregistered look on a partial or in-progress session (D1-04) | none |

## Tests that pin questionable behaviour

- `tests/test_exit_contract.py` (`a_gap_up_entry_does_not_move_the_protective_levels`, `a_gap_down_entry_does_not_move_the_protective_levels`) and `tests/test_broker_protection.py` (`executable_quote_drives_geometry_while_signal_entry_drives_slippage`) pin the signal-close-anchored bracket while the entry is re-priced (B-02).
- `tests/test_broker_protection.py:821-856` pins cancelling protective legs before the close is accepted (C1-02).
- `tests/test_risk.py:132-147` (`shipped_notional_cap_delivery_curve_is_explicit`) pins the delivery curve of the 25% notional cap:
  - a 47 bps stop delivers 23.5% of the configured risk budget;
  - 83.34 bps delivers 41.67%;
  - only a 200 bps stop delivers 100%.

  At the operative 30 bps stop the ratio is 15%, that is 0.075% of equity (B-06). The behaviour is deliberate and known, but it means the configured `risk_per_trade_pct` of 0.5% is never delivered.
- `tests/test_risk.py` (`25bp_entry_notional_stress_ratio_is_vetoed_at_30_percent`) pins the stress arithmetic that makes 30 bps stops the binding geometry (B-05).
- `tests/test_runtime_safety.py:1057-1069` pins deterministic close and flatten ids (B-01, B-17).

## Positive notes

- The exit contract tests (57 tests) cover stop-first ties, gaps, entry-bar ranges, deadline precedence, the breakeven and trailing next-bar rule, and replay/runtime agreement on share counts. This is the strongest part of the suite.
- `tests/research/test_factory_end_to_end.py` shows a synthetic strong edge passing the full gate chain to "champion", and that worse costs take the pass away, so the pipeline is reachable by construction. With realistic edge sizes it is not (D2-04).
- No test makes network calls. The suite is deterministic apart from wall-clock use.

## Over-engineering

- About 40% of assertions are equality checks on structured receipts, provenance and identity fields, not on prices, P&L or orders (estimated from the assertion mix and module names; not counted line by line).
- The six factory end-to-end tests take 32 minutes and the 61 edge-discovery tests take 38 minutes, which discourages running the suite before a deploy.

## Summary

- One failing test on the head, `tests/research/test_llm_strategy.py:156`; the code side is questionable (v6 and v7 are offered to equity proposals but cannot execute).
- The most dangerous runtime paths found by the code review (flatten booking, client-id reuse across days, rejection-triggered flatten, 429 and timeouts, ETB) have no tests, and several tests pin the questionable behaviour.
- Tests write an empty edge ledger into the source tree through a repo-relative default path.
