from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent.config import validate_config
from agent.contracts.rule import (
    RuleSpecError, evaluate_rule_signal, rule_spec_hash, rule_variant_id,
    validate_rule_spec,
)
from agent.edge import apply_variant, resolve_validated_variant
from research.edge_lab import EdgeLedger
from research.gates import (
    falsification_gate, heldout_separation, matched_cluster_test,
    performance_floor, placebo_null_distribution, structural_floor,
    verified_gate_envelope,
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
    specs = mutate_from_diagnosis(
        hypothesis["rule_spec"], {"primary_failure": "negative_expectancy"},
        int(payload["variants_per_strategy"]))
    # Twenty sessions: the held-out partition must be large enough to support
    # rolling-origin folds, otherwise the variant is under-powered rather than
    # adequately failed and the family is never eligible for replacement.
    sessions = [f"2026-01-{day:02d}" for day in range(5, 25)]
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
                 "net_pnl": -1.0, "return_value": -.00001}
                for row in control_rows]
        variants.append({
            "variant_id": variant_id, "rule_spec": spec, "vehicle": payload["vehicle"],
            "account": {"account_id": f"account:{hypothesis['hypothesis_id']}:{variant_id}",
                        "starting_cash": payload["starting_cash"],
                        "ending_equity": payload["starting_cash"] - len(rows),
                        "realized_pnl": -float(len(rows)), "max_drawdown": float(len(rows)),
                        "trades": len(rows), "rows": rows},
            "diagnostic": {"primary_failure": "negative_expectancy", "net_pnl": -len(rows)},
            "worker_pid": int(hypothesis["slot"]) + 100,
        })
    return {"hypothesis": hypothesis, "mode": payload["mode"],
            "diagnostic": {"primary_failure": "negative_expectancy", "net_pnl": -6},
            "evaluation_start": sessions[0], "evaluation_end": sessions[-1],
            "variants": list(reversed(variants)), "control_rows": control_rows,
            "expected_variants": len(specs), "worker_pid": int(hypothesis["slot"]) + 100}


def persist_rule_gate(ledger, candidate_id, lane):
    candidate = ledger.candidate(candidate_id)
    candidate_config = json.loads(candidate["config_json"])
    fit = [] if lane == "shadow" else [
        {"vehicle": "equity", "session_date": "2026-01-05",
         "opportunity_id": f"{lane}-fit", "net_pnl": 1.0}]
    # Eight held-out sessions are the minimum that can carry a sign-flip
    # falsification below alpha; two never could.
    heldout = [
        {"vehicle": "equity", "symbol": "SPY", "session_date": f"2026-01-{day:02d}",
         "opportunity_id": f"{lane}-held-{day}", "net_pnl": 1.0}
        for day in range(6, 14)]
    baseline = [{**row, "net_pnl": 0.0, "opportunity_id": f"base-{index}"}
                for index, row in enumerate(heldout)]
    fit_floor = structural_floor(
        fit, vehicle="equity", min_trades=1, min_sessions=1,
        required=lane != "shadow")
    held_floor = structural_floor(
        heldout, vehicle="equity", min_trades=1, min_sessions=1)
    separation = (heldout_separation(fit, heldout) if lane == "backtest" else
                  {"passes": True, "mode": "new_data"})
    control = matched_cluster_test(heldout, baseline, vehicle="equity")
    placebo = placebo_null_distribution(heldout, baseline, vehicle="equity")
    falsification = {
        **falsification_gate(placebo["observed"], placebo["placebo"]),
        "draws": int(placebo["draws"]), "seed": int(placebo["seed"])}
    absolute = performance_floor(heldout, vehicle="equity")
    gate = verified_gate_envelope(
        lane=lane, vehicle="equity", fit=fit, heldout=heldout,
        fit_floor=fit_floor, heldout_floor=held_floor,
        control={**control, "kind": "matched_root_baseline"},
        p_value=control["p_value"], q_value=.01, alpha=.05,
        falsification=falsification, separation=separation,
        checks={"family_fdr_significant": True, "global_fdr_significant": True,
                "falsification": bool(falsification["passes"]),
                "heldout_net_pnl_positive": bool(absolute["net_pnl_positive"]),
                "heldout_expectancy_positive": bool(absolute["expectancy_positive"]),
                "heldout_delta_lcb_positive": bool(control["mean_delta_lcb"] > 0)},
        passes=True,
        performance={"heldout_delta": control["mean_delta"],
                     "heldout_delta_lcb": control["mean_delta_lcb"],
                     "heldout_net_pnl": absolute["net_pnl"],
                     "heldout_expectancy": absolute["expectancy"],
                     "max_drawdown": 0.0})
    run = ledger.append_run(candidate_id, lane=lane, fit=fit, heldout=heldout,
                            config=candidate_config,
                            metrics={"gate": {"passes": True}, "confidence": .99,
                                     "heldout_delta": 1.0, "max_drawdown": 0.0,
                                     "heldout_trades": len(heldout)})
    for row in [*fit, *heldout]:
        ledger.append_trade(run["run_id"], row)
    ledger.record_verified_gate(run["run_id"], gate)


class StrategyFactoryTests(unittest.TestCase):
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

    def test_rule_grammar_is_bounded_and_content_addressed(self):
        spec = validate_rule_spec({"family": "mean_reversion", "lookback": 10,
                                   "slow_lookback": 30})
        self.assertTrue(rule_variant_id(spec).startswith("rule.mean-reversion."))
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({"family": "mean_reversion", "python": "import os"})
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({"family": "invented_alpha"})

    def test_initial_catalog_has_seven_distinct_hypotheses(self):
        hypotheses = initial_hypotheses()
        self.assertEqual(len(hypotheses), 7)
        self.assertEqual(len({item.family for item in hypotheses}), 7)
        self.assertEqual(len({item.hypothesis_id for item in hypotheses}), 7)
        with self.assertRaisesRegex(FactoryError, "between 1 and 7"):
            initial_hypotheses(8)

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
            self.assertTrue(all(item["not_before"] == "2026-01-24"
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

    def test_default_seven_strategy_shape_runs_fourteen_isolated_arms(self):
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
            candidate = ledger.register_candidate(
                variant_id, strategy_id="rule", vehicle="equity",
                hypothesis="bounded momentum",
                config={"strategy": {"id": "rule", "version": "v1",
                                     "variant_id": variant_id, "rule_spec": spec}},
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


if __name__ == "__main__":
    unittest.main()
