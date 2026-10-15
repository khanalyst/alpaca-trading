# Review handoff: G2b paper epochs, trader projection, shadow entry point and cohort experiment (coordinator review)

Method: done directly by the coordinator, with targeted reading only (not line by line).

- `research/paper_epoch.py` (2,024 lines): header, class and function inventory, and a repository-wide search for callers.
- `research/paper_epoch_cli.py` (158 lines) and `paper_epoch_export.py` (345 lines): command inventory.
- `deploy/trader_projection.py` (988 lines): docstring, the quote-thinning and cost-measurement paths (found by pattern), and its callers.
- `deploy/shadow.py` (249 lines): argument parser and loop structure.
- `deploy/cohort_experiment.py` (103 lines): read in full.

## Findings

### G2b-01 The paper-epoch subsystem (2,527 lines, plus tests) is not wired into anything (MEDIUM, over-engineering or unfinished, VERIFIED)

`research/paper_epoch.py` implements a frozen "paper primary versus shadow" epoch store with:

- eight exception classes;
- seven frozen dataclasses;
- append-only SQLite triggers;
- a hash-chained audit log;
- an integrity verifier.

The only callers are its own CLI (`research/paper_epoch_cli.py`, registered as a subcommand at `research.py:50` and `:1315`) and its export module. A search of `agent/`, `deploy/`, `compose.yaml`, every shell script and all documentation finds no other reference. No service writes observations to it and no document tells an operator to use it.

It arrived in one commit (`79553f7`, 2026-08-30) and has not changed since. Its own docstring says it "is never alpha evidence and has no promotion authority".

Suggested action: delete it with its tests, or wire it into the trader and shadow services and document it. As it stands, it is maintenance cost with no output.

### G2b-02 The trader projection measures costs on the unthinned quote stream before compacting (positive, VERIFIED)

`deploy/trader_projection.py` keeps every non-quote event, plus:

- the first and last quote of every minute;
- the latest quote visible at each bar-availability cutoff (`:413-522`).

Quote-cost schedules are measured from the raw stream by `_iter_cost_rows` and `measure_quote_costs` (`:568-642`) before thinning, as the docstring promises. A failed measurement is written as an explicit `unavailable` schedule rather than replaced by a default (`:606-642`, `:811-817`).

This is a manual command. Its only reference elsewhere is a code-identity listing in `research/cost_counterfactual.py:55`.

### G2b-03 `cohort_experiment.py` registers before evaluating and refuses to overwrite (positive, INFO, VERIFIED)

- The registration (cohort, costs, provenance, no automatic winner selection) is written with mode `"x"` and fsync before any data is built (`deploy/cohort_experiment.py:26-47`).
- The output directory must be new (`exist_ok=False`).
- A 500,000-bar bound guards the context study.

It is good hygiene. It also adds one more cohort generation: `mechanism_cohort` v1, alongside v2, the refresh cohort and the cost cohort (see G2a-05).

### G2b-04 The shadow service is a thin, correct polling loop around `ShadowRunner` (INFO, VERIFIED at structure level)

`deploy/shadow.py` polls on the configured interval, records session acceptance on every poll (`:115-133`, `:192-198`) and keeps a WAL anchor connection open (`:136-165`). The substance is in `research/live_shadow.py` (groups G1a and G1b). Acceptance uses the same zero-tolerance gap rule as the trader (C2-02), so the shadow lane inherits the same session rejection.

## Summary

- The projection and the cohort registration are careful and honest.
- The paper-epoch store is a fully built but unused subsystem, and the clearest single deletion candidate in this group.
