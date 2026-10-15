# Review handoff: F1a strategy factory (coordinator review)

Method: done directly by the coordinator, with targeted reading only (not line by line).

- `research/strategy_factory.py` (6,003 lines): header, and a size inventory of all 66 top-level definitions, taken from the AST.
- Read in full: the selection-to-qualification hand-off (4985 to 5030) and the qualification and check assembly (5060 to 5110).
- Cross-checked the floors and split in `research/gates.py:114-122` and `:889-907`, and the gate floors in `research/edge_discovery_core.py:1700-1720`.

## Findings

### F1a-01 One 1,983-line function runs the factory (MEDIUM, over-engineering, VERIFIED)

`_run_factory` covers `research/strategy_factory.py:4007-5989`, a third of the module in a single function. The next largest are:

- `_gate`: 366 lines;
- `_run_diagnostic_factory`: 334 lines;
- `_tuned_variants`: 244 lines;
- `_signal_quality_screen_record`: 244 lines.

No unit test can exercise a stage of `_run_factory` in isolation. The 2,359-test suite instead tests through large fixtures, which is why the factory shard alone takes about 32 minutes (H2b-03).

### F1a-02 Selection and confirmation are separated correctly (positive, VERIFIED)

- A variant can be selected only if it passes the development checks and is significant under both the family and the global BY correction on held-out data (`:5001-5006`).
- Among those, the factory ranks by adjusted p-value, then held-out lower bound, then held-out net P&L (`:4989-4999`).
- Only the top variant opens a sealed qualification window. The ledger claim is committed before the window is released, so a crash cannot make that window look unused (`:5017-5030`).
- Cumulative (LORD++) alpha is "deliberately not spent on historical or offline forward evidence"; the one online test is deferred to live shadow (`:4985-4988`, `:5011-5013`).

So the F2b-01 question is answered for the factory lane: non-executable v6/v7 proposals do not spend LORD++ alpha here. They can still add tests to the BY families. That was not traced.

### F1a-03 The earliest date any factory proof could pass is about mid-January 2027 (MEDIUM, VERIFIED by arithmetic from code)

`chronological_split` divides whole sessions 70/30 (`research/gates.py:889-907`; the gate calls it with `fit_fraction=.7`, `research/edge_discovery_core.py:1689`).

The fit, held-out and overall partitions must each meet the floors of 100 trades and 30 sessions (`:1708-1719`; `research/gates.py:114-116`). The held-out 30% must therefore contain at least 30 sessions, which needs at least 100 recorded sessions in total.

The system learns only from its own IEX recorder (H1b-05), whose corpus starts in late August 2026 (the audit's in-sample window began on 26 August). One hundred accepted sessions arrive in about mid-January 2027, and only if every session is accepted, which C2-02 currently prevents.

The documentation's 210-session readiness arithmetic (I1-03) puts it later still.

## Summary

- The factory's statistical protocol is sound in design.
- Its implementation is concentrated in one 2,000-line function.
- Its floors, applied to a self-recorded corpus, put the first possible proof months away. Even then, the power arithmetic in F2b-02 says the floors are too small to detect plausible effects.
