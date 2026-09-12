"""Complete inventory diagnostics cannot authorize or invent missing returns."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.config import load_config
from research import diagnostic_suite as suite
from research.diagnostic_shadow import build_diagnostic_cohort
from research.mechanism_cohort import mechanism_cohort


ROOT = Path(__file__).resolve().parents[2]


class CompleteDiagnosticSuiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load_config(ROOT / "config.yaml")
        self.data = self.root / "market.jsonl"
        self.row = {
            "kind": "bar", "symbol": "SPY", "provider": "alpaca", "feed": "iex",
            "source_mode": "historical_backfill",
            "timestamp": "2026-01-02T14:30:00+00:00",
            "as_of": "2026-01-02T14:31:00+00:00",
            "observed_at": "2026-01-03T00:00:00+00:00",
            "open": 100, "high": 100.1, "low": 99.9, "close": 100,
            "volume": 1000,
        }
        self.data.write_text(json.dumps(self.row) + "\n", encoding="utf-8")
        self.output = self.root / "results.json"

    def tearDown(self):
        suite._close_worker()
        self.tmp.cleanup()

    def test_inventory_resolves_exact_existing_ids_and_overrides(self):
        before = deepcopy(self.config)
        actual = suite.build_inventory(self.config, code_hash="a" * 64)
        shadow = build_diagnostic_cohort(self.config, code_identity="a" * 64)
        mechanisms = mechanism_cohort()
        expected = {item["variant_id"] for item in shadow["arms"]}
        expected.update(item["variant_id"] for family in mechanisms["families"]
                        for item in family["arms"])
        expected.update({"ibr.baseline", "ibr.range.30", "ibr.range.45",
                         "ibr.target.1_5r", "ibr.target.3r", "ibr.buffer.0bps",
                         "ibr.buffer.10bps"})
        self.assertEqual(len(expected), 43)
        self.assertEqual({item["variant_id"] for item in actual["arms"]}, expected)
        self.assertEqual(actual["cohort_counts"], {
            "diagnostic_shadow": 24, "intraday-mechanisms.v1": 12, "ibr_registry": 7})
        arm = next(item for item in actual["arms"] if item["variant_id"] == "ibr.range.45")
        self.assertEqual(arm["overrides"], {"strategy.range_minutes": 45})
        self.assertEqual(self.config, before)

    def test_missing_evidence_never_becomes_a_negative_point_estimate(self):
        base = {"executed_trades": 0, "measured_net_expectancy": 0.0}
        self.assertEqual(suite.classify(base), "insufficient_observations")
        self.assertEqual(suite.classify({**base, "data_rejection_count": 1}), "missing_data")
        self.assertEqual(suite.classify({**base, "no_signal_count": 2}), "no_signal")
        self.assertEqual(suite.classify({**base, "signal_execution_rejection_count": 2}),
                         "execution_blocked")
        self.assertEqual(suite.classify({"executed_trades": 1,
                         "evidence_status": "insufficient_trade_sample",
                         "measured_net_expectancy": 100}), "underpowered")
        self.assertEqual(suite.classify({"executed_trades": 20,
                         "measured_net_expectancy": -2}), "negative_point_estimate")

    def test_preflight_exposes_incompatibility_without_changing_policy(self):
        before = deepcopy(self.config)
        result = suite.admission_preflight(self.config)
        self.assertAlmostEqual(result["fixed_scenario_minimum_stop_bps"], 25.0 / .30)
        self.assertEqual(result["rule_grammar_stop_floor_bps"], 30.0)
        self.assertEqual(result["static_bar_round_trip_cost_bps"], 17.0)
        self.assertTrue(result["grammar_floor_below_fixed_scenario_requirement"])
        self.assertFalse(result["plan_mutation"])
        self.assertFalse(result["costs_measured"])
        self.assertEqual(self.config, before)
        self.config["risk"]["max_stressed_cost_to_risk_ratio"] = 0.0
        disabled = suite.admission_preflight(self.config)
        self.assertTrue(disabled["no_finite_stop_admissible"])
        self.assertIsNone(disabled["fixed_scenario_minimum_stop_bps"])
        json.dumps(disabled, allow_nan=False)

    def test_small_real_survey_is_complete_non_authorizing_and_immutable(self):
        self.config["broker"]["api_key"] = "secret-must-not-be-persisted"
        self.config["broker"]["secret_key"] = "another-secret-must-not-be-persisted"
        calls = []
        def progress(done, total, variant):
            self.assertTrue(self.output.with_name("results.json.manifest.json").exists())
            calls.append((done, total, variant))
        with patch.object(suite, "_code_bundle_hash", return_value="a" * 64), \
                patch("research.edge_ledger.EdgeLedger", side_effect=AssertionError("ledger opened")), \
                patch("research.strategy_factory.run_factory", side_effect=AssertionError("factory ran")), \
                patch.object(suite, "measure_fit_diagnostics",
                             side_effect=AssertionError("full fit diagnostics ran")):
            result = suite.run_suite(self.data, runtime_config=self.config,
                                     output=self.output, workers=1,
                                     diagnostic_only=True, progress=progress)
        self.assertEqual(len(result["results"]), 43)
        self.assertEqual(len(calls), 43)
        self.assertEqual(sum(result["outcome_counts"].values()), 43)
        self.assertFalse(result["authorizing"])
        self.assertEqual(result["eligible"], [])
        self.assertEqual(result["proofs"], [])
        self.assertIsNone(result["portfolio_pnl"])
        self.assertFalse(result["full_fit_diagnostics"])
        self.assertEqual(result["cost_calibration"]["reason"], "no_contemporaneous_quotes")
        for arm in result["results"]:
            diagnostic = (arm["legacy_comparison"]["diagnostic"]
                          if arm["strategy_id"] == "ibr" else arm["diagnostic"])
            self.assertEqual(diagnostic["executed_trades"], 0)
            self.assertIsNone(diagnostic["measured_net_expectancy"])
            self.assertIsNone(diagnostic["gross_pnl"])
            self.assertNotIn(arm["outcome"], {"positive_point_estimate", "negative_point_estimate"})
            self.assertFalse(arm["authorizing"])
            self.assertIsNone(arm["fit_diagnostics"])
            self.assertEqual(arm["fit_diagnostics_status"]["status"], "not_requested")
            self.assertFalse(arm["fit_diagnostics_status"]["complete_prefix_audit"])
            self.assertIn("neither negative evidence", arm["fit_diagnostics_status"]["interpretation"])
            if arm["strategy_id"] == "ibr":
                self.assertEqual(arm["outcome"], "unavailable")
                self.assertIsNone(arm["diagnostic"])
                self.assertEqual(arm["rows"], [])
                self.assertIn("historical_source", arm["runtime_evidence"]["reason_codes"])
                self.assertTrue(arm["replay_scope"]["shared_signal_setup_risk"])
                self.assertFalse(arm["replay_scope"]["broker_equivalence"])
                self.assertEqual(arm["legacy_comparison"]["replay_scope"]["runtime_parity"],
                                 "partial")
        self.assertEqual(result["outcome_counts"]["unavailable"], 7)
        self.assertEqual(sum(result["legacy_comparison_outcome_counts"].values()), 7)
        self.assertEqual(result["ibr_runtime"]["status"], "unavailable")
        self.assertIsNone(result["ibr_runtime"]["diagnostic"])
        self.assertNotIn("arms", result["ibr_runtime"])
        manifest = json.loads(
            self.output.with_name("results.json.manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["full_fit_diagnostics"])
        self.assertEqual(manifest["ibr_runtime_contract"]["max_events"], 100_000)
        self.assertEqual(len(manifest["ibr_runtime_contract"]["arm_config_identities"]), 7)
        self.assertNotIn("secret-must-not-be-persisted", self.output.read_text())
        original = self.output.read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            suite.run_suite(self.data, runtime_config=self.config, output=self.output,
                            diagnostic_only=True)
        self.assertEqual(self.output.read_bytes(), original)

    def test_explicit_opt_in_and_worker_bounds(self):
        with self.assertRaisesRegex(ValueError, "explicit"):
            suite.run_suite(self.data, runtime_config=self.config, output=self.output)
        for workers in (0, 5, True, 1.5):
            with self.subTest(workers=workers), self.assertRaisesRegex(ValueError, "workers"):
                suite.run_suite(self.data, runtime_config=self.config, output=self.output,
                                diagnostic_only=True, workers=workers)
        self.assertFalse(self.output.exists())

    def test_runtime_ibr_uses_shared_forward_engine_and_preserves_legacy_comparison(self):
        # Synthetic prices verify plumbing and accounting, not market edge.
        from tests.research.test_ibr_diagnostic_adapter import _runtime, _source

        rows = _source()
        self.data.write_text("".join(json.dumps(row) + "\n" for row in rows),
                             encoding="utf-8")
        legacy = {}
        original_evaluate = suite._evaluate_arm
        original_forward = suite.run_offline_forward_ibr

        def evaluate(arm):
            result = original_evaluate(arm)
            if arm["strategy_id"] == "ibr":
                legacy[arm["variant_id"]] = deepcopy(result)
            return result

        def forward(*args, **kwargs):
            frozen = json.loads(self.output.with_name(
                "results.json.manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(frozen["ibr_runtime_contract"]["arm_config_identities"]), 7)
            return original_forward(*args, **kwargs)

        with patch.object(suite, "_code_bundle_hash", return_value="a" * 64), \
                patch.object(suite, "_evaluate_arm", side_effect=evaluate), \
                patch.object(suite, "run_offline_forward_ibr", side_effect=forward) as replay, \
                patch("research.live_shadow.ShadowStore", side_effect=AssertionError("store opened")), \
                patch("research.edge_ledger.EdgeLedger", side_effect=AssertionError("ledger opened")):
            result = suite.run_suite(self.data, runtime_config=_runtime(),
                                     output=self.output, diagnostic_only=True, workers=1)
        replay.assert_called_once()
        self.assertEqual(len(result["results"]), 43)
        self.assertEqual(len({arm["variant_id"] for arm in result["results"]}), 43)
        self.assertEqual(result["ibr_runtime"]["status"], "measured")
        for arm in result["results"]:
            if arm["strategy_id"] != "ibr":
                self.assertNotIn("legacy_comparison", arm)
                continue
            for key, value in arm["legacy_comparison"].items():
                self.assertEqual(value, legacy[arm["variant_id"]][key])
            self.assertFalse(arm["eligible"])
            self.assertFalse(arm["replay_scope"]["broker_equivalence"])
            self.assertTrue(arm["replay_scope"]["shared_cost_and_account_engine"])
        baseline = next(arm for arm in result["results"]
                        if arm["variant_id"] == "ibr.baseline")
        self.assertGreater(baseline["rows"][0]["quantity"], 1)
        self.assertAlmostEqual(baseline["rows"][0]["net_pnl"],
                               baseline["runtime_evidence"]["account"]["realized_pnl"])
        self.assertEqual(baseline["legacy_comparison"]["replay_scope"]["quantity_model"],
                         "fixed_shares")

    def test_invalid_ibr_budgets_fail_before_manifest_or_replay(self):
        with patch.object(suite, "_evaluate_arm") as replay:
            for value in (0, -1, True, 1.5, None):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "ibr_max_events"):
                    suite.run_suite(self.data, runtime_config=self.config, output=self.output,
                                    diagnostic_only=True, ibr_max_events=value)
        replay.assert_not_called()
        self.assertFalse(self.output.with_name("results.json.manifest.json").exists())

    def test_full_fit_diagnostics_calls_only_rule_arms_and_preserves_replays(self):
        default_output = self.root / "default.json"
        full_output = self.root / "full.json"
        measured = {"schema": "fit-diagnostics.test", "status": "measured"}
        with patch.object(suite, "_code_bundle_hash", return_value="a" * 64), \
                patch.object(suite, "measure_fit_diagnostics",
                             return_value=measured) as measure:
            default = suite.run_suite(
                self.data, runtime_config=self.config, output=default_output,
                workers=1, diagnostic_only=True)
            full = suite.run_suite(
                self.data, runtime_config=self.config, output=full_output,
                workers=1, diagnostic_only=True, full_fit_diagnostics=True)

        self.assertEqual(measure.call_count, 36)
        self.assertFalse(default["full_fit_diagnostics"])
        self.assertTrue(full["full_fit_diagnostics"])
        default_manifest = json.loads(default_output.with_name(
            "default.json.manifest.json").read_text(encoding="utf-8"))
        full_manifest = json.loads(full_output.with_name(
            "full.json.manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(default_manifest["full_fit_diagnostics"])
        self.assertTrue(full_manifest["full_fit_diagnostics"])
        self.assertNotEqual(default["manifest_hash"], full["manifest_hash"])

        def without_fit(result):
            return {key: value for key, value in result.items()
                    if key not in {"fit_diagnostics", "fit_diagnostics_status"}}

        self.assertEqual([without_fit(item) for item in default["results"]],
                         [without_fit(item) for item in full["results"]])
        for arm in full["results"]:
            if arm["strategy_id"] == "rule":
                self.assertEqual(arm["fit_diagnostics"], measured)
                self.assertEqual(arm["fit_diagnostics_status"]["status"], "measured")
                self.assertTrue(arm["fit_diagnostics_status"]["complete_prefix_audit"])
            else:
                self.assertIsNone(arm["fit_diagnostics"])
                self.assertEqual(arm["fit_diagnostics_status"]["status"],
                                 "not_applicable")

    def test_full_fit_diagnostics_rejects_non_boolean_values_before_replay(self):
        with patch.object(suite, "_evaluate_arm") as replay:
            for value in (0, 1, None, "true"):
                with self.subTest(value=value), \
                        self.assertRaisesRegex(ValueError, "full_fit_diagnostics"):
                    suite.run_suite(
                        self.data, runtime_config=self.config, output=self.output,
                        diagnostic_only=True, full_fit_diagnostics=value)
        replay.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_future_observation_fails_before_manifest_or_replay(self):
        self.row["observed_at"] = "2099-01-03T00:00:00+00:00"
        self.data.write_text(json.dumps(self.row) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError), patch.object(suite, "_evaluate_arm") as replay:
            suite.run_suite(self.data, runtime_config=self.config, output=self.output,
                            diagnostic_only=True, workers=1)
        replay.assert_not_called()
        self.assertFalse(self.output.with_name("results.json.manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
