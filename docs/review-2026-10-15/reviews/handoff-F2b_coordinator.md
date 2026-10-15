# Review handoff: F2b LLM rule proposals and fit diagnostics (coordinator review)

Method: done directly by the coordinator, with targeted reading only (not line by line).

- `research/llm_strategy.py` (2,096 lines): header, function inventory, the vehicle gate (651 to 658), grammar schema construction (1258 to 1310) and the timeout wrapper (999 to 1030).
- `research/fit_diagnostics.py` (1,972 lines): header, function inventory, the MDE and cost-hurdle helpers (1129 to 1146, 1393 to 1412).
- Cross-checked against `agent/contracts/rule.py:1476-1487` and `research/factory_core.py:509-520` and `:1330-1335`.
- Ran `tests/research/test_llm_strategy.py`: 1 failed and 20 passed.

## Findings

### F2b-01 The LLM is offered two rule grammars that can never be deployed, and the test guarding this has been left failing (MEDIUM, VERIFIED)

`RuleProposalAdapter._grammar_schema("equity")` returns `rule_spec_json_schema()` (`research/llm_strategy.py:1308`). That schema now includes `rule-strategy.v6` and `rule-strategy.v7` (`agent/contracts/rule.py:538`).

Both are declared non-executable:

- `rule_vehicle_executable` returns `False` for v6 and v7 (`agent/contracts/rule.py:1482-1483`).
- The factory replays them but marks every trade `diagnostic_only` and `authorizing: False` (`research/factory_core.py:509-520`, `:1330-1335`).

`_validate_vehicle_rule_spec` only blocks v3 to v5 for options (`research/llm_strategy.py:651-658`), so equity proposals in v6 or v7 pass. LLM calls and replay budget can therefore be spent on candidates that cannot be promoted.

The only failing test in the whole suite catches exactly this. `tests/research/test_llm_strategy.py:156` expects the equity grammar to be v1 to v5, and fails with "Items in the first set but not the second: 'rule-strategy.v6', 'rule-strategy.v7'". The guard went red when v6 and v7 were added and was neither fixed nor consciously updated.

Not traced (cross-group, F1a): whether such proposals also consume LORD++ alpha in `research/strategy_factory.py`.

Suggested fix: exclude non-executable schemas from the provider grammar in authorising lanes, or reject them in `_validate_vehicle_rule_spec`. Then update the test to state the intended policy.

### F2b-02 The factory's own power diagnostic implies a sample the system will not reach (INFO, VERIFIED by arithmetic)

`_mde` reports a clustered minimum detectable effect for a target of 0.05R per trade at alpha 0.05 (`research/fit_diagnostics.py:1393-1411`).

With per-trade R dispersion of about 1.06R (bracket-like outcomes; see F3b), 80% power at that effect needs about `((1.96 + 0.84) x 1.06 / 0.05)^2`, which is about 3,500 independent trades. Session clustering only raises that number.

The protocol floors are 100 to 150 trades over 30 sessions, and the repository already computes the diagnostic that shows them to be about 25 times too small for the target effect it chose.

### F2b-03 The LLM boundary is data-minimising and bounded (positive, VERIFIED)

- The provider receives aggregate fit diagnostics, never market rows (`research/llm_strategy.py:591-600`).
- Fit diagnostics "never ... inspect held-out/sealed rows" (`research/fit_diagnostics.py:1-7`).
- Responses are size-capped, must be strict JSON, and are validated against the grammar (`:846-956`).
- Provider calls run in a daemon thread with a hard wait bound (`:999-1030`), so a hung SDK cannot keep the research job alive. The thread itself keeps running after the timeout, but it holds no lock.

### F2b-04 Fit diagnostics collapse behaviourally identical parameter sets before the expensive replay (positive, INFO)

`collapse_behavior_aliases` and `fit_behavior_fingerprint` (`research/fit_diagnostics.py:1737-1907`) deduplicate specifications that produce identical fit behaviour. This avoids charging the multiple-testing budget twice for the same rule.

## Summary

- The LLM integration is well isolated.
- It currently invites proposals in grammars that cannot be deployed, and the red test that says so has been tolerated.
- The fit diagnostics already contain the power calculation that explains why the factory cannot succeed at its floors.
