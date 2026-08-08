from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from agent.config import validate_config
from agent.contracts.rule import (
    RuleSpecError, evaluate_rule_signal, rule_variant_id, validate_rule_spec,
)
from agent.edge import apply_variant, resolve_validated_variant
from research.edge_lab import EdgeLedger
from research.strategy_factory import (
    FactoryLedger, initial_hypotheses, mutate_from_diagnosis, run_factory,
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


class StrategyFactoryTests(unittest.TestCase):
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

    def test_diagnosis_drives_bounded_mutations(self):
        root = initial_hypotheses(1)[0].rule_spec
        variants = mutate_from_diagnosis(
            root, {"primary_failure": "negative_expectancy"}, count=4)
        self.assertEqual(len(variants), 4)
        self.assertEqual(variants[0], root)
        self.assertEqual(len({rule_variant_id(item) for item in variants}), 4)

    def test_parallel_cycle_uses_isolated_accounts_and_queues_replacement(self):
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
            self.assertTrue(result["replacements"])
            boundary = result["replacements"][0]["not_before"]
            self.assertEqual(boundary, result["results"][0]["evaluation_end"])
            status = FactoryLedger(db).status()
            self.assertEqual(status["accounts"], 4)
            self.assertEqual(
                [row["status"] for row in status["hypotheses"]],
                ["retired", "queued"])
            forward = run_factory(
                losing_breakouts(15), db_path=db, strategies=1,
                variants_per_strategy=2, workers=1,
                min_trades=50, min_sessions=10, alpha=.05,
                max_generations=3)
            self.assertTrue(forward["results"])
            self.assertTrue(all(row["evaluation_start"] > boundary
                                for row in forward["results"]))

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
                run = ledger.append_run(
                    candidate["candidate_id"], lane=lane,
                    fit=[] if lane == "shadow" else [row], heldout=[row],
                    metrics={"gate": {"passes": True}, "confidence": 1.0,
                             "heldout_ci_low": 1.0, "heldout_trades": 1,
                             "max_drawdown": 0.0})
                ledger.append_trade(run["run_id"], {**row, "opportunity_id": lane})
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
