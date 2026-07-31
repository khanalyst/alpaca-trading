# Work-in-progress handoff — 2026-07-31

Branch: `codex/platform-correctness-docker`

Base commit: `94d10e1`

This is an explicit checkpoint, not a deployment-ready release. Work stopped
during the final execution-correctness audit at the user's request.

## Implemented in this checkpoint

- Durable strategy experiment assignments now have a draining phase, immutable
  retry lineage, fresh equal-balance baseline/candidate accounts, and strict
  current-provenance/family-corrected promotion evidence.
- The seven strategy lanes run from one recorded market snapshot; each active
  assignment locks an exact baseline/candidate configuration. Variant work
  inside a lane can use configured worker threads while durable writes remain
  serialized.
- Paper entries use observed OKX order-book depth, persist fill evidence, model
  passive maker orders as pending/full-or-unfilled, use observed 1-minute bars
  for exits, refuse stale persisted marks as timeout fills, and exclude invalid
  outcomes from inference.
- Live order submission uses client order IDs and read-only recovery after an
  ambiguous response; it does not blindly resubmit. Maker-first crossing is
  blocked while the maker order state is ambiguous.
- Runtime recorder output separates funding forecasts from realized funding
  history. Forecast revisions retain observation and settlement timestamps.
- Historical download snapshots are exclusive, immutable, fully manifested,
  and hash/row/range validated before tournament use. Non-finite report values
  are serialized as JSON `null`.
- Legacy VM export parsing is read-only and horizon-censored. The supplied VM
  files were not modified. Its 3,520 historical shadow decisions are unresolved
  under the required horizons rather than being assigned fabricated outcomes.
- Docker/Compose deployment, non-root services, health checks, scheduler,
  dashboard, persistent volumes, external-backup override, CI, and operator
  documentation were added.
- The runtime cycle is 60 seconds for marks/reconciliation and 300 seconds for
  LLM decisions.

## Validation completed before this checkpoint

- Focused integration set: 124 tests passed.
- Experiment lifecycle suite passed in its implementation lane.
- VM export/provenance/recorder suite: 79 passed, 1 skipped.
- Docker/deployment/documentation suite: 67 passed.
- Compose configuration renders successfully.
- `git diff --check` passes.
- Docker image build was not run locally because no Docker daemon was available;
  CI is configured to build it.

The latest complete suite run executed 1,017 tests and exposed four integration
fixture/documentation-index failures. Those checkpoint-only fixture problems
were corrected immediately afterward and the affected 141-test focused set
passed. A complete-suite rerun after that final fixture correction is still
pending, so this branch does not claim a clean complete suite.

## Known unfinished correctness work

These items were found during the interrupted final audit and must be resolved
before treating paper results as reliable or deploying live:

1. `agent/market.py::_funding_history_context` still reads the unified
   `fundingRate` field as a realized settlement. Align it with
   `research/record_flow.py`: accept only OKX `realizedRate` as realized money.
2. `agent/shadow.py::_execution_exit` can currently accept a valid timeout when
   the execution-bar list is empty. Require continuous one-minute coverage from
   the last processed bar through the exit observation; otherwise keep the
   position unresolved or close it as invalid evidence.
3. Recheck order-book and ticker timestamps at evaluation/exit time, not only
   when each API response is acquired. Sequential symbol collection can age an
   early book before the common evaluation starts.
4. A maker fill bar is skipped for stop/target evaluation. A stop crossed in
   the same later bar that proves the maker fill must be handled
   conservatively; ambiguous same-bar paths must not become optimistic trades.
5. Timeout exits use an executable directional bid/ask and paper PnL also
   subtracts half a spread. Remove that double charge or use a non-executable
   midpoint consistently with the documented spread model.
6. Depth VWAP changes actual filled notional, but stored `risk_usd` is still
   based on pre-fill sizing. Rebase risk/margin attribution to the exact fill
   without exceeding the original risk cap.
7. Strategy lanes are logically parallel on the same timestamp/snapshot, but
   `StrategyShadowCoordinator` currently iterates lanes sequentially. Only
   configurations within a lane use worker threads. Decide whether wall-clock
   parallel strategy execution is required and, if so, add it without allowing
   concurrent SQLite writes.
8. Add focused regressions for the six execution cases above, then rerun the
   complete test suite, compilation, Compose validation, and Docker build.
9. Complete the interrupted independent adversarial audit of execution,
   inference validity, docs, and deployment claims.
10. The supplied VM history is a legacy, non-manifested export and is correctly
    refused by the new tournament gate. A fresh immutable OKX snapshot must be
    downloaded (or a separately provenance-preserving import tool designed)
    before running a representative tournament under the new rules.

## Recommended exact continuation

Start at item 1 above, add one regression per behavior before changing code,
run `python -m unittest discover -v`, then build and start Compose on a machine
with Docker. Do not promote or reuse legacy findings as current edge evidence.
