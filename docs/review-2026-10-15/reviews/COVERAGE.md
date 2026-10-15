# Review coverage: every tracked file

Reviewed commit `eefb965`. Each of the 343 tracked files (excluding this review) is listed with its review group, who reviewed it, how deeply, and the handoff file that records the result.

| file | lines | group | reviewer | depth | handoff |
| --- | --- | --- | --- | --- | --- |
| `.dockerignore` | 16 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `.env.example` | 99 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `.github/workflows/ci.yml` | 61 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `.gitignore` | 30 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `ARCHITECTURE.md` | 947 | I1_top_docs | coordinator | parameters checked against code | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `AZURE_DEPLOYMENT.md` | 305 | I1_top_docs | coordinator | parameters checked against code | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `Dockerfile` | 33 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `OPERATIONS.md` | 783 | I1_top_docs | coordinator | parameters checked against code | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `README.md` | 750 | I1_top_docs | coordinator | parameters checked against code | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `SETUP.md` | 1060 | I1_top_docs | coordinator | parameters checked against code | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `agent/__init__.py` | 0 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/allocation.py` | 256 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/alpaca_domain.py` | 542 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/alpaca_market_data.py` | 580 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/alpaca_provider.py` | 707 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/alpaca_sdk.py` | 191 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/alpaca_session.py` | 218 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/brain.py` | 170 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/config.py` | 669 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/contracts/__init__.py` | 48 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/contracts/ibr.py` | 806 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/contracts/market_context.py` | 70 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/contracts/risk_geometry.py` | 177 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/contracts/rule.py` | 2979 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/edge.py` | 490 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/engine.py` | 310 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/engine_cycle.py` | 983 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/execution_lifecycle.py` | 2964 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/governance.py` | 315 | C1_state_trial | review agent | every line | [handoff-C1_state_trial.md](handoff-C1_state_trial.md) |
| `agent/instruments.py` | 129 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/journal.py` | 419 | C1_state_trial | review agent | every line | [handoff-C1_state_trial.md](handoff-C1_state_trial.md) |
| `agent/market.py` | 163 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/market_entry_risk.py` | 880 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/order_timing.py` | 34 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/paper_trial.py` | 1136 | C1_state_trial | review agent | every line | [handoff-C1_state_trial.md](handoff-C1_state_trial.md) |
| `agent/paper_trial_operator.py` | 662 | C1_state_trial | review agent | every line | [handoff-C1_state_trial.md](handoff-C1_state_trial.md) |
| `agent/registry.py` | 220 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/risk.py` | 867 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/risk_inputs.py` | 272 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `agent/runtime_control.py` | 705 | C1_state_trial | review agent | every line | [handoff-C1_state_trial.md](handoff-C1_state_trial.md) |
| `agent/runtime_inspection.py` | 753 | C2_startup_acceptance_watchdog | review agent | every line | [handoff-C2_startup_acceptance_watchdog.md](handoff-C2_startup_acceptance_watchdog.md) |
| `agent/startup_edge_policy.py` | 724 | C2_startup_acceptance_watchdog | review agent | every line | [handoff-C2_startup_acceptance_watchdog.md](handoff-C2_startup_acceptance_watchdog.md) |
| `agent/state.py` | 493 | C1_state_trial | review agent | every line | [handoff-C1_state_trial.md](handoff-C1_state_trial.md) |
| `agent/state_store.py` | 121 | C1_state_trial | review agent | every line | [handoff-C1_state_trial.md](handoff-C1_state_trial.md) |
| `agent/strategy.py` | 339 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `agent/variants.py` | 106 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `compose.yaml` | 428 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `config.yaml` | 131 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `deploy/README.md` | 667 | I1_top_docs | coordinator | parameters checked against code | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/__init__.py` | 70 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/agent.env.example` | 9 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `deploy/alpaca-recorder.service` | 29 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/alpaca-research.service` | 31 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/alpaca-research.timer` | 10 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/alpaca-trader.service` | 32 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/alpaca-watchdog.service` | 33 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/backfill.py` | 482 | H1b_dataset_cache | coordinator | targeted | [handoff-H1b_coordinator.md](handoff-H1b_coordinator.md) |
| `deploy/cancel_paper_trial.py` | 68 | C2_startup_acceptance_watchdog | review agent | every line | [handoff-C2_startup_acceptance_watchdog.md](handoff-C2_startup_acceptance_watchdog.md) |
| `deploy/cohort_experiment.py` | 103 | G2b_paper_epoch_projection | coordinator | targeted | [handoff-G2b_coordinator.md](handoff-G2b_coordinator.md) |
| `deploy/compose.external-backup.yaml` | 12 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/dashboard.py` | 1379 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/dashboard_workbench.py` | 316 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/feed_study.py` | 120 | H1b_dataset_cache | coordinator | targeted | [handoff-H1b_coordinator.md](handoff-H1b_coordinator.md) |
| `deploy/health.py` | 1601 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/market_observations.py` | 115 | H1b_dataset_cache | coordinator | targeted | [handoff-H1b_coordinator.md](handoff-H1b_coordinator.md) |
| `deploy/paper-orb.config.json` | 158 | C2_startup_acceptance_watchdog | review agent | every line | [handoff-C2_startup_acceptance_watchdog.md](handoff-C2_startup_acceptance_watchdog.md) |
| `deploy/paper-orf.config.json` | 158 | C2_startup_acceptance_watchdog | review agent | every line | [handoff-C2_startup_acceptance_watchdog.md](handoff-C2_startup_acceptance_watchdog.md) |
| `deploy/profile_recorder_keys.py` | 124 | H1b_dataset_cache | coordinator | targeted | [handoff-H1b_coordinator.md](handoff-H1b_coordinator.md) |
| `deploy/provenance.py` | 164 | H1b_dataset_cache | coordinator | targeted | [handoff-H1b_coordinator.md](handoff-H1b_coordinator.md) |
| `deploy/recorder.py` | 3028 | H1a_recorder | coordinator | targeted | [handoff-H1a_coordinator.md](handoff-H1a_coordinator.md) |
| `deploy/recorder_market.py` | 531 | H1a_recorder | coordinator | targeted | [handoff-H1a_coordinator.md](handoff-H1a_coordinator.md) |
| `deploy/research-cycle.sh` | 1940 | H2a_research_orchestration | coordinator | targeted | [handoff-H2a_coordinator.md](handoff-H2a_coordinator.md) |
| `deploy/research-llm.env.example` | 7 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/research.env.example` | 36 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/research_budget.py` | 52 | H2a_research_orchestration | coordinator | targeted | [handoff-H2a_coordinator.md](handoff-H2a_coordinator.md) |
| `deploy/research_cache.py` | 934 | H1b_dataset_cache | coordinator | targeted | [handoff-H1b_coordinator.md](handoff-H1b_coordinator.md) |
| `deploy/research_census.py` | 1044 | H2a_research_orchestration | coordinator | targeted | [handoff-H2a_coordinator.md](handoff-H2a_coordinator.md) |
| `deploy/research_dataset.py` | 959 | H1b_dataset_cache | coordinator | targeted | [handoff-H1b_coordinator.md](handoff-H1b_coordinator.md) |
| `deploy/research_snapshot.py` | 550 | H1b_dataset_cache | coordinator | targeted | [handoff-H1b_coordinator.md](handoff-H1b_coordinator.md) |
| `deploy/scheduler.py` | 395 | H2a_research_orchestration | coordinator | targeted | [handoff-H2a_coordinator.md](handoff-H2a_coordinator.md) |
| `deploy/scheduler_output.py` | 803 | H2a_research_orchestration | coordinator | targeted | [handoff-H2a_coordinator.md](handoff-H2a_coordinator.md) |
| `deploy/session_acceptance.py` | 1668 | C2_startup_acceptance_watchdog | review agent | every line | [handoff-C2_startup_acceptance_watchdog.md](handoff-C2_startup_acceptance_watchdog.md) |
| `deploy/shadow.py` | 249 | G2b_paper_epoch_projection | coordinator | targeted | [handoff-G2b_coordinator.md](handoff-G2b_coordinator.md) |
| `deploy/test_suite.py` | 56 | H2a_research_orchestration | coordinator | targeted | [handoff-H2a_coordinator.md](handoff-H2a_coordinator.md) |
| `deploy/trader_projection.py` | 988 | G2b_paper_epoch_projection | coordinator | targeted | [handoff-G2b_coordinator.md](handoff-G2b_coordinator.md) |
| `deploy/trader_supervisor.py` | 241 | C2_startup_acceptance_watchdog | review agent | every line | [handoff-C2_startup_acceptance_watchdog.md](handoff-C2_startup_acceptance_watchdog.md) |
| `deploy/update-compose.sh` | 85 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `deploy/watchdog.py` | 311 | C2_startup_acceptance_watchdog | review agent | every line | [handoff-C2_startup_acceptance_watchdog.md](handoff-C2_startup_acceptance_watchdog.md) |
| `docs/audit-2026-09-21/FORWARD-2026-09-23.md` | 77 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/README.md` | 83 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/SIGNAL-VALUE.md` | 265 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/exit-walkforward-2026-09-23.json` | 1462 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/exit_research.py` | 74 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/fetch60.py` | 37 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/forward-costs-2026-09-22.json` | 1810 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/forward-costs-2026-09-23.json` | 1682 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/forward_costs.py` | 74 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/harness.py` | 54 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/prereg-interim-2026-09-22.json` | 83 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/prereg-interim-2026-09-23.json` | 119 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/q1-post-fix-summary.json` | 218 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/q1trades.py` | 60 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/q2-19session-summary.json` | 350 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/q2.py` | 45 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/replay-results.json` | 2978 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/runall.py` | 70 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/spread-reference.json` | 154 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/step5-geometry-grid.json` | 15746 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/step5b.py` | 81 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/step6-signal-quality.json` | 1922 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/step6b.py` | 59 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/step6c-clustered-signal-quality.json` | 1514 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/step6c.py` | 48 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/audit-2026-09-21/survivors.py` | 23 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/current-findings.md` | 9 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/edge-diagnostics.md` | 132 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/paper-activation-2026-09-15.json` | 258 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/paper-shadow-trials.md` | 241 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/preregistered-hypotheses.md` | 118 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/rule_strategy_v3_exit_protocol.md` | 83 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/strategy-variant-deltas.json` | 151 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `docs/trader-research-v6.md` | 94 | I2_audit_evidence | coordinator | key documents in full; JSON checked programmatically | [handoff-I2_audit_coordinator.md](handoff-I2_audit_coordinator.md) |
| `main.py` | 475 | B_execution_broker | review agent | every line | [handoff-B_execution_broker.md](handoff-B_execution_broker.md) |
| `report.py` | 424 | C2_startup_acceptance_watchdog | review agent | every line | [handoff-C2_startup_acceptance_watchdog.md](handoff-C2_startup_acceptance_watchdog.md) |
| `requirements.lock.txt` | 35 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `requirements.txt` | 5 | H2b_health_dashboard_infra | coordinator | targeted (security read of handlers) | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `research.py` | 1346 | F3a_edge_discovery_cli | coordinator | targeted | [handoff-F3a_coordinator.md](handoff-F3a_coordinator.md) |
| `research/README.md` | 1039 | I1_top_docs | coordinator | parameters checked against code | [handoff-I1_H2b_coordinator.md](handoff-I1_H2b_coordinator.md) |
| `research/__init__.py` | 24 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/calibration.py` | 587 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/cost_counterfactual.py` | 1545 | E2_cost_tools_data | coordinator | targeted | [handoff-E2_coordinator.md](handoff-E2_coordinator.md) |
| `research/cost_rerun.py` | 1038 | E2_cost_tools_data | coordinator | targeted | [handoff-E2_coordinator.md](handoff-E2_coordinator.md) |
| `research/costs.py` | 2407 | E1_costs | review agent | every line | [handoff-E1_costs.md](handoff-E1_costs.md) |
| `research/diagnostic_accounts.py` | 1169 | G2a_diag_accounts_trader | coordinator | targeted | [handoff-G2a_coordinator.md](handoff-G2a_coordinator.md) |
| `research/diagnostic_cohort_contract.py` | 189 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `research/diagnostic_shadow.py` | 386 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `research/diagnostic_suite.py` | 472 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `research/edge_discovery_core.py` | 2059 | F3a_edge_discovery_cli | coordinator | targeted | [handoff-F3a_coordinator.md](handoff-F3a_coordinator.md) |
| `research/edge_identity.py` | 145 | F3a_edge_discovery_cli | coordinator | targeted | [handoff-F3a_coordinator.md](handoff-F3a_coordinator.md) |
| `research/edge_lab.py` | 918 | F3a_edge_discovery_cli | coordinator | targeted | [handoff-F3a_coordinator.md](handoff-F3a_coordinator.md) |
| `research/edge_ledger.py` | 994 | F3b_edge_ledger | coordinator | targeted | [handoff-F3b_coordinator.md](handoff-F3b_coordinator.md) |
| `research/edge_ledger_proof.py` | 1254 | F3b_edge_ledger | coordinator | targeted | [handoff-F3b_coordinator.md](handoff-F3b_coordinator.md) |
| `research/edge_ledger_store.py` | 328 | F3b_edge_ledger | coordinator | targeted | [handoff-F3b_coordinator.md](handoff-F3b_coordinator.md) |
| `research/factory_core.py` | 2895 | F1b_factory_core | review agent | every line | [handoff-F1b_factory_core.md](handoff-F1b_factory_core.md) |
| `research/factory_ledger.py` | 2555 | F2a_factory_ledger_report | coordinator | targeted | [handoff-F2a_coordinator.md](handoff-F2a_coordinator.md) |
| `research/factory_report.py` | 1910 | F2a_factory_ledger_report | coordinator | targeted | [handoff-F2a_coordinator.md](handoff-F2a_coordinator.md) |
| `research/fit_diagnostics.py` | 1972 | F2b_llm_fit | coordinator | targeted | [handoff-F2b_coordinator.md](handoff-F2b_coordinator.md) |
| `research/gates.py` | 4461 | D2_gates | review agent | every line | [handoff-D2_gates.md](handoff-D2_gates.md) |
| `research/ibr.py` | 1476 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `research/ibr_diagnostic.py` | 1014 | G2a_diag_accounts_trader | coordinator | targeted | [handoff-G2a_coordinator.md](handoff-G2a_coordinator.md) |
| `research/live_shadow.py` | 8349 | G1a_live_shadow_part1 | review agent | every line (first half) | [handoff-G1a_live_shadow_part1.md](handoff-G1a_live_shadow_part1.md) |
| `research/live_shadow.py` | 8349 | G1b_live_shadow_part2 | review agent | every line (second half) | [handoff-G1b_live_shadow_part2.md](handoff-G1b_live_shadow_part2.md) |
| `research/live_shadow_ingest.py` | 1473 | G1c_live_shadow_ingest | coordinator | targeted | [handoff-G1c_coordinator.md](handoff-G1c_coordinator.md) |
| `research/llm_strategy.py` | 2096 | F2b_llm_fit | coordinator | targeted | [handoff-F2b_coordinator.md](handoff-F2b_coordinator.md) |
| `research/market_context_study.py` | 137 | E2_cost_tools_data | coordinator | targeted | [handoff-E2_coordinator.md](handoff-E2_coordinator.md) |
| `research/market_data.py` | 791 | E2_cost_tools_data | coordinator | targeted | [handoff-E2_coordinator.md](handoff-E2_coordinator.md) |
| `research/maturity.py` | 21 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/mechanism_cohort.py` | 58 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `research/mechanism_cohort_v2.py` | 133 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `research/mechanism_cost_cohort.py` | 40 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `research/mechanism_deadline_cohort.py` | 97 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `research/mechanism_refresh_cohort.py` | 451 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `research/paper_epoch.py` | 2024 | G2b_paper_epoch_projection | coordinator | targeted | [handoff-G2b_coordinator.md](handoff-G2b_coordinator.md) |
| `research/paper_epoch_cli.py` | 158 | G2b_paper_epoch_projection | coordinator | targeted | [handoff-G2b_coordinator.md](handoff-G2b_coordinator.md) |
| `research/paper_epoch_export.py` | 345 | G2b_paper_epoch_projection | coordinator | targeted | [handoff-G2b_coordinator.md](handoff-G2b_coordinator.md) |
| `research/path_telemetry.py` | 855 | E2_cost_tools_data | coordinator | targeted | [handoff-E2_coordinator.md](handoff-E2_coordinator.md) |
| `research/preregistered.py` | 568 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/proof.py` | 169 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/proof_payload.py` | 630 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/protocol.md` | 732 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/quote_costs.py` | 1237 | E1_costs | review agent | every line | [handoff-E1_costs.md](handoff-E1_costs.md) |
| `research/signal_quality.py` | 1389 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/source_validation.py` | 277 | E2_cost_tools_data | coordinator | targeted | [handoff-E2_coordinator.md](handoff-E2_coordinator.md) |
| `research/stats.py` | 1387 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/strategy_factory.py` | 6003 | F1a_strategy_factory | coordinator | targeted | [handoff-F1a_coordinator.md](handoff-F1a_coordinator.md) |
| `research/stressed_cost_calibration.py` | 732 | E1_costs | review agent | every line | [handoff-E1_costs.md](handoff-E1_costs.md) |
| `research/trader_diagnostics.py` | 634 | G2a_diag_accounts_trader | coordinator | targeted | [handoff-G2a_coordinator.md](handoff-G2a_coordinator.md) |
| `research/trader_research.py` | 756 | G2a_diag_accounts_trader | coordinator | targeted | [handoff-G2a_coordinator.md](handoff-G2a_coordinator.md) |
| `research/trial.py` | 523 | D1_stats_signal_prereg | review agent | every line | [handoff-D1_stats_signal_prereg.md](handoff-D1_stats_signal_prereg.md) |
| `research/variants.yaml` | 11 | A_signals | review agent | every line | [handoff-A_signals.md](handoff-A_signals.md) |
| `tests/__init__.py` | 15 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/__init__.py` | 0 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_bar_gap_scope.py` | 369 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_calibration_publication.py` | 351 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_cost_counterfactual.py` | 671 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_cost_rerun.py` | 243 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_costs.py` | 1093 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_counterfactual_geometry_stage.py` | 41 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_cross_sectional_residual.py` | 445 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_diagnostic_accounts.py` | 422 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_diagnostic_live_shadow.py` | 959 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_diagnostic_llm_epoch.py` | 161 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_diagnostic_quote_index.py` | 367 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_diagnostic_suite.py` | 357 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_economic_diagnosis.py` | 46 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_edge_discovery.py` | 2348 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_edge_identity.py` | 149 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_entry_bar_visibility.py` | 709 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_entry_deadline_admission.py` | 141 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_factory_discovery_ladder.py` | 151 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_factory_end_to_end.py` | 295 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_factory_fdr.py` | 256 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_factory_identity.py` | 100 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_factory_lesson_epoch.py` | 440 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_factory_lifecycle_budget.py` | 421 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_factory_replacement.py` | 57 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_factory_report.py` | 628 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_factory_worker_view.py` | 208 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_fit_diagnostics.py` | 584 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_gates.py` | 1436 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_ibr.py` | 607 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_ibr_diagnostic_adapter.py` | 341 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_ibr_diagnostic_evidence.py` | 278 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_ibr_lane_parity.py` | 116 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_ibr_runtime_parity.py` | 657 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_live_shadow.py` | 1492 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_live_shadow_cadence.py` | 138 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_live_shadow_controls.py` | 203 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_live_shadow_ingest.py` | 719 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_live_shadow_repair.py` | 271 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_live_shadow_session_projection.py` | 328 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_llm_strategy.py` | 585 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_llm_tuning.py` | 1669 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_market_context_study.py` | 52 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_market_data.py` | 290 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_market_data_lane_boundaries.py` | 394 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_mechanism_cohort.py` | 53 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_mechanism_cohort_v2.py` | 66 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_mechanism_cost_cohort.py` | 36 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_mechanism_deadline_cohort.py` | 148 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_mechanism_refresh_cohort.py` | 153 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_null_entry_clock.py` | 253 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_observability.py` | 69 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_offline_shadow_worker.py` | 184 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_option_exit_tolerance.py` | 251 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_paired_gate_identity.py` | 237 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_paper_epoch.py` | 351 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_paper_epoch_export.py` | 197 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_paper_performance.py` | 263 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_path_telemetry.py` | 134 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_pre_admission_path_telemetry.py` | 389 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_preregistered.py` | 251 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_proof.py` | 315 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_quote_costs.py` | 574 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_quote_index_descriptor.py` | 215 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_recorder_cadence_fix.py` | 95 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_refusal_reasons.py` | 121 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_replay_code_identity.py` | 43 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_replay_engine_epoch.py` | 253 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_reproof_lifecycle.py` | 36 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_research_integrity.py` | 424 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_rule_context_v5.py` | 139 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_rule_context_v6.py` | 220 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_rule_geometry_v7.py` | 105 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_rule_grammar_v2.py` | 499 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_runtime_stressed_cost_parity.py` | 157 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_screen_horizon_safety.py` | 189 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_semantic_near_dedup.py` | 259 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_shadow_batch_safety.py` | 312 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_shadow_calendar_poll.py` | 324 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_shadow_deadlines.py` | 323 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_shadow_diagnostic_performance.py` | 286 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_shadow_entry_slippage.py` | 418 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_shadow_incremental_poll.py` | 323 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_shadow_poll_metrics.py` | 387 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_shadow_replay_memo.py` | 340 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_signal_maturity_verification.py` | 43 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_signal_quality.py` | 385 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_signal_quality_null.py` | 187 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_slippage_parity.py` | 91 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_slot_lifecycle.py` | 661 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_source_validation.py` | 114 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_stats.py` | 197 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_strategy_factory.py` | 1825 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_stressed_cost_calibration.py` | 421 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_trader_diagnostics.py` | 196 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_trader_projection.py` | 236 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_trader_research.py` | 691 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_trial.py` | 367 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_trial_confidence.py` | 160 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_unpriceable_corpus.py` | 335 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_validate_data_streaming.py` | 100 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/research/test_vehicle_cost_propagation.py` | 70 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_allocation.py` | 444 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_alpaca_runtime.py` | 880 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_backfill.py` | 594 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_brain.py` | 62 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_broker_protection.py` | 1229 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_cli.py` | 446 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_cohort_integration_regression.py` | 293 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_dashboard_workbench.py` | 147 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_default_paper_selection.py` | 138 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_default_research_capacity.py` | 24 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_deploy.py` | 4232 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_diagnostic_cohort_contract.py` | 139 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_evidence_reporting.py` | 106 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_execution_fault_handover.py` | 255 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_execution_lifecycle.py` | 1107 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_exit_contract.py` | 1186 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_feed_study.py` | 26 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_governance.py` | 241 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_ibr_shadow_operations.py` | 74 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_instrument_policy.py` | 202 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_market_entry_availability.py` | 182 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_market_observations.py` | 52 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_order_timing.py` | 43 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_paper_release_profile.py` | 118 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_paper_selection_status.py` | 213 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_paper_shadow_status.py` | 186 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_paper_supervisor_status.py` | 126 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_paper_trial_cohort_contract.py` | 324 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_paper_trial_operator.py` | 394 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_paper_trial_runtime.py` | 1030 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_parallel_runtime_status.py` | 570 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_pinned_runtime.py` | 354 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_preacceptance_diagnostic_cycle.py` | 400 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_recorder_key_layout.py` | 154 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_recorder_measurement.py` | 507 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_recorder_status_contract.py` | 69 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_registry.py` | 38 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_research_budget.py` | 25 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_research_cache.py` | 369 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_research_calibration_config.py` | 37 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_research_census.py` | 496 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_research_dataset_streaming.py` | 523 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_research_direct_status.py` | 57 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_research_progress.py` | 245 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_research_readiness_preflight.py` | 427 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_research_snapshot.py` | 138 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_risk.py` | 625 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_risk_geometry.py` | 71 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_rule_signal_inputs.py` | 178 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_runtime_inspection.py` | 457 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_runtime_safety.py` | 2212 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_session_acceptance.py` | 566 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_session_acceptance_readiness_parity.py` | 113 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_shadow_coverage_readiness.py` | 313 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_shadow_observation_status.py` | 71 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_shadow_service_cadence.py` | 206 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_shadow_wal_lifetime.py` | 150 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_state.py` | 542 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_strategy.py` | 320 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_strategy_zero_parameters.py` | 170 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
| `tests/test_trader_supervisor.py` | 189 | J_tests | coordinator | AST inventory of every test; targeted reading | [handoff-J_tests_coordinator.md](handoff-J_tests_coordinator.md) |
