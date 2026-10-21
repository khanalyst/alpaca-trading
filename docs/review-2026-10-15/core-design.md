# Core design: the 10,000 to 15,000 line system

Status: a design and migration plan, not yet built. It follows Path A, Step 4, of the [review](README.md#11-what-to-do-three-paths). No existing module is deleted by this document; each stage below is a separate change with its own go-ahead.

## 1. What the core must do, and what it must not

**Must do:**

1. Run one strategy at a time through one event loop that is shared by backtest, paper and live. Parity then holds by construction, rather than through a shadow runner that tries to prove it afterwards.
2. Keep the safety contract on every path:
   - flat by the close;
   - every exposure protected at the broker;
   - every exit booked;
   - no liquidation on a single transient error.
3. Price research with honest per-symbol costs and an honest fill model: limit orders can fail to fill, stops can gap.
4. Make the configuration self-consistent: the risk that is configured must be the risk that is delivered.

**Will not do** (each was built and produced no edge; see the review):

- autonomous strategy search or LLM proposals;
- options;
- several rule-grammar versions;
- cohort lineages;
- online-FDR ledgers over dozens of arms;
- paper epochs;
- a shadow runner.

## 2. Event flow

```
historical SIP files (backtest)  or  Alpaca WebSocket bars and quotes (paper, live)
        |
        v
   events.Bar / events.Quote   (one time convention: a bar is available at start + interval)
        |
        v
   strategy.on_bar(state, bar) -> intents      (first signal per symbol-session, enforced once)
        |
        v
   risk.size(intent, portfolio) -> order or refusal (with a reason)
        |
        v
   broker: sim.SimBroker (backtest)  or  alpaca.AlpacaBroker (paper, live)
        |
        v
   fills -> portfolio (journal, outcomes) <- reconcile (broker is the truth on restart)
        |
        v
   engine safety: force flat at the close; transient-failure policy; protection re-arm
```

The backtest and live paths differ only in the source of events and in the broker adapter. Strategy, risk, exits, costs and journal are the same objects.

## 3. Package layout and line budgets

| module | responsibility | budget | source |
| --- | --- | --- | --- |
| `core/events.py` | Bar, Quote, Fill, Intent, OrderRequest dataclasses and the availability rule | 150 | new, from `research/market_data.py` conventions |
| `core/calendar.py` | Alpaca calendar, early closes, DST via `zoneinfo`; force-flat time and entry cut-off derived from the actual close | 150 | trim of `agent/alpaca_session.py` and `agent/market.py` |
| `core/strategy.py` | the Strategy protocol, and the first-signal-per-symbol-session rule enforced once for every strategy | 150 | new |
| `core/strategies/<name>.py` | one file per live hypothesis | about 200 each | for example the liquid VWAP reversion in `studies/liquid_reversion/strategy.py` |
| `core/exits.py` | stop, target, max hold, thesis deadline, breakeven and trailing transitions, stop-first ties, gap fills | 450 | **kept verbatim** from `agent/contracts/rule.py` (the exit functions, about 325 lines) |
| `core/risk.py` | size from stop distance with a notional cap, gross cap and daily loss stop; refuses a configuration whose risk cannot be delivered | 300 | trim of `agent/risk.py` (867) |
| `core/costs.py` | per-symbol round trip: one tick or the measured spread, slippage, SEC fee on sells, FINRA TAF | 150 | `studies/liquid_reversion/costs.py` |
| `core/geometry.py` | broker-valid price ticks and stop and target rounding | 180 | **kept** `agent/contracts/risk_geometry.py` |
| `core/portfolio.py` | positions, P&L attribution, SQLite journal of orders, fills and outcomes | 400 | trim of `agent/state.py`, `agent/journal.py` |
| `core/reconcile.py` | broker-to-local reconciliation. Closes are booked from fills bound to the entry order; legs are refreshed; open broker exposure is re-adopted on restart | 450 | replaces the 3,171-line `agent/execution_lifecycle.py` |
| `core/broker/sim.py` | simulated broker. Market, limit and stop orders on bars; limit fills only on trade-through; stops gap at the open; latency of one bar | 400 | new, consistent with `core/exits.py` |
| `core/broker/alpaca.py` | REST trading: idempotent client ids unique per exposure, HTTP timeouts, 429 back-off, submit-failure classification, bracket and stop orders | 900 | trim of `agent/alpaca_provider.py`, `alpaca_session.py`, `alpaca_domain.py` (1,549 together) |
| `core/broker/stream.py` | Alpaca WebSocket bars and quotes (IEX free tier: up to 30 symbols; SIP with the paid plan) | 250 | new; replaces REST polling and most of `deploy/recorder.py` |
| `core/engine.py` | the loop; force flat independent of strategy and pause state; transient-failure policy; protection re-arm | 500 | rewrite of `agent/engine_cycle.py`, keeping the policies fixed in this change |
| `core/config.py` | one validated config with derived consistency checks | 250 | trim of `agent/config.py` (669) |
| `core/cli.py` | `backtest`, `paper`, `live`, `flatten`, `status` | 250 | trim of `main.py`, `report.py` |
| `research/data.py` | fetch Alpaca historical SIP one-minute bars into monthly files; quality checks | 400 | `studies/liquid_reversion/fetch_sip.py`, `bars.py` |
| `research/stats.py` | session-clustered t, sign-flip permutation, Benjamini-Yekutieli, block bootstrap, power and minimum detectable effect | 400 | **kept** selected functions of `research/stats.py` (1,387) plus `studies/liquid_reversion/power.py` |
| `research/walkforward.py`, `research/report.py` | rolling train and test windows, out-of-sample report | 600 | `studies/liquid_reversion/walkforward.py`, `run.py` |
| `ops/watchdog.py` | heartbeat freshness, plus a force-flat backstop that does not depend on the trader process | 300 | trim of `deploy/watchdog.py` |
| `ops/` deploy files | Dockerfile, Compose with two services (trader, watchdog), and an off-hours research timer | 150 | **kept** hardening from `Dockerfile` and `compose.yaml` |
| **total, excluding tests** | | **about 7,500 to 9,000**, rising to 10,000 to 15,000 with three or four strategies and their studies | |

The tests should total about 4,000 lines:

- the 57 exit-contract tests, kept as they are;
- simulated-broker fill tests;
- reconciliation scenarios: the B-01, C1-01 and C1-02 cases in `tests/test_exit_safety.py`;
- engine safety scenarios: the C2-01, B-03 and B-04 cases in `tests/test_cycle_resilience.py`;
- one parity test that feeds the same bars through backtest and paper-with-simulated-broker and requires identical intents and orders.

Target CI time: under five minutes.

## 4. What stays unchanged

1. **The exit contract**: `initialize_exit_state`, `completed_bar_exit_transition`, `exit_deadline`, `hold_deadline`, `thesis_exit_deadline`, `canonical_exit_reason` and `frozen_target_reference`, with `tests/test_exit_contract.py`. This is the strongest code in the repository.
2. **Price geometry**: `agent/contracts/risk_geometry.py`, which handles broker-valid ticks and rounds stops away from entry and targets towards it.
3. **Timing conventions** from the recorder and backfill:
   - a bar is available at its completed minute boundary;
   - `observed_at` is the real receipt time;
   - late or backfilled rows are labelled;
   - feeds are never mixed.
4. **Statistics**: session-clustered inference, sign-flip tests and BY from `research/stats.py`. LORD++ (`research/factory_ledger.py:301-382`) is correct but unnecessary when hypotheses are tested one at a time with sealed forward windows.
5. **Container hardening**: a non-root user, a read-only filesystem, dropped capabilities, and a dashboard bound to localhost with confined report paths.
6. **The order-lifecycle policies fixed in this change set** (two review rounds):
   - exit client ids unique per exposure;
   - flatten closes bound to their entry, or to a position known only to the broker, and booked;
   - a partly filled bracket leg cancelled and the residual closed, never mistaken for a working close;
   - a protective stop re-armed after a failed close on every exit path, kept in any copy of the book the caller writes back, and never placed over a close that reached the broker;
   - force flat while paused, and while the calendar endpoint is down (the last good calendar is used);
   - transient read failures held before escalation, including reconciliation failures in the readiness path;
   - every failed entry looked up by client id before it is classified; an unresolved one recorded durably and adopted, with its legs, once the broker shows it.

## 5. What is archived (Git keeps the history)

| area | modules | lines |
| --- | --- | --- |
| Strategy search | `research/strategy_factory.py`, `factory_core.py`, `factory_ledger.py`, `factory_report.py`, `llm_strategy.py`, `fit_diagnostics.py`, `agent/brain.py` | about 17,600 |
| Edge pipeline | `research/edge_discovery_core.py`, `edge_lab.py`, `edge_ledger*.py`, `edge_identity.py`, `gates.py`, `agent/edge.py`, `agent/startup_edge_policy.py`, `agent/allocation.py`, `agent/registry.py`, `agent/variants.py` | about 12,000 |
| Shadow and diagnostics | `research/live_shadow*.py`, `diagnostic_*.py`, `trader_research.py`, `trader_diagnostics.py`, `ibr_diagnostic.py`, `deploy/shadow.py`, `deploy/trader_projection.py` | about 15,700 |
| Cohorts and grammar versions | `research/mechanism_*cohort*.py`, `diagnostic_cohort_contract.py`, rule schemas v1 to v7 apart from the one kept for exits | about 3,500 |
| Paper trial and epochs | `agent/paper_trial*.py`, `agent/governance.py`, `research/paper_epoch*.py` | about 4,600 |
| Cost tooling | `research/costs.py`, `quote_costs.py`, `stressed_cost_calibration.py`, `cost_counterfactual.py`, `cost_rerun.py` (replaced by `core/costs.py`) | about 7,000 |
| Orchestration | `deploy/research-cycle.sh`, `deploy/research_*.py`, `deploy/scheduler.py`, `deploy/cohort_experiment.py`, `deploy/feed_study.py`, `deploy/market_observations.py`, `deploy/recorder*.py` (once the stream is in) | about 9,800 |
| Options | the options paths in `agent/` (unreachable under the shipped configuration) | about 1,450 |
| Documentation | merged into one operations README and one strategy note per live hypothesis | about 6,000 of 7,000 |

Before archiving, finish the two preregistered forward tests (VWAP reversion and opening-range fade) or close them formally, so that no sealed commitment is silently abandoned.

## 6. Migration plan

Each stage is one reviewed change, and the system stays runnable after every one.

| stage | change | done when |
| --- | --- | --- |
| 0 | Runtime safety fixes (this change set): B-01, C1-01, C1-02, C2-01, B-03, B-04, and the second-round findings F1 to F5 and EC-01 to EC-05 | the new tests pass, the runtime suite is green, and the tests fail on the old code |
| 1 | Stop the factory, LLM and shadow services in Compose. Keep the trader paused and the recorder running. Bind the paper-trial identity to the strategy code it runs (C1-03) | no research job runs during market hours, and a deploy no longer breaks the safety paths |
| 2 | Build `core/` next to `agent/`, with the simulated broker and the kept exit contract. Add the parity test | identical intents and orders from backtest and paper-with-simulated-broker on recorded data |
| 3 | Run `studies/liquid_reversion` on ten years of SIP bars on the VM. Apply its preregistered decision rule | a written decision, either deploy to paper or archive the hypothesis |
| 4 | Paper-trade the surviving strategy, if any, on `core/` with the WebSocket stream, for at least 20 sessions | no safety event, and fills, slippage and costs within the modelled bounds |
| 5 | Archive the modules in section 5, one change per area. Shrink the test suite to match | CI under five minutes, and about 12,000 lines in the main tree |

## 7. Risks of the migration, and their mitigations

- **Losing a safety behaviour that is pinned only by an old test.** Mitigation: port the scenario tests from `tests/test_broker_protection.py`, `tests/test_runtime_safety.py`, `tests/test_exit_safety.py` and `tests/test_cycle_resilience.py` before deleting their modules. Delete a test only together with the behaviour it pins.
- **Re-introducing look-ahead in the simulated broker.** Mitigation: the availability rule lives only in `core/events.py`, and a property test corrupts every bar after the decision time and requires an identical decision.
- **Scope creep back into a factory.** Mitigation: one strategy file per hypothesis, a written mechanism and power calculation before any code, and no automatic parameter search outside the walk-forward train windows.
