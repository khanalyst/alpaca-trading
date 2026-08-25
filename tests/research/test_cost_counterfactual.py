"""Frozen stressed-cost counterfactual regressions."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.cost_counterfactual import (load_frozen_specs, main,
                                          run_counterfactual)
from research.edge_lab import content_hash
from research.factory_core import initial_hypotheses


class CostCounterfactualTests(unittest.TestCase):
    def test_same_frozen_variant_is_measured_without_authorization(self):
        spec = initial_hypotheses(1)[0].rule_spec
        seen = []

        def simulate(_bars, _snapshots, frozen, **kwargs):
            ratio = kwargs["policy"].max_stressed_cost_to_risk_ratio
            seen.append((frozen, ratio))
            if ratio == .30:
                rows = [{
                    "opportunity_id": "same-opportunity",
                    "session_date": "2026-01-02",
                    "execution_disposition": "refused",
                    "signal_opportunity": True,
                    "no_trade": True,
                    "reject_stage": "cost_stress",
                    "reject_reason": "stressed_cost_risk_limit",
                }]
            else:
                rows = [{
                    "opportunity_id": "same-opportunity",
                    "session_date": "2026-01-02",
                    "execution_disposition": "executed",
                    "signal_opportunity": True,
                    "no_trade": False,
                    "net_pnl": 5.0,
                    "return_value": .1,
                }]
            return {"rows": rows}

        config = {
            "broker": {"data_feed": "iex"},
            "risk": {
                "stressed_cost_scenario_bps": 25.0,
                "max_stressed_cost_to_risk_ratio": .30,
            },
        }
        with patch("research.cost_counterfactual._read_discovery_rows",
                   return_value=([{"kind": "bar"}], [], {}, [])), \
             patch("research.cost_counterfactual.simulate_account",
                   side_effect=simulate):
            result = run_counterfactual(
                [], specs=[spec], runtime_config=config,
                baseline_ratio=.30, alternative_ratio=.60)

        self.assertEqual([ratio for _spec, ratio in seen], [.30, .60])
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertEqual(result["status"], "measured")
        self.assertTrue(result["diagnostic_only"])
        self.assertFalse(result["authorizing"])
        self.assertFalse(result["promotion_allowed"])
        self.assertFalse(result["production_mutation"])
        self.assertEqual(result["difference"]["stressed_cost_rejections"], -1)
        self.assertEqual(result["difference"]["admitted_trades"], 1)
        self.assertEqual(result["only_changed_field"],
                         "risk.max_stressed_cost_to_risk_ratio")
        self.assertTrue(result["config_evidence"]["exact_only_changed_field"])
        self.assertEqual(result["config_evidence"]["changed_paths"],
                         ["risk.max_stressed_cost_to_risk_ratio"])
        self.assertEqual(result["pairing"]["matched"], 1)
        self.assertEqual(
            result["pairing"]["transition_classifications"],
            {"direct_cost_gate_admission": 1})
        self.assertTrue(result["pairing"]["direct_causal_interpretation_isolated"])
        self.assertTrue(result["invariants"]["controlled_change_verified"])
        self.assertEqual(result["proofs"], [])

    def test_spec_loader_deduplicates_content_identity(self):
        spec = initial_hypotheses(1)[0].rule_spec
        loaded = load_frozen_specs({
            "reports": [{"variants": [
                {"rule_spec": spec}, {"rule_spec": dict(spec)},
            ]}],
        })
        self.assertEqual(loaded, [spec])

    def test_empirical_cluster_measurement_and_funnel_are_diagnostic(self):
        spec = initial_hypotheses(1)[0].rule_spec
        config = {
            "broker": {"data_feed": "iex"},
            "risk": {
                "stressed_cost_scenario_bps": 25.0,
                "max_stressed_cost_to_risk_ratio": .30,
            },
        }
        original = json.loads(json.dumps(config))

        def simulate(_bars, _snapshots, _frozen, **_kwargs):
            rows = []
            for index, value in enumerate((-1.0, 1.0, 2.0), 1):
                rows.append({
                    "opportunity_id": f"trade-{index}",
                    "session_date": f"2026-01-0{index}",
                    "execution_disposition": "executed",
                    "signal_opportunity": True,
                    "no_trade": False,
                    "net_pnl": value * 10.0,
                    "return_value": value / 100.0,
                    "r_multiple": value,
                    "costs": float(index),
                    "stressed_cost_to_risk_ratio": .2 + index / 100.0,
                    "entry_slippage": {"slippage_bps": float(index)},
                    "entry_fill_source": "quote",
                    "exit_fill_source": "quote",
                    "target_price": 101.0,
                    "exit_reason": "target" if index != 2 else "stop",
                })
            rows.append({
                "opportunity_id": "no-signal",
                "session_date": "2026-01-04",
                "execution_disposition": "no_signal",
                "signal_opportunity": False,
                "no_trade": True,
            })
            return {"rows": rows}

        with patch("research.cost_counterfactual._read_discovery_rows",
                   return_value=([{"kind": "bar"}], [], {}, [])), \
             patch("research.cost_counterfactual.simulate_account",
                   side_effect=simulate):
            result = run_counterfactual(
                [], specs=[spec], runtime_config=config,
                bootstrap_draws=64, bootstrap_min_clusters=2,
                bootstrap_block_length=2)

        self.assertEqual(config, original)
        summary = result["arms"]["baseline"]["summary"]
        self.assertEqual(summary["observation_unit"], "variant_trade")
        self.assertEqual(summary["r_multiple"]["count"], 3)
        self.assertAlmostEqual(summary["r_multiple"]["mean"], 2 / 3)
        self.assertAlmostEqual(
            summary["r_multiple"]["sample_standard_deviation"],
            1.5275252316519468)
        self.assertEqual(summary["trades_per_session"]["observed_sessions"], 4)
        self.assertEqual(summary["trades_per_session"]["sessions_with_trades"], 3)
        self.assertEqual(summary["trades_per_session"]["counts"][-1]["trades"], 0)
        self.assertEqual(summary["target_reach"]["target_reached_trades"], 2)
        self.assertEqual(summary["costs"]["count"], 3)
        self.assertEqual(summary["entry_slippage"]["count"], 3)
        self.assertEqual(summary["fill_sources"]["entry"], {"quote": 3})
        variant_distribution = result["arms"]["baseline"][
            "variant_level_empirical_distribution"]
        self.assertEqual(variant_distribution["variant_count"], 1)
        self.assertAlmostEqual(
            variant_distribution["per_trade_r_sample_sigma"]["mean"],
            1.5275252316519468)
        self.assertAlmostEqual(
            variant_distribution["mean_trades_per_session"]["mean"], .75)
        section = result["arms"]["baseline"]["section_05_measurement"]
        self.assertTrue(section["confidence_interval"]["available"])
        self.assertTrue(section["mde_power"]["available"])
        self.assertFalse(section["dead_band"]["fixed_width_assumed"])
        self.assertIn("no fixed 0.38R", section["dead_band"]["interpretation"])
        funnel = result["funnel"]
        self.assertEqual(funnel["requested_windows"], 5)
        self.assertFalse(funnel["actual_window_measurements_available"])
        self.assertEqual(funnel["nominal_trade_floor_sum"], 600)
        self.assertEqual(funnel["readiness_context"]["offline_required_sessions"], 150)
        self.assertEqual(funnel["readiness_context"]["shadow_required_sessions"], 60)
        self.assertEqual(funnel["readiness_context"]["total_required_sessions"], 210)
        self.assertTrue(all(not item["measurement_available"]
                            for item in funnel["windows"]))
        json.dumps(result, allow_nan=False)

    def test_duplicate_and_malformed_pair_rows_are_excluded(self):
        spec = initial_hypotheses(1)[0].rule_spec
        calls = 0

        def simulate(_bars, _snapshots, _frozen, **_kwargs):
            nonlocal calls
            calls += 1
            valid = {
                "opportunity_id": "duplicate",
                "session_date": "2026-01-02",
                "execution_disposition": "executed",
                "signal_opportunity": True,
                "no_trade": False,
                "net_pnl": 1.0,
                "return_value": .01,
            }
            if calls == 1:
                return {"rows": [valid, dict(valid), {
                    "opportunity_id": "malformed",
                    "session_date": "2026-01-03",
                    "execution_disposition": "refused",
                    "signal_opportunity": True,
                    "no_trade": True,
                }, {
                    "session_date": "2026-01-04",
                    "execution_disposition": "executed",
                    "signal_opportunity": True,
                    "no_trade": False,
                    "r_multiple": 99.0,
                }, {
                    "opportunity_id": "wrong-stage",
                    "session_date": "2026-01-05",
                    "execution_disposition": "refused",
                    "signal_opportunity": True,
                    "no_trade": True,
                    "reject_stage": "open_risk_limit",
                    "reject_reason": "stressed_cost_risk_limit",
                }]}
            return {"rows": [valid]}

        config = {"broker": {"data_feed": "iex"}, "risk": {}}
        with patch("research.cost_counterfactual._read_discovery_rows",
                   return_value=([{"kind": "bar"}], [], {}, [])), \
             patch("research.cost_counterfactual.simulate_account",
                   side_effect=simulate):
            result = run_counterfactual(
                [], specs=[spec], runtime_config=config,
                bootstrap_draws=8, bootstrap_min_clusters=2)

        pairing = result["pairing"]
        self.assertEqual(pairing["matched"], 0)
        self.assertEqual(pairing["duplicate_keys"], 1)
        self.assertEqual(pairing["malformed_rows"], 3)
        self.assertFalse(pairing["complete_pairing"])
        self.assertFalse(pairing["direct_causal_interpretation_isolated"])
        self.assertEqual(
            result["arms"]["baseline"]["summary"]["malformed_reasons"],
            {"cost_gate_stage_mismatch": 1,
             "duplicate_opportunity_key": 2,
             "missing_opportunity_id": 1,
             "refusal_missing_reason": 1})
        self.assertEqual(result["arms"]["baseline"]["summary"]["trades"], 0)
        section = result["arms"]["baseline"]["section_05_measurement"]
        self.assertEqual(section["finite_r_observations"], 0)
        self.assertEqual(section["excluded_identity_rows"], 3)
        self.assertEqual(section["excluded_identity_reasons"],
                         {"duplicate_opportunity_key": 2,
                          "missing_opportunity_id": 1})
        self.assertIn("incomplete_or_ambiguous_pairing",
                      result["invariants"]["invariant_failures"])
        self.assertFalse(result["invariants"]["controlled_change_verified"])

    def test_cli_persists_source_digest_atomically(self):
        spec = initial_hypotheses(1)[0].rule_spec
        payload = {"schema": "strategy-factory.v1", "rule_specs": [spec],
                   "diagnostic_only": True, "authorizing": False, "proofs": [],
                   "dataset_hash": content_hash([{"kind": "bar"}])}
        config = {"broker": {"data_feed": "iex"}, "risk": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "diagnostic.json"
            output = root / "nested" / "counterfactual.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            with patch("research.cost_counterfactual.load_config",
                       return_value=config), \
                 patch("research.cost_counterfactual._read_discovery_rows",
                       return_value=([{"kind": "bar"}], [], {}, [])), \
                 patch("research.cost_counterfactual.simulate_account",
                       return_value={"rows": []}), \
                 redirect_stdout(io.StringIO()) as stdout:
                exit_code = main([
                    "--data", str(root / "ignored.jsonl"),
                    "--specs", str(source),
                    "--agent-config", str(root / "config.yaml"),
                    "--bootstrap-draws", "8",
                    "--bootstrap-min-clusters", "2",
                    "--output", str(output),
                ])
            saved = json.loads(output.read_text(encoding="utf-8"))
            printed = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(saved, printed)
            self.assertTrue(saved["provenance"]["source_report_hash_available"])
            self.assertRegex(saved["provenance"]["source_report_hash"],
                             r"^[0-9a-f]{64}$")
            self.assertTrue(saved["provenance"]["source_report_safety_matches"])
            self.assertTrue(saved["provenance"]["source_report_proofs_empty"])
            self.assertTrue(saved["provenance"]["source_report_hash_verified"])
            self.assertEqual(saved["provenance"]["source_report_binding_origin"],
                             "source_path_recomputed")
            self.assertTrue(
                saved["invariants"]["source_report_binding_verified"])
            self.assertTrue(
                saved["invariants"]["source_report_contract_complete"])
            digest = saved.pop("content_hash")
            self.assertEqual(digest, content_hash(saved))
            self.assertFalse(output.with_name(output.name + ".tmp").exists())

    def test_path_dependent_output_change_and_source_mismatch_are_visible(self):
        spec = initial_hypotheses(1)[0].rule_spec
        calls = 0

        def simulate(_bars, _snapshots, _frozen, **_kwargs):
            nonlocal calls
            calls += 1
            return {"rows": [{
                "opportunity_id": "same",
                "session_date": "2026-01-02",
                "execution_disposition": "executed",
                "signal_opportunity": True,
                "no_trade": False,
                "net_pnl": float(calls),
                "return_value": float(calls) / 100.0,
            }]}

        with patch("research.cost_counterfactual._read_discovery_rows",
                   return_value=([{"kind": "bar"}], [], {}, [])), \
             patch("research.cost_counterfactual.simulate_account",
                   side_effect=simulate):
            result = run_counterfactual(
                [], specs=[spec],
                runtime_config={"broker": {"data_feed": "iex"}, "risk": {}},
                source_report_identity={
                    "dataset_hash": "wrong", "diagnostic_only": True,
                    "authorizing": False, "proof_count": 0,
                }, bootstrap_draws=8, bootstrap_min_clusters=2)

        pairing = result["pairing"]
        self.assertEqual(pairing["transition_classifications"],
                         {"path_dependent_output_change": 1})
        self.assertEqual(pairing["unexpected_or_path_dependent_transitions"], 1)
        self.assertFalse(pairing["only_expected_cost_gate_transitions"])
        self.assertFalse(pairing["direct_causal_interpretation_isolated"])
        self.assertIn("source_report_dataset_hash_mismatch",
                      result["invariants"]["invariant_failures"])
        self.assertIn("source_report_binding_unverified",
                      result["invariants"]["invariant_failures"])
        self.assertFalse(
            result["invariants"]["source_report_binding_verified"])
        self.assertFalse(result["invariants"]["controlled_change_verified"])

    def test_cost_gate_to_other_refusal_is_not_an_admitted_direct_effect(self):
        spec = initial_hypotheses(1)[0].rule_spec
        calls = 0

        def simulate(_bars, _snapshots, _frozen, **_kwargs):
            nonlocal calls
            calls += 1
            return {"rows": [{
                "opportunity_id": "same",
                "session_date": "2026-01-02",
                "execution_disposition": "refused",
                "signal_opportunity": True,
                "no_trade": True,
                "reject_stage": ("cost_stress" if calls == 1
                                 else "open_risk_limit"),
                "reject_reason": ("stressed_cost_risk_limit" if calls == 1
                                  else "max_open_risk_reached"),
            }]}

        with patch("research.cost_counterfactual._read_discovery_rows",
                   return_value=([{"kind": "bar"}], [], {}, [])), \
             patch("research.cost_counterfactual.simulate_account",
                   side_effect=simulate):
            result = run_counterfactual(
                [], specs=[spec],
                runtime_config={"broker": {"data_feed": "iex"}, "risk": {}},
                bootstrap_draws=8, bootstrap_min_clusters=2)

        pairing = result["pairing"]
        self.assertEqual(pairing["transition_classifications"],
                         {"downstream_transition_after_cost_gate_change": 1})
        self.assertFalse(pairing["direct_causal_interpretation_isolated"])
        self.assertFalse(result["invariants"]["controlled_change_verified"])

    def test_incomplete_source_contract_and_false_signal_cost_row_fail_closed(self):
        spec = initial_hypotheses(1)[0].rule_spec
        calls = 0

        def simulate(_bars, _snapshots, _frozen, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"rows": [{
                    "opportunity_id": "same",
                    "session_date": "2026-01-02",
                    "execution_disposition": "refused",
                    "signal_opportunity": False,
                    "no_trade": True,
                    "reject_stage": "cost_stress",
                    "reject_reason": "stressed_cost_risk_limit",
                }]}
            return {"rows": [{
                "opportunity_id": "same",
                "session_date": "2026-01-02",
                "execution_disposition": "executed",
                "signal_opportunity": True,
                "no_trade": False,
            }]}

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "incomplete.json"
            source.write_text(json.dumps({"rule_specs": [spec]}), encoding="utf-8")
            with patch("research.cost_counterfactual._read_discovery_rows",
                       return_value=([{"kind": "bar"}], [], {}, [])), \
                 patch("research.cost_counterfactual.simulate_account",
                       side_effect=simulate):
                result = run_counterfactual(
                    [], specs=[spec], runtime_config={},
                    source_report_path=source,
                    bootstrap_draws=8, bootstrap_min_clusters=2)

        failures = result["invariants"]["invariant_failures"]
        self.assertIn("source_report_dataset_hash_missing", failures)
        self.assertIn("source_report_safety_contract_missing", failures)
        self.assertIn("source_report_proofs_contract_missing", failures)
        self.assertIn("incomplete_or_ambiguous_pairing", failures)
        self.assertFalse(result["invariants"]["source_report_contract_complete"])
        self.assertFalse(result["invariants"]["controlled_change_verified"])
        self.assertEqual(
            result["pairing"]["malformed_reasons"]["baseline"],
            {"cost_gate_signal_opportunity_mismatch": 1})
        self.assertFalse(result["pairing"]["direct_causal_interpretation_isolated"])

    def test_nonfinite_economics_are_reported_without_breaking_json(self):
        spec = initial_hypotheses(1)[0].rule_spec
        row = {
            "opportunity_id": "nonfinite",
            "session_date": "2026-01-02",
            "execution_disposition": "executed",
            "signal_opportunity": True,
            "no_trade": False,
            "net_pnl": 1.0,
            "return_value": .01,
            "costs": float("inf"),
            "r_multiple": float("nan"),
        }
        with patch("research.cost_counterfactual._read_discovery_rows",
                   return_value=([{"kind": "bar"}], [], {}, [])), \
             patch("research.cost_counterfactual.simulate_account",
                   return_value={"rows": [row]}):
            result = run_counterfactual(
                [], specs=[spec],
                runtime_config={"broker": {"data_feed": "iex"}, "risk": {}},
                bootstrap_draws=8, bootstrap_min_clusters=2)

        self.assertEqual(result["pairing"]["matched"], 0)
        self.assertEqual(result["pairing"]["malformed_rows"], 2)
        self.assertEqual(
            result["pairing"]["malformed_reasons"]["baseline"],
            {"invalid_numeric_costs": 1})
        summary = result["arms"]["baseline"]["summary"]
        self.assertEqual(summary["costs"]["invalid"], 1)
        self.assertEqual(summary["r_multiple"]["invalid"], 1)
        json.dumps(result, allow_nan=False)

    def test_counterfactual_rejects_equal_or_invalid_ratios(self):
        spec = initial_hypotheses(1)[0].rule_spec
        for baseline, alternative in (
                (.3, .3), (0, .6), (.3, float("nan")), (True, .6)):
            with self.subTest(baseline=baseline, alternative=alternative), \
                    self.assertRaises(ValueError):
                run_counterfactual(
                    [], specs=[spec], runtime_config={},
                    baseline_ratio=baseline, alternative_ratio=alternative)
        for setting in (0, -1, "bad", 1.5, True):
            with self.subTest(bootstrap_draws=setting), self.assertRaises(ValueError):
                run_counterfactual(
                    [], specs=[spec], runtime_config={},
                    bootstrap_draws=setting)
        for starting_cash in (0, -1, float("inf"), True):
            with self.subTest(starting_cash=starting_cash), self.assertRaises(ValueError):
                run_counterfactual(
                    [], specs=[spec], runtime_config={},
                    starting_cash=starting_cash)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            run_counterfactual(
                [], specs=[spec], runtime_config={}, source_report_hash="abc")


if __name__ == "__main__":
    unittest.main()
