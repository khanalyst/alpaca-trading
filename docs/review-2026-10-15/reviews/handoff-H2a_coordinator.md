# Review handoff: H2a research orchestration (coordinator review)

Method: done directly by the coordinator.

- `deploy/research-cycle.sh`: header, main flow (lines 1 to 60 and 1860 to 1940), and every function, `set` toggle and lock found by pattern.
- `deploy/scheduler.py`: missed-run logic and run loop (lines 88 to 100 and 186 to 215).
- `deploy/research_budget.py` and `deploy/test_suite.py`: read in full.
- `deploy/research_census.py` and `deploy/scheduler_output.py`: function-level structure only.

## Findings

### H2a-01 A scheduler restart during the session starts a full research run next to the live trader (MEDIUM, VERIFIED)

`deploy/scheduler.py:93-95`:

```python
if today >= scheduled and last_run_date != today.date().isoformat():
    return today
```

After 03:00 UTC, a scheduler that has not yet run today runs immediately. A deploy, VM reboot or container restart during US hours therefore launches a research cycle with a 10 GB, 1.5-CPU budget and a 4-hour timeout (`compose.yaml:273-274`, `.env.example` `ALPACA_RESEARCH_TIMEOUT_SECONDS=14400`) while the trader runs.

That matters because the trader acts only on the newest bar if it closed less than 30 seconds ago (B-12). CPU contention that delays a cycle silently drops signals.

Suggested fix: never start a missed run between 09:00 and 16:30 New York. Defer it to the next off-hours slot, or reduce its priority with `nice`/`ionice` and a CPU share.

### H2a-02 1,940 lines of bash orchestrating Python, with 24 embedded Python heredocs and 21 `set +e` toggles (MEDIUM, VERIFIED by count)

- Even trivial steps (a UUID, a timestamp, file locks) shell out to Python heredocs (`deploy/research-cycle.sh:30-37`, `59-230`).
- Error handling flips between `set -e` and `set +e` 21 times. The script is careful (strict mode at line 2, `trap on_exit EXIT` at line 419, flock leases, explicit terminal outcomes at lines 1909-1940), but every new step must re-implement status, lease and progress plumbing.

Suggested fix: move the orchestration into one Python entry point with a step list. Keep bash as a two-line wrapper.

### H2a-03 Terminal outcomes are honest and well separated (positive, VERIFIED)

The cycle distinguishes:

- `completed` (with proof);
- `llm_provider_failure`;
- `search_exhausted`;
- `no_data` (a corpus that could not be priced);
- `completed_no_edge`.

Each has an explanatory message (`deploy/research-cycle.sh:1909-1940`). This is good practice and should survive any simplification.

### H2a-04 `research_budget.py` is a sound guard (positive, VERIFIED)

It refuses symlinks and non-regular inputs and checks the input bytes against both a maximum and the free temporary space with an 8x expansion estimate (`deploy/research_budget.py:14-31`).

## Summary

- Research can start during trading hours after any restart (H2a-01).
- The bash orchestration is careful but over-engineered and brittle to change (H2a-02).
- The outcome classification and the budget guard are good.
