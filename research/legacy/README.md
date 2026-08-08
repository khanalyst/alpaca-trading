# Legacy exploratory research

This package contains one-shot analysis programs whose reports and conclusions
are already committed.  They are retained so a rejected idea can be inspected
or replayed, but they are not active strategy, discovery, qualification, or
tournament code.  Run a retained CLI with its new module path, for example:

```bash
python -m research.legacy.edge_report --help
```

Moved programs:

- `analyse_flow.py`, `deep_edge.py`, `edge_report.py`, `fetch_flow_data.py`
- `find_edge.py`, `make_legacy_dataset.py`, `portfolio_sim.py`
- `phase1_v2_backtest.py`, `selection_study.py`, `unbiased_recheck.py`
- `validate_candidate.py`

`maker_study.py` remains at `research/maker_study.py` because its path is cited
by the complete `scalp-maker` forward contract.  `signal_lab.py` remains at
`research/signal_lab.py` as the shared helper referenced by registered research
notes and imported by the retained studies.  These are deliberate
deferrals, not active promotion paths.

The moved programs still import the active `research.edge_lab` harness.  Their
historical outputs and frozen conclusions are unchanged; only import and CLI
paths changed.
