from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from agent.config import validate_config
from agent.contracts.rule import (
    RuleSpecError, evaluate_rule_signal, rule_spec_hash, rule_variant_id,
    validate_rule_spec,
)
from agent.edge import apply_variant, resolve_validated_variant
from research.edge_lab import EdgeLedger
from research.edge_identity import candidate_assumptions
from research.edge_ledger_store import content_hash, provenance_hash
from research import gates
from research.gates import (
    falsification_gate, heldout_separation, matched_cluster_test,
    performance_floor, placebo_null_distribution, qualification_report,
    structural_floor, verified_gate_envelope, walk_forward_report,
)
from research.llm_strategy import PROPOSAL_SCHEMA, ProposalResult
import research.factory_core as core_module
import research.strategy_factory as factory_module
from research.strategy_factory import (
    FactoryError, FactoryLedger, initial_hypotheses, mutate_from_diagnosis,
    replacement_hypothesis, run_factory,
)


def losing_breakouts(sessions=12):
    rows = []
    base = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    for offset in range(sessions):
        start = base + timedelta(days=offset)
        values = []
        for minute in range(15):
            values.append((100, 101, 99.5, 100.2, 1000))
        values.extend([
            (100.2, 102.5, 100.1, 102.0, 5000),
            (102.0, 102.2, 101.8, 102.0, 2000),
            (102.0, 102.1, 98.0, 98.5, 2000),
        ])
        for minute, (open_, high, low, close, volume) in enumerate(values):
            timestamp = start + timedelta(minutes=minute)
            observed = timestamp + timedelta(minutes=1)
            rows.append({
                "kind": "bar", "provider": "test", "feed": "sip",
                "symbol": "SPY", "timestamp": timestamp.isoformat(),
                "as_of": observed.isoformat(), "observed_at": observed.isoformat(),
                "open": open_, "high": high, "low": low, "close": close,
                "volume": volume,
            })
    return rows


def fake_adequate_worker(payload):
    hypothesis = payload["hypothesis"]
    if int(hypothesis["slot"]) == 0:
        time.sleep(.02)
    # Use the orchestrator-selected specs. Reconstructing the old first batch
    # here would hide refinement progression and replay duplicate identities.
    specs = [validate_rule_spec(item) for item in payload["specs"]]
    # The terminal-negative protocol requires at least 30 held-out sessions.
    # With the factory's 70/30 split, 120 sessions leave 36 held out and three
    # adequately sized, independently negative walk-forward windows.
    sessions = [
        (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index))
        .date().isoformat()
        for index in range(120)
    ]
    replay_sessions = sorted({
        (row.timestamp.date().isoformat() if hasattr(row, "timestamp") else
         str(row["timestamp"])[:10])
        for row in payload.get("bars", ())
    })
    control_rows = [
        {"vehicle": payload["vehicle"], "symbol": "SPY", "session_date": session,
         "opportunity_id": f"control:{session}", "net_pnl": 0.0,
         "return_value": 0.0, "no_trade": False}
        for session in sessions
    ]
    variants = []
    for spec in specs:
        variant_id = rule_variant_id(spec)
        rows = [{**row, "opportunity_id": f"{variant_id}:{row['session_date']}",
                 "net_pnl": -1.0, "return_value": -.00001,
                 "risk_usd": 10.0, "r_multiple": -0.1}
                for row in control_rows]
        variants.append({
            "variant_id": variant_id, "rule_spec": spec, "vehicle": payload["vehicle"],
            "account": {"account_id": (f"account:{hypothesis['hypothesis_id']}:"
                                        f"{variant_id}:{uuid.uuid4().hex[:8]}"),
                        "starting_cash": payload["starting_cash"],
                        "ending_equity": payload["starting_cash"] - len(rows),
                        "realized_pnl": -float(len(rows)), "max_drawdown": float(len(rows)),
                        "trades": len(rows), "rows": rows},
            "diagnostic": {"primary_failure": "negative_expectancy", "net_pnl": -len(rows)},
            "worker_pid": int(hypothesis["slot"]) + 100,
        })
    return {"hypothesis": hypothesis, "mode": payload["mode"],
            "diagnostic": {"primary_failure": "negative_expectancy", "net_pnl": -6},
            # This fixture isolates replacement lifecycle behavior. Dedicated
            # tests exercise coordinate -> interaction -> confirmation.
            "refinement": {"phase": "confirmatory"},
            "evaluation_start": replay_sessions[0] if replay_sessions else sessions[0],
            "evaluation_end": replay_sessions[-1] if replay_sessions else sessions[-1],
            "variants": list(reversed(variants)), "control_rows": control_rows,
            "expected_variants": len(specs), "worker_pid": int(hypothesis["slot"]) + 100}


def persist_rule_gate(ledger, candidate_id, lane):
    candidate = ledger.candidate(candidate_id)
    candidate_config = json.loads(candidate["config_json"])
    symbols = ("SPY", "QQQ", "IWM", "DIA")
    # Shadow fixtures mirror the live boundary: forty older selection sessions
    # and forty newer confirmatory sessions (160 confirmatory trades, clearing
    # the strict 150-trade shadow floor).
    session_count = 80 if lane == "shadow" else 40

    def row(symbol, session, opportunity, net_pnl):
        return {
            "vehicle": "equity", "symbol": symbol,
            "session_date": session, "opportunity_id": opportunity,
            "net_pnl": net_pnl, "entry_price": 100.0,
            "exit_price": 101.0, "quantity": 1.0, "risk_usd": 1.0,
            "entry_fill_source": "quote", "exit_fill_source": "quote",
            "entry_feed": "sip", "exit_feed": "sip",
            "entry_provider": "alpaca", "exit_provider": "alpaca",
            "entry_quote_age_seconds": 0.0,
            "exit_quote_age_seconds": 0.0,
        }

    fit = [] if lane == "shadow" else [
        row(symbol, (datetime(2025, 11, 20) + timedelta(days=offset)).date().isoformat(),
            f"{lane}-fit-{offset}-{symbol}", 1.0)
        for offset in range(session_count) for symbol in symbols]
    first_heldout = (datetime(2025, 11, 27) if lane == "shadow"
                     else datetime(2026, 1, 6))
    all_heldout = [
        row(symbol, (first_heldout + timedelta(days=offset)).date().isoformat(),
            f"{lane}-held-{offset}-{symbol}", 1.0)
        for offset in range(session_count) for symbol in symbols]
    heldout = list(all_heldout)
    selection_sessions: list[str] = []
    confirmatory_sessions: list[str] = []
    selection_rows: list[dict] = []
    if lane == "shadow":
        all_sessions = sorted({row["session_date"] for row in all_heldout})
        split = len(all_sessions) // 2
        selection_sessions, confirmatory_sessions = all_sessions[:split], all_sessions[split:]
        selection_rows = [row for row in all_heldout
                          if row["session_date"] in set(selection_sessions)]
        heldout = [row for row in all_heldout
                   if row["session_date"] in set(confirmatory_sessions)]
    baseline = [{**row, "net_pnl": 0.0, "opportunity_id": f"base-{index}"}
                for index, row in enumerate(heldout)]
    fit_baseline = [{**row, "net_pnl": 0.0,
                     "opportunity_id": f"fit-base-{index}"}
                    for index, row in enumerate(fit)]
    fit_floor = structural_floor(
        fit, vehicle="equity",
        min_trades=150 if lane == "shadow" else 100, min_sessions=30,
        min_clusters=30,
        required=lane != "shadow")
    held_floor = structural_floor(
        heldout, vehicle="equity", min_trades=150 if lane == "shadow" else 100,
        min_sessions=30, min_clusters=30)
    separation = (heldout_separation(fit, heldout) if lane == "backtest" else
                  {"passes": True, "mode": "new_data"})
    control = matched_cluster_test(heldout, baseline, vehicle="equity")
    fit_control = (matched_cluster_test(fit, fit_baseline, vehicle="equity")
                   if fit else {"available": True, "actual_control": True,
                                "matched": 0, "mean_delta": None,
                                "p_value": 1.0, "mode": "prior_backtest"})
    placebo = placebo_null_distribution(heldout, baseline, vehicle="equity")
    falsification = {
        **falsification_gate(placebo["observed"], placebo["placebo"]),
        "draws": int(placebo["draws"]), "seed": int(placebo["seed"])}
    absolute = performance_floor(heldout, vehicle="equity")
    walk = walk_forward_report(heldout, baseline, vehicle="equity", folds=3)
    qualification = qualification_report(
        heldout, baseline, vehicle="equity",
        sessions=sorted({row["session_date"] for row in heldout}),
        candidate_id=candidate_id, preselected=True)
    hashes = provenance_hash(config=candidate_config)
    if lane == "shadow":
        hashes.update({
            "independent_confirmatory": True,
            "disjoint_sessions": True,
            "session_disjoint": True,
            "selection_sessions": selection_sessions,
            "confirmatory_sessions": confirmatory_sessions,
            "selection_session_digest": content_hash(selection_sessions),
            "confirmatory_session_digest": content_hash(confirmatory_sessions),
            "p_value_source": "live_shadow_confirmatory_gate",
            "selection_raw_p_value": control["p_value"],
            "confirmatory_raw_p_value": control["p_value"],
        })
    fdr_record = (FactoryLedger(ledger.path).record_fdr_decision(
        "shadow-confirmation-v4:equity", f"{candidate_id}:shadow",
        control["p_value"], alpha=.05) if lane == "shadow" else None)
    gate = verified_gate_envelope(
        lane=lane, vehicle="equity", fit=fit, heldout=heldout,
        fit_baseline=fit_baseline, heldout_baseline=baseline,
        null_source=baseline,
        fit_floor=fit_floor, heldout_floor=held_floor,
        fit_control=fit_control,
        control={**control, "kind": "matched_root_baseline"},
        p_value=control["p_value"], q_value=control["p_value"], alpha=.05,
        falsification=falsification, separation=separation,
        checks={"family_fdr_significant": True, "global_fdr_significant": True,
                "cumulative_fdr_significant": True,
                "falsification": bool(falsification["passes"]),
                "heldout_net_pnl_positive": bool(absolute["net_pnl_positive"]),
                "heldout_expectancy_positive": bool(absolute["expectancy_positive"]),
                "heldout_delta_lcb_positive": bool(control["mean_delta_lcb"] > 0)},
        passes=True,
        walk_forward=walk,
        qualification=qualification,
        null_control={"kind": "randomized_entry_null",
                      "matched": control["matched"], "available": True,
                      "mean_delta": control["mean_delta"],
                      "mean_delta_lcb": control["mean_delta_lcb"],
                      "p_value": control["p_value"]},
        online_fdr={"scope": ("shadow-confirmation-v4:equity" if lane == "shadow"
                              else "test"),
                    "test_id": f"{candidate_id}:{lane}",
                    "p_value": control["p_value"], "raw_p_value": control["p_value"],
                    "selection_raw_p_value": control["p_value"],
                    "confirmatory_raw_p_value": control["p_value"],
                    "family_q_value": control["p_value"], "global_q_value": control["p_value"],
                    "allocated_alpha": (fdr_record["allocated_alpha"]
                                        if fdr_record is not None else .05),
                    "alpha": .05,
                    "tests": (fdr_record["tests"] if fdr_record is not None else 1),
                    "decision": True,
                    "required": True, "tested": True,
                    "p_value_kind": "raw_confirmatory",
                    "p_value_source": "live_shadow_confirmatory_gate",
                    "independent_confirmatory": lane == "shadow",
                    "disjoint_sessions": lane == "shadow",
                    "session_disjoint": lane == "shadow",
                    **({"selection_sessions": selection_sessions,
                        "confirmatory_sessions": confirmatory_sessions,
                        "selection_session_digest": content_hash(selection_sessions),
                        "confirmatory_session_digest": content_hash(confirmatory_sessions)}
                       if lane == "shadow" else {})},
        provenance=hashes, candidate_id=candidate_id,
        performance={"heldout_delta": control["mean_delta"],
                     "heldout_delta_lcb": control["mean_delta_lcb"],
                     "heldout_net_pnl": absolute["net_pnl"],
                     "heldout_expectancy": absolute["expectancy"],
                     "max_drawdown": 0.0})
    run = ledger.append_run(candidate_id, lane=lane, fit=fit, heldout=heldout,
                            config=candidate_config,
                            metrics={"gate": {"passes": True,
                                               "verified_gate": gate,
                                               "gate_hash": gate["content_hash"]},
                                     "confidence": .99,
                                     "heldout_delta": 1.0, "max_drawdown": 0.0,
                                     "heldout_trades": len(heldout),
                                     **({"shadow_source": {
                                         "schema": "shadow-ingest.v1",
                                         "candidate_id": candidate_id,
                                         "vehicle": "equity",
                                         "independent_confirmatory": True,
                                         "disjoint_sessions": True,
                                         "session_disjoint": True,
                                         "selection_sessions": selection_sessions,
                                         "confirmatory_sessions": confirmatory_sessions,
                                         "selection_session_digest": content_hash(selection_sessions),
                                         "confirmatory_session_digest": content_hash(confirmatory_sessions),
                                         "p_value_source": "live_shadow_confirmatory_gate",
                                         "sessions": [{
                                             "session_date": day,
                                             "source_digest": f"source:{day}",
                                             "shadow_digest": f"shadow:{day}",
                                             "replay_digest": f"replay:{day}",
                                             "account_id": f"account:{day}",
                                             "trade_count": 4,
                                         } for day in [*selection_sessions,
                                                       *confirmatory_sessions]],
                                         "selection": {
                                             "sessions": selection_sessions,
                                             "session_digest": content_hash(selection_sessions),
                                             "rows_digest": content_hash(selection_rows),
                                             "candidate_source": selection_rows,
                                             "baseline_source": [
                                                 {**row, "net_pnl": 0.0,
                                                  "opportunity_id": f"selection-base-{index}"}
                                                 for index, row in enumerate(selection_rows)],
                                             "null_source": [
                                                 {**row, "net_pnl": 0.0,
                                                  "opportunity_id": f"selection-null-{index}"}
                                                 for index, row in enumerate(selection_rows)],
                                             "minimums": {"trades": 150, "sessions": 30},
                                             "baseline_rows_digest": content_hash([
                                                 {**row, "net_pnl": 0.0,
                                                  "opportunity_id": f"selection-base-{index}"}
                                                 for index, row in enumerate(selection_rows)]),
                                             "null_rows_digest": content_hash([
                                                 {**row, "net_pnl": 0.0,
                                                  "opportunity_id": f"selection-null-{index}"}
                                                 for index, row in enumerate(selection_rows)]),
                                             "p_value_source": "selection_window_gate",
                                             "raw_p_value": control["p_value"],
                                             "alpha": .05, "test_iterations": 20_000,
                                             "bh": {
                                                 "family_values": {"fixture": {candidate_id: control["p_value"]}},
                                                 "family_results": {candidate_id: {
                                                     "p": control["p_value"], "p_adjusted": control["p_value"],
                                                     "significant": True, "family_size": 1}},
                                                 "global_values": {candidate_id: control["p_value"]},
                                                 "global_results": {candidate_id: {
                                                     "p": control["p_value"], "p_adjusted": control["p_value"],
                                                     "significant": True, "family_size": 1}},
                                             },
                                         },
                                         "confirmatory": {
                                             "sessions": confirmatory_sessions,
                                             "session_digest": content_hash(confirmatory_sessions),
                                             "rows_digest": content_hash(heldout),
                                             "baseline_rows_digest": content_hash(baseline),
                                             "null_rows_digest": content_hash(baseline),
                                             "test_id": f"{candidate_id}:shadow",
                                             "p_value_source": "live_shadow_confirmatory_gate",
                                             "raw_p_value": control["p_value"],
                                         },
                                         "baseline": {"candidate_id": "fixture:baseline",
                                                      "rows_digest": content_hash(baseline),
                                                      "role": "paired_root_control"},
                                         "null": {"candidate_id": "fixture:null",
                                                   "rows_digest": content_hash(baseline),
                                                   "role": "randomized_entry_null"},
                                     }, "replay_digests": [f"replay:{day}"
                                                           for day in [*selection_sessions,
                                                                       *confirmatory_sessions]]}
                                     if lane == "shadow" else {})})
    for row in [*fit, *heldout]:
        ledger.append_trade(run["run_id"], row)
    ledger.record_verified_gate(run["run_id"], gate)
    if lane == "shadow":
        source = ledger.run(run["run_id"])["metrics"]["shadow_source"]
        ledger.record_shadow_ingestion(
            candidate_id,
            {"schema": "shadow-ingest.v1", "candidate_id": candidate_id,
             "vehicle": "equity", "source": source,
             "replay_digests": [item["replay_digest"] for item in source["sessions"]],
             "run_provenance": {**hashes, "candidate_id": candidate_id},
             "candidate_proof": {key: ledger.candidate(candidate_id).get(key)
                                 for key in ("candidate_id", "dataset_hash",
                                             "config_hash", "code_hash",
                                             "provenance_hash")},
             "gate_hash": gate["content_hash"]}, run_id=run["run_id"])


class StrategyFactoryTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise bounded mutation/lifecycle mechanics with
        # compact synthetic corpora.  Explicitly lower protocol constants in
        # this test-only context; production ``run_factory`` remains strict,
        # and dedicated floor-regression tests below cover that boundary.
        self.compact_protocol = patch.multiple(
            gates,
            PROTOCOL_BACKTEST_MIN_TRADES=1,
            PROTOCOL_BACKTEST_MIN_SESSIONS=1,
            PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
            PROTOCOL_SHADOW_MIN_TRADES=1,
            PROTOCOL_SHADOW_MIN_SESSIONS=1,
            PROTOCOL_SHADOW_MIN_CLUSTERS=1,
            PROTOCOL_QUALIFICATION_MIN_TRADES=1,
            PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
            PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1,
        )
        self.compact_protocol.start()
        self.addCleanup(self.compact_protocol.stop)

    def test_facade_reexports_deterministic_core_symbols_by_identity(self):
        moved = (
            "DEFAULT_STRATEGIES", "DEFAULT_VARIANTS", "MAX_STRATEGIES", "MAX_VARIANTS",
            "StrategyHypothesis", "_hypothesis_id", "_thesis", "_falsification",
            "initial_hypotheses", "_session", "_visible", "_option_at",
            "_simulate_trade", "simulate_account", "diagnose", "_safe_variant",
            "mutate_from_diagnosis", "replacement_hypothesis",
        )
        for name in moved:
            self.assertIs(getattr(factory_module, name), getattr(core_module, name), name)

    def test_initial_and_replacement_results_are_deterministic(self):
        first = initial_hypotheses()
        second = initial_hypotheses()
        self.assertEqual(first, second)
        previous = vars(first[0])
        diagnosis = {"primary_failure": "negative_expectancy"}
        replacement_one = replacement_hypothesis(
            previous, diagnosis, max_generations=4)
        replacement_two = replacement_hypothesis(
            previous, diagnosis, max_generations=4)
        self.assertEqual(replacement_one, replacement_two)

    def test_underpowered_shadow_accounts_do_not_advance_the_forward_boundary(self):
        # Accounts are diagnostic rows and are written before adequacy is
        # known.  Only a durable shadow proof run may advance last_boundary.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge_lab.sqlite3"
            EdgeLedger(path)
            factory = FactoryLedger(path)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            factory.add_account("cycle", hypothesis.hypothesis_id, {
                "variant_id": "rule.synthetic",
                "vehicle": "equity", "worker_pid": 1,
                "evaluation_start": "2026-01-01",
                "evaluation_end": "2026-01-31",
                "account": {"account_id": "account.synthetic",
                            "starting_cash": 100.0, "ending_equity": 100.0,
                            "realized_pnl": 0.0, "max_drawdown": 0.0,
                            "trades": 0},
            })
            self.assertIsNone(factory.last_boundary(
                hypothesis.hypothesis_id, "equity"))
            # Conversely, an actual shadow run is a consumed boundary.  This
            # distinguishes the all-intended-adequacy guard from simply
            # disabling forward validation altogether.
            candidate = EdgeLedger(path).register_candidate(
                "rule.synthetic.boundary", strategy_id="rule", vehicle="equity",
                hypothesis="h", config={"strategy": {"rule_spec":
                    hypothesis.rule_spec}},
                axes={"hypothesis_id": hypothesis.hypothesis_id})
            run = EdgeLedger(path).append_run(
                candidate["candidate_id"], lane="shadow", vehicle="equity",
                fit=[], heldout=[{"vehicle": "equity",
                                  "session_date": "2026-02-01",
                                  "opportunity_id": "boundary",
                                  "net_pnl": 0.0}])
            EdgeLedger(path).append_trade(run["run_id"], {
                "vehicle": "equity", "session_date": "2026-02-01",
                "opportunity_id": "boundary", "net_pnl": 0.0})
            # An unverified/crash-created run is not a durable boundary.
            self.assertIsNone(factory.last_boundary(
                hypothesis.hypothesis_id, "equity"))
            # The helper appends the gate evidence through the same
            # record_verified_gate path used by the factory.
            persist_rule_gate(EdgeLedger(path), candidate["candidate_id"], "shadow")
            self.assertEqual(factory.last_boundary(
                hypothesis.hypothesis_id, "equity"), "2026-02-14")

    def test_run_factory_records_family_pass_global_fail_as_a_failed_gate(self):
        """A normal BH family pass must not bypass the cycle-global gate."""
        rows = losing_breakouts(sessions=20)
        calls = {"gate": 0}

        def fake_diagnose(task):
            return {"hypothesis_id": task["hypothesis"]["hypothesis_id"],
                    "diagnostic": {"primary_failure": "negative_expectancy"}}

        def fake_worker(task):
            sessions = sorted({factory_module._session(bar) for bar in task["bars"]})
            control = [{"vehicle": "equity", "symbol": "SPY",
                        "session_date": session,
                        "opportunity_id": f"control:{session}",
                        "net_pnl": 0.0, "return_value": 0.0,
                        "no_trade": False} for session in sessions]
            variants = []
            for spec in task["specs"]:
                variant_id = rule_variant_id(spec)
                account_rows = [{**row,
                                 "opportunity_id": f"{variant_id}:{row['session_date']}"}
                                for row in control]
                variants.append({
                    "variant_id": variant_id, "rule_spec": spec,
                    "vehicle": "equity",
                    "account": {"account_id": f"account:{variant_id}",
                                "starting_cash": 100_000.0,
                                "ending_equity": 100_000.0,
                                "realized_pnl": 0.0, "max_drawdown": 0.0,
                                "trades": len(account_rows), "rows": account_rows},
                    "diagnostic": {"primary_failure": "negative_expectancy",
                                   "net_pnl": 0.0},
                    "worker_pid": 1})
            return {"hypothesis": task["hypothesis"], "mode": task["mode"],
                    "diagnostic": task["diagnostic"],
                    "evaluation_start": sessions[0], "evaluation_end": sessions[-1],
                    "variants": variants, "control_rows": control,
                    "null_rows": {}, "expected_variants": len(variants),
                    "worker_pid": 1}

        def fake_gate(rows, baseline, **kwargs):
            ordered = sorted(rows, key=lambda row: row["session_date"])
            fit, heldout = factory_module.chronological_split(ordered, fit_fraction=.7)
            fit_floor = structural_floor(
                fit, vehicle="equity", min_trades=1, min_sessions=1)
            held_floor = structural_floor(
                heldout, vehicle="equity", min_trades=1, min_sessions=1)
            p_value = .01 if calls["gate"] == 0 else .9
            calls["gate"] += 1
            control = {"actual_control": False, "available": False,
                       "matched": 0, "mean_delta": None}
            checks = {
                "fit_structurally_adequate": True,
                "heldout_structurally_adequate": True,
                "separated": True, "actual_control_available": False,
                "fit_delta_positive": False, "heldout_delta_positive": False,
                "heldout_p_significant": p_value <= .05, "falsification": False,
                "heldout_net_pnl_positive": False,
                "heldout_expectancy_positive": False,
                "null_control_available": False,
                "null_control_delta_positive": False,
                "walk_forward_majority_positive": False,
                "qualification_net_positive": False,
                "qualification_delta_positive": False,
            }
            return {
                "passes_without_family": False, "passes": False,
                "p_raw": p_value, "sample_adequate": True,
                "heldout_sample_adequate": True,
                "confidence": 1.0 - p_value,
                "floor": structural_floor(ordered, vehicle="equity",
                                           min_trades=1, min_sessions=1),
                "fit_floor": fit_floor, "heldout_floor": held_floor,
                "fit_net_pnl": 0.0, "heldout_net_pnl": 0.0,
                "heldout_expectancy": 0.0,
                "heldout_performance": {"net_pnl_positive": False,
                                        "expectancy_positive": False},
                "fit_trades": len(fit), "heldout_trades": len(heldout),
                "heldout_delta_lcb": None, "max_drawdown": 0.0,
                "test": {**control, "p_value": p_value},
                "fit_test": {}, "control": control,
                "null_control": {"kind": "randomized_entry_null",
                                  "matched": 0, "available": False,
                                  "mean_delta": None, "p_value": 1.0},
                "walk_forward": {}, "qualification": {"available": False},
                "falsification": {"passes": False, "draws": 0},
                "heldout_separation": {"passes": True},
                "checks_without_family": checks,
                "mode": "backtest", "alpha": .05,
                "_fit_rows": fit, "_heldout_rows": heldout,
            }

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(factory_module, "ProcessPoolExecutor",
                              side_effect=OSError), \
                 patch.object(factory_module, "_diagnose_worker",
                              side_effect=fake_diagnose), \
                 patch.object(factory_module, "_worker",
                              side_effect=fake_worker), \
                 patch.object(factory_module, "_gate",
                              side_effect=fake_gate):
                result = run_factory(
                    rows, db_path=Path(directory) / "edge.sqlite3",
                    strategies=3, variants_per_strategy=2, workers=1,
                    min_trades=1, min_sessions=1, alpha=.05)
        target = next(item for item in result["results"]
                      if item["gate"]["p_raw"] == .01)
        self.assertTrue(target["gate"]["multiple_tests"]["significant"])
        self.assertFalse(target["gate"]["global_multiple_tests"]["significant"])
        self.assertTrue(target["gate"]["checks_without_family"])
        self.assertFalse(target["gate"]["passes"])
        # A development-only family pass is diagnostic. It does not consume a
        # sealed window or create an authoritative EdgeLedger proof run when
        # the cycle-global correction rejects it.
        self.assertIsNone(target["run_id"])
        self.assertFalse(target["gate"]["post_selection"][
            "qualification_consumed"])
        self.assertIsNone(result["post_selection"]["selected_test_id"])
        self.assertEqual(result["cumulative_fdr_state"]["tests"], 0)
        self.assertFalse(result["worker_failures"])

    def test_rule_grammar_is_bounded_and_content_addressed(self):
        spec = validate_rule_spec({"family": "mean_reversion", "lookback": 10,
                                   "slow_lookback": 30})
        self.assertTrue(rule_variant_id(spec).startswith("rule.mean-reversion."))
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({"family": "mean_reversion", "python": "import os"})
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({"family": "invented_alpha"})

    def test_initial_catalog_covers_all_eleven_rule_families(self):
        hypotheses = initial_hypotheses()
        self.assertEqual(len(hypotheses), 11)
        self.assertEqual(len({item.family for item in hypotheses}), 11)
        self.assertEqual(len({item.hypothesis_id for item in hypotheses}), 11)
        with self.assertRaisesRegex(FactoryError, "between 1 and 11"):
            initial_hypotheses(12)

    def test_generation_budget_must_be_positive(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                    FactoryError, "max_generations must be at least 1"):
                run_factory(
                    losing_breakouts(), db_path=Path(directory) / "edge.sqlite3",
                    strategies=1, variants_per_strategy=2, workers=1,
                    min_trades=1, min_sessions=1, alpha=1.0,
                    max_generations=0)

    def test_diagnosis_drives_bounded_mutations(self):
        root = initial_hypotheses(1)[0].rule_spec
        variants = mutate_from_diagnosis(
            root, {"primary_failure": "negative_expectancy"}, count=4)
        self.assertEqual(len(variants), 4)
        self.assertEqual(variants[0], root)
        self.assertEqual(len({rule_variant_id(item) for item in variants}), 4)
        saturated = validate_rule_spec({**root, "threshold_bps": 500.0,
                                        "target_r": 10.0})
        expanded = mutate_from_diagnosis(
            saturated, {"primary_failure": "negative_expectancy"}, count=8)
        self.assertEqual(len(expanded), 8)
        self.assertEqual(len({rule_variant_id(item) for item in expanded}), 8)

    def test_underpowered_variant_prevents_family_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "edge.sqlite3"
            result = run_factory(
                losing_breakouts(), db_path=db, strategies=1,
                variants_per_strategy=4, workers=1,
                min_trades=1, min_sessions=1, alpha=1.0,
                max_generations=3)
            self.assertEqual(result["strategies"], 1)
            self.assertEqual(result["variants"], 4)
            self.assertEqual(result["accounts"], 4)
            self.assertEqual(len({row["account_id"] for row in result["results"]}), 4)
            self.assertFalse(result["replacements"])
            self.assertTrue(any(not row["gate"]["sample_adequate"]
                                for row in result["results"]))
            status = FactoryLedger(db).status()
            self.assertEqual(status["accounts"], 4)
            self.assertEqual([row["status"] for row in status["hypotheses"]], ["queued"])

    def test_all_adequate_variants_replace_deterministically_and_generation_cap_stays_pending(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError), \
                patch.object(factory_module, "_worker", side_effect=fake_adequate_worker):
            db = Path(directory) / "edge.sqlite3"
            result = run_factory(
                losing_breakouts(), db_path=db, strategies=2,
                variants_per_strategy=2, workers=2,
                min_trades=1, min_sessions=1, alpha=1.0, max_generations=2)
            self.assertEqual(
                [(row["hypothesis_id"], row["variant_id"]) for row in result["results"]],
                sorted((row["hypothesis_id"], row["variant_id"])
                       for row in result["results"]))
            self.assertEqual(len(result["replacements"]), 2)
            self.assertTrue(all(item["not_before"] == "2026-01-14"
                                for item in result["replacements"]))

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError), \
                patch.object(factory_module, "_worker", side_effect=fake_adequate_worker):
            db = Path(directory) / "edge.sqlite3"
            pending = run_factory(
                losing_breakouts(), db_path=db, strategies=1,
                variants_per_strategy=2, workers=1,
                min_trades=1, min_sessions=1, alpha=1.0, max_generations=1)
            self.assertEqual(pending["status"], "pending_replacement_capacity")
            self.assertTrue(pending["pending"])
            self.assertEqual(FactoryLedger(db).active("equity")[0]["status"],
                             "pending_generation_limit")

    def _exhausted_slots(self, db, **kwargs):
        """Run one cycle that mutates, then one that hits the generation cap."""
        run_factory(losing_breakouts(), db_path=db, strategies=2,
                    variants_per_strategy=2, workers=2, min_trades=1,
                    min_sessions=1, alpha=1.0, max_generations=2)
        return run_factory(losing_breakouts(sessions=30), db_path=db,
                           strategies=2, variants_per_strategy=2, workers=2,
                           min_trades=1, min_sessions=1, alpha=1.0,
                           max_generations=2, **kwargs)

    def test_false_discovery_correction_spans_every_family_in_the_cycle(self):
        # Selection compares candidates across families, so the correction that
        # authorizes a champion has to be corrected over the same set.  A
        # per-family correction alone leaves the cross-family selection effect
        # uncorrected no matter how strict each family's own batch looks.
        scopes = []
        real = factory_module.benjamini_hochberg

        def recording(values, **kwargs):
            scopes.append(tuple(sorted(values)))
            return real(values, **kwargs)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError), \
                patch.object(factory_module, "_worker", side_effect=fake_adequate_worker), \
                patch.object(factory_module, "benjamini_hochberg", side_effect=recording):
            db = Path(directory) / "edge.sqlite3"
            run_factory(losing_breakouts(), db_path=db, strategies=2,
                        variants_per_strategy=2, workers=2, min_trades=1,
                        min_sessions=1, alpha=1.0, max_generations=2)

        # Family-local batches are keyed by variant alone; the global batch is
        # keyed "hypothesis:variant" and must carry every variant in the cycle.
        globals_ = [keys for keys in scopes
                    if keys and all(":" in key for key in keys)]
        self.assertTrue(globals_, "no cycle-global correction was applied")
        widest = max(globals_, key=len)
        families = {key.split(":", 1)[0] for key in widest}
        self.assertGreater(len(families), 1)
        locals_ = [keys for keys in scopes if keys and keys not in globals_]
        for keys in locals_:
            self.assertLess(len(keys), len(widest))

    def test_generation_exhaustion_rotates_one_fresh_family_per_cycle(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError), \
                patch.object(factory_module, "_worker", side_effect=fake_adequate_worker):
            db = Path(directory) / "edge.sqlite3"
            result = self._exhausted_slots(db)
            # Both slots are exhausted; the per-cycle budget rotates one and
            # leaves the other pending until the next cycle.
            self.assertEqual(len(result["rotations"]), 1)
            self.assertEqual([row["reason"] for row in result["pending"]],
                             ["generation_limit"])
            seed = result["rotations"][0]
            ledger = FactoryLedger(db)
            parent = ledger.hypothesis(seed["parent_hypothesis_id"])
            self.assertEqual(parent["status"], "retired")
            self.assertEqual(seed["slot"], parent["slot"])
            self.assertEqual(seed["generation"], parent["generation"] + 1)
            self.assertNotEqual(seed["family"], parent["family"])
            self.assertEqual(ledger.hypothesis(seed["hypothesis_id"])["status"],
                             "queued")
            self.assertIn(seed["hypothesis_id"],
                          {item["hypothesis_id"] for item in ledger.active("equity")})
            rotation = [event for event in ledger.events(parent["hypothesis_id"])
                        if event["payload"].get("rotation") is True]
            self.assertEqual(len(rotation), 1)
            self.assertEqual(rotation[0]["status"], "retired")
            self.assertEqual(rotation[0]["payload"]["rotation_index"], 0)

    def test_rotation_can_be_disabled_and_keeps_the_pending_slot_behavior(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError), \
                patch.object(factory_module, "_worker", side_effect=fake_adequate_worker):
            db = Path(directory) / "edge.sqlite3"
            result = self._exhausted_slots(db, max_rotations=0)
            self.assertEqual(result["rotations"], [])
            self.assertEqual(result["status"], "pending_replacement_capacity")
            self.assertEqual(
                {item["status"] for item in FactoryLedger(db).active("equity")},
                {"pending_generation_limit"})

    def test_rotation_bounds_are_not_configurable_upwards(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "edge.sqlite3"
            for kwargs in ({"rotation_budget": 5}, {"max_rotations": 9},
                           {"max_rotations": -1}):
                with self.subTest(**kwargs), self.assertRaises(FactoryError):
                    run_factory(losing_breakouts(), db_path=db, strategies=1,
                                variants_per_strategy=2, workers=1, **kwargs)

    def test_worker_failure_requeues_and_does_not_freeze_the_cycle(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError), \
                patch.object(factory_module, "_worker", side_effect=RuntimeError("boom")):
            db = Path(directory) / "edge.sqlite3"
            result = run_factory(
                losing_breakouts(), db_path=db, strategies=1,
                variants_per_strategy=2, workers=1,
                min_trades=1, min_sessions=1)
            self.assertEqual(result["status"], "partial_worker_failure")
            status = FactoryLedger(db).status()
            self.assertEqual(status["cycles"], 0)
            self.assertEqual(status["hypotheses"][0]["status"], "queued")

    def test_llm_replacement_is_validated_registered_then_parent_retires(self):
        proposed = validate_rule_spec({
            "family": "volume_breakout", "lookback": 9,
            "slow_lookback": 25, "threshold_bps": 12,
            "confirmation": "trend",
        })

        class Adapter:
            def propose(self, **kwargs):
                return ProposalResult(
                    True, schema=PROPOSAL_SCHEMA, rule_spec=proposed,
                    variant_id=rule_variant_id(proposed),
                    spec_id=rule_spec_hash(proposed),
                    evidence={"provider": "openai", "model": "test-model",
                              "request_hash": "r" * 64,
                              "raw_response_hash": "x" * 64,
                              "normalized_spec_hash": rule_spec_hash(proposed)})

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError), \
                patch.object(factory_module, "_worker", side_effect=fake_adequate_worker):
            db = Path(directory) / "edge.sqlite3"
            result = run_factory(
                losing_breakouts(), db_path=db, strategies=1,
                variants_per_strategy=2, workers=1,
                min_trades=1, min_sessions=1, alpha=1.0,
                max_generations=2,
                strategy_llm={"enabled": True, "provider": "openai",
                              "model": "test-model"},
                proposal_adapter=Adapter())
            self.assertEqual(len(result["replacements"]), 1)
            self.assertEqual(result["replacements"][0]["rule_spec"], proposed)
            ledger = FactoryLedger(db)
            hypotheses = ledger.hypotheses(vehicle="equity")
            parent = next(item for item in hypotheses if item["generation"] == 0)
            child = next(item for item in hypotheses if item["generation"] == 1)
            self.assertEqual(parent["status"], "retired")
            self.assertEqual(child["parent_hypothesis_id"], parent["hypothesis_id"])
            evidence_events = [event for event in ledger.events(parent["hypothesis_id"])
                               if event["payload"].get("llm_evidence")]
            self.assertTrue(evidence_events)
            self.assertEqual(
                evidence_events[-1]["payload"]["replacement_hypothesis_id"],
                child["hypothesis_id"])

    def test_llm_failure_keeps_parent_active_and_direct_retire_is_rejected(self):
        class Adapter:
            def propose(self, **kwargs):
                return ProposalResult(
                    False, error="invalid proposal",
                    evidence={"provider": "openai", "model": "test-model",
                              "request_hash": "r" * 64})

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError), \
                patch.object(factory_module, "_worker", side_effect=fake_adequate_worker):
            db = Path(directory) / "edge.sqlite3"
            result = run_factory(
                losing_breakouts(), db_path=db, strategies=1,
                variants_per_strategy=2, workers=1,
                min_trades=1, min_sessions=1, alpha=1.0,
                max_generations=2,
                strategy_llm={"enabled": True, "provider": "openai",
                              "model": "test-model"},
                proposal_adapter=Adapter())
            self.assertFalse(result["replacements"])
            self.assertEqual(result["pending"][0]["reason"],
                             "llm_proposal_failed")
            ledger = FactoryLedger(db)
            parent = ledger.hypotheses(vehicle="equity")[0]
            self.assertEqual(parent["status"], "pending_llm_replacement")
            with self.assertRaisesRegex(Exception, "retirement requires"):
                ledger.event(parent["hypothesis_id"], "retired", "manual")

    def test_configured_seven_strategy_shape_runs_fourteen_isolated_arms(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_factory(
                losing_breakouts(3), db_path=Path(directory) / "edge.sqlite3",
                strategies=7, variants_per_strategy=2, workers=7,
                min_trades=50, min_sessions=10, alpha=.05)
            self.assertEqual(result["parallel_workers"], 7)
            self.assertEqual(result["strategies"], 7)
            self.assertEqual(result["variants"], 14)
            self.assertEqual(result["accounts"], 14)
            self.assertEqual(len({row["account_id"] for row in result["results"]}), 14)

    def test_validated_generated_rule_is_the_only_runtime_activation_path(self):
        spec = validate_rule_spec({"family": "momentum_continuation",
                                   "lookback": 5, "slow_lookback": 10})
        variant_id = rule_variant_id(spec)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "edge.sqlite3"
            ledger = EdgeLedger(db)
            runtime_config = validate_config({
                "strategy": {"id": "rule", "version": "v1",
                             "variant_id": "auto"},
            })
            assumptions = candidate_assumptions(
                runtime_config, vehicle="equity", strategy_id="rule",
                variant_id=variant_id, rule_spec=spec)
            candidate = ledger.register_candidate(
                variant_id, strategy_id="rule", vehicle="equity",
                hypothesis="bounded momentum",
                config=assumptions,
                axes={"hypothesis_id": "test"})
            row = {"vehicle": "equity", "session_date": "2026-01-05",
                   "opportunity_id": "test", "net_pnl": 1.0,
                   "return_value": .001}
            for lane in ("backtest", "shadow"):
                persist_rule_gate(ledger, candidate["candidate_id"], lane)
                if lane == "backtest":
                    ledger.transition(candidate["candidate_id"], "backtest_passed",
                                      reason="held-out gate passed")
                else:
                    ledger.transition(candidate["candidate_id"], "shadow",
                                      reason="forward gate passed")
                    ledger.transition(candidate["candidate_id"], "validated",
                                      reason="both gates passed")
            config = validate_config({"strategy": {"id": "rule", "version": "v1",
                                                    "variant_id": "auto"}})
            record = resolve_validated_variant(config, db_path=db)
            applied = apply_variant(config, record)
            self.assertEqual(applied["strategy"]["variant_id"], variant_id)
            self.assertEqual(applied["strategy"]["rule_spec"], spec)


class CorpusDescriptorTests(unittest.TestCase):
    """A worker re-reading its own sessions computes exactly what a copy did."""

    def setUp(self):
        # Corpus-descriptor tests intentionally use a compact losing-breakout
        # fixture.  Keep the production factory floor strict while allowing
        # these backend-equivalence checks to exercise the descriptor logic.
        self.compact_protocol = patch.multiple(
            gates,
            PROTOCOL_BACKTEST_MIN_TRADES=1,
            PROTOCOL_BACKTEST_MIN_SESSIONS=1,
            PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
            PROTOCOL_SHADOW_MIN_TRADES=1,
            PROTOCOL_SHADOW_MIN_SESSIONS=1,
            PROTOCOL_SHADOW_MIN_CLUSTERS=1,
            PROTOCOL_QUALIFICATION_MIN_TRADES=1,
            PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
            PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1,
        )
        self.compact_protocol.start()
        self.addCleanup(self.compact_protocol.stop)

    @staticmethod
    def _write(root: Path, rows, *, partitioned: bool) -> Path:
        if not partitioned:
            target = root / "corpus.jsonl"
            target.write_text("".join(json.dumps(row, sort_keys=True) + "\n"
                                      for row in rows), encoding="utf-8")
            return target
        directory = root / "sessions"
        directory.mkdir()
        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row["timestamp"][:10], []).append(row)
        for session, session_rows in grouped.items():
            (directory / f"{session}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in session_rows),
                encoding="utf-8")
        return directory

    def test_a_reread_partition_reproduces_the_sliced_books_exactly(self):
        rows = losing_breakouts()
        _, bars, snapshot_map, quotes = factory_module._read_discovery_rows(rows)
        sealed = {factory_module._session(bar) for bar in bars[-40:]}
        expected = ([bar for bar in bars if factory_module._session(bar) not in sealed],
                    [snap for snap in snapshot_map.values()
                     if snap.session_date.isoformat() not in sealed],
                    [quote for quote in quotes
                     if quote.session_date.isoformat() not in sealed])
        end = max(factory_module._session(bar) for bar in bars)
        with tempfile.TemporaryDirectory() as directory:
            for partitioned in (False, True):
                root = Path(directory) / f"corpus-{partitioned}"
                root.mkdir()
                source = self._write(root, rows, partitioned=partitioned)
                self.assertEqual(
                    factory_module.corpus_slice(source, after=None, until=end,
                                                exclude=sorted(sealed)),
                    expected)

    def test_both_task_shapes_drive_one_identical_worker_result(self):
        rows = losing_breakouts()
        _, bars, snapshot_map, quotes = factory_module._read_discovery_rows(rows)
        hypothesis = {**vars(initial_hypotheses(1, vehicle="equity")[0]),
                      "status": "queued"}
        end = max(factory_module._session(bar) for bar in bars)
        sealed = sorted({factory_module._session(bar) for bar in bars[-40:]})
        common = {"hypothesis": hypothesis, "vehicle": "equity", "mode": "backtest",
                  "existing_specs": [], "variants_per_strategy": 2,
                  "starting_cash": 100_000.0, "costs": factory_module.CostModel()}
        with tempfile.TemporaryDirectory() as directory:
            source = self._write(Path(directory), rows, partitioned=False)
            copied, reread = ({**common, "bars": [
                bar for bar in bars if factory_module._session(bar) not in set(sealed)],
                "snapshots": [snap for snap in snapshot_map.values()
                              if snap.session_date.isoformat() not in set(sealed)],
                "quotes": [quote for quote in quotes
                           if quote.session_date.isoformat() not in set(sealed)]},
                {**common, "corpus": {"source": str(source), "after": None,
                                      "until": end, "exclude": sealed}})
            # Both phases must agree across both task shapes: the diagnosis the
            # orchestrator tunes from, and the evaluation it schedules after.
            diagnoses = [factory_module._diagnose_worker(task)["diagnostic"]
                         for task in (copied, reread)]
            self.assertEqual(diagnoses[0], diagnoses[1])
            specs = factory_module.mutate_from_diagnosis(
                hypothesis["rule_spec"], diagnoses[0], 2)
            for task, diagnostic in zip((copied, reread), diagnoses):
                task["diagnostic"] = diagnostic
                task["specs"] = specs
            # The account ids carry a random suffix; pin it so the comparison is
            # of the computed evidence, not of two fresh uuids.
            with patch.object(factory_module.uuid, "uuid4",
                              return_value=uuid.UUID(int=7)):
                first = factory_module._worker(copied)
                second = factory_module._worker(reread)
        for result in (first, second):
            result.pop("worker_pid")
            for variant in result["variants"]:
                variant.pop("worker_pid")
        self.assertEqual(first, second)
        self.assertTrue(first["variants"][0]["account"]["rows"])

    def test_a_path_corpus_produces_the_same_cycle_on_both_backends(self):
        rows = losing_breakouts()
        results = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, rows, partitioned=False)
            results["memory"] = run_factory(
                rows, db_path=root / "memory.sqlite3", strategies=1,
                variants_per_strategy=2, workers=1, min_trades=1,
                min_sessions=1, alpha=1.0)
            results["process"] = run_factory(
                source, db_path=root / "process.sqlite3", strategies=1,
                variants_per_strategy=2, workers=1, min_trades=1,
                min_sessions=1, alpha=1.0)
            with patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError):
                results["thread"] = run_factory(
                    source, db_path=root / "thread.sqlite3", strategies=1,
                    variants_per_strategy=2, workers=1, min_trades=1,
                    min_sessions=1, alpha=1.0)
        # Restricted CI/sandbox hosts can deny the POSIX primitives required
        # to construct a process pool.  That is the production code's explicit
        # thread-fallback contract, not evidence divergence; on capable hosts
        # this same assertion still exercises the real process backend.
        self.assertIn(results["process"]["parallel_backend"],
                      {"process", "thread_fallback"})
        self.assertEqual(results["thread"]["parallel_backend"], "thread_fallback")

        def evidence(result):
            return (result["dataset_hash"],
                    [(row["variant_id"], row["gate"]["gate_hash"],
                      row["gate"]["p_raw"], row["gate"]["heldout_net_pnl"],
                      row["status"]) for row in result["results"]])

        self.assertEqual(evidence(results["memory"]), evidence(results["process"]))
        self.assertEqual(evidence(results["memory"]), evidence(results["thread"]))


if __name__ == "__main__":
    unittest.main()
