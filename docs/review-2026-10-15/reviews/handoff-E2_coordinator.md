# Review handoff: E2 market data normalisation, source validation, cost tools and path telemetry (coordinator review)

Method: done directly by the coordinator, with targeted reading only (not line by line).

- `research/market_data.py` (791 lines): read in full the identity defaults, availability rules and bar prefix (187 to 330, 380 to 462 and 656 to 697).
- `deploy/backfill.py`: the `as_of` stamping lines.
- `research/cost_counterfactual.py` (1,545 lines): function inventory, constants and code-bundle identity (40 to 58, 631 to 657).
- `research/cost_rerun.py`, `source_validation.py`, `market_context_study.py` and `path_telemetry.py`: headers and callers only.

## Findings

### E2-01 A bar without `as_of` or `observed_at` would be visible at the start of its minute (LOW, latent, VERIFIED)

`_identity` defaults both `observed_at` and `as_of` to the event timestamp (`research/market_data.py:189-195`). For a one-minute bar, the event timestamp is the start of the minute, so `record_available_at` would make the bar's high, low and close visible 60 seconds early.

Real data is safe on two counts:

- The recorder and backfill stamp `as_of` at the completed minute boundary, which is the start plus 60 seconds (`deploy/backfill.py:22`, `:253`).
- `replay_bar_prefix` also requires `row.end <= decision_timestamp` (`research/market_data.py:451-452`).

The exposure is limited to legacy or hand-made fixtures, and to any future caller that checks only `replay_record_is_available`.

Suggested fix: for bars, default `as_of` to `timestamp + interval_seconds`.

### E2-02 The repository's own cost study already found the cost-to-stop ratio pinned by the stop floor (corroborating, VERIFIED)

The `research/cost_rerun.py` docstring records "a near-constant 0.16-0.18R execution drag across every family". It explains why: "a fixed round trip divided by a stop the risk gate pins near a fixed width is the same number whatever the strategy does".

This is the mechanism behind A-02 (ATR is inert) and B-05 (the stress floor equals the stop floor), found independently by the project.

### E2-03 Source provenance validation is shared and fail-closed (positive, VERIFIED by usage)

`research/source_validation.py` is imported by nine write boundaries, including:

- `ibr.py`
- `strategy_factory.py`
- `preregistered.py`
- `edge_lab.py`
- `cost_rerun.py`
- `research.py`

Each of these validates feed and provider identity before it writes evidence.

### E2-04 Path telemetry censors gaps instead of rolling forward (positive, INFO)

`research/path_telemetry.py` measures excursions only over contiguous, completed bars. It reports a missing bar as censoring, and takes exit reasons from `agent.contracts.rule` rather than re-deriving them.

### E2-05 A one-off experiment module hosts a shared utility (LOW, over-engineering, VERIFIED)

`research/cost_counterfactual.py` (1,545 lines) evaluates one threshold change (`risk.max_stressed_cost_to_risk_ratio`). It also owns `_code_bundle_files` and `_code_bundle_hash`, private names imported by:

- `trader_research.py`
- `diagnostic_suite.py`
- `cost_rerun.py`

The bundle hashes every `.py` file under `agent/` and `research/`, plus six extras (`:54-57`, `:631-655`). Any edit anywhere therefore invalidates in-flight research ("code changed during research", `research/trader_research.py:657`). This is the same whole-tree identity that breaks paper-trial resumption (C1-03).

## Summary

- Market data timing is handled carefully on the real data paths: completed-minute `as_of`, a decision-time bar prefix and explicit backfill labelling.
- The latent default is a hazard only for future callers.
- The cost tooling corroborates the central structural finding: the stop floor fixes costs at about 0.17R per trade whatever the signal.
