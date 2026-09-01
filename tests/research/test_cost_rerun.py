"""The cost re-run must isolate the cost model and nothing else.

Both arms replay the same corpus, specs, policy, and sizing logic; the cost
schedule is the changed input.  Realized quantities and equity may diverge
causally from that treatment, so the synthetic fixture pins the invariant
trade count/reference R and the R decomposition against each replay's own
P&L, while keeping zero-trade rows attributable to their control.
"""

import json
from pathlib import Path
import tempfile
import unittest

from agent.contracts.rule import validate_rule_spec
from research.cost_rerun import (deterministic_cohort, render_text,
                                 run_cost_rerun, verify_cost_evidence,
                                 write_immutable_evidence)
from research.quote_costs import QuoteCostError
from tests.research.test_factory_end_to_end import edge_corpus


def _config() -> dict:
    config = json.loads(Path("config.yaml").read_text(encoding="utf-8"))
    # The synthetic fixture carries no broker calendar bounds; a production
    # corpus is calendar-authoritative and keeps the shipped requirement.
    config["session"] = {**config["session"], "require_exact_calendar": False}
    return config


class CostRerunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = _config()
        # Stops wide enough to clear the stressed-cost admission gate, as the
        # factory's own tuning had to reach, so both arms actually execute.
        cls.cohort = [validate_rule_spec({**spec, "stop_atr": 7.0})
                      for spec in deterministic_cohort()][:8]
        cls.report = run_cost_rerun(
            # Keep the existing 50-quote cell floor, but provide enough
            # sessions for the late exit bucket to be genuinely measured.
            edge_corpus(25), runtime_config=cls.config, specs=cls.cohort,
            min_quotes_per_cell=50)
        cls.traded = [item for item in cls.report["results"]
                      if item["configured"]["trades"] > 0]

    def test_the_run_is_diagnostic_and_cannot_authorize(self):
        self.assertTrue(self.report["diagnostic_only"])
        self.assertFalse(self.report["authorizing"])

    def test_the_measured_model_is_fitted_not_assumed(self):
        models = self.report["cost_models"]
        self.assertEqual(models["configured_round_trip_bps"], 17.0)
        self.assertTrue(models["measured"]["provenance"].startswith("measured:"))
        # The fixture quotes a 2 bps spread; the shipped model assumes 4 + 6.
        self.assertLess(models["measured_round_trip_bps"],
                        models["configured_round_trip_bps"])

    def test_evidence_manifest_is_content_addressed_and_split_bound(self):
        valid, reason = verify_cost_evidence(self.report)
        self.assertTrue(valid, reason)
        evidence = self.report["evidence"]
        self.assertTrue(evidence["split_valid"])
        self.assertTrue(evidence["corpus_hash"])
        self.assertTrue(evidence["config_hash"])
        self.assertTrue(evidence["spec_hash"])
        self.assertTrue(evidence["measurement_code_hash"])
        self.assertTrue(evidence["measurement_code_files"])
        self.assertTrue(evidence["fit_sessions_hash"])
        self.assertTrue(evidence["validation_sessions_hash"])

        tampered = json.loads(json.dumps(self.report))
        tampered["bars"] += 1
        self.assertEqual(
            verify_cost_evidence(tampered),
            (False, "report_content_hash_invalid"),
        )

    def test_immutable_writer_refuses_to_replace_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            written = write_immutable_evidence(target, self.report)
            self.assertEqual(written["content_hash"], self.report["content_hash"])
            with self.assertRaisesRegex(ValueError, "already exists"):
                write_immutable_evidence(target, self.report)

    def test_immutable_writer_rejects_a_stale_supplied_hash(self):
        tampered = json.loads(json.dumps(self.report))
        tampered["bars"] += 1
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "tampered.json"
            with self.assertRaisesRegex(ValueError, "content hash"):
                write_immutable_evidence(target, tampered)
            self.assertFalse(target.exists())

    def test_option_vehicle_is_rejected_until_option_costs_are_measured(self):
        with self.assertRaisesRegex(ValueError, "equity only"):
            run_cost_rerun([], runtime_config=self.config, specs=(),
                            vehicle="option")

    def test_quote_snapshot_alias_is_measured_end_to_end(self):
        corpus = [
            ({**row, "kind": "quote_snapshot"}
             if str(row.get("kind") or "").lower() == "quote" else row)
            for row in edge_corpus(25)
        ]
        report = run_cost_rerun(
            corpus, runtime_config=self.config, specs=self.cohort[:1],
            min_quotes_per_cell=50)
        self.assertGreater(report["quotes"], 0)
        self.assertTrue(
            report["cost_models"]["measured"]["provenance"].startswith(
                "measured:"))

    def test_an_undercovered_requested_bucket_fails_the_rerun_clearly(self):
        # Six sessions leave only 12 quotes in the late exit bucket at the
        # existing 50-quote floor.  The strict measured resolver must surface
        # that missing evidence instead of producing a non-comparable report.
        with self.assertRaisesRegex(
                QuoteCostError,
                r"requested measured cost bucket .*unavailable.*under-covered"):
            run_cost_rerun(edge_corpus(6), runtime_config=self.config,
                           specs=self.cohort[:1], min_quotes_per_cell=50)

    def test_only_the_cost_model_differs_between_arms(self):
        self.assertTrue(self.traded)
        for item in self.traded:
            with self.subTest(variant=item["label"]):
                # Same specs, corpus, policy, and sizing logic.  This synthetic
                # fixture preserves trade count/reference R while each arm's
                # realized path remains causally cost-dependent.
                self.assertEqual(item["configured"]["trades"],
                                 item["measured"]["trades"])
                self.assertAlmostEqual(item["configured"]["reference_r"],
                                       item["measured"]["reference_r"], places=9)
                self.assertLess(item["measured"]["drag_r"],
                                item["configured"]["drag_r"])
                self.assertGreater(item["measured"]["net_pnl"],
                                   item["configured"]["net_pnl"])

    def test_measured_cost_cells_are_used_inside_account_replay(self):
        observed = 0
        for item in self.traded:
            for cell in item["measured"]["breakdown"]:
                if not cell["executions"]:
                    continue
                observed += 1
                provenance = cell["cost_model_provenance_counts"]
                self.assertTrue(provenance)
                self.assertTrue(all(
                    value > 0 and key.startswith("measured:") and
                    cell["symbol"] in key
                    for key, value in provenance.items()))
                self.assertTrue(cell["entry_cost_model_provenance_counts"])
                self.assertTrue(cell["exit_cost_model_provenance_counts"])
                self.assertGreaterEqual(cell["gross_pnl"], cell["net_pnl"])
                uncertainty = cell["uncertainty"]
                if uncertainty["ci95_r"] is not None:
                    self.assertLessEqual(uncertainty["ci95_r"]["lower"],
                                         cell["net_r"])
                    self.assertGreaterEqual(uncertainty["ci95_r"]["upper"],
                                            cell["net_r"])
        self.assertGreater(observed, 0)

    def test_the_r_decomposition_ties_out_to_net(self):
        for item in self.traded:
            for arm in ("configured", "measured"):
                with self.subTest(variant=item["label"], arm=arm):
                    row = item[arm]
                    self.assertAlmostEqual(
                        row["reference_r"] - row["drag_r"], row["net_r"],
                        places=9)

    def test_the_admission_gate_is_reported_separately_from_cost(self):
        gate = self.report["stressed_cost_gate"]
        self.assertEqual(gate["scenario_bps"], 25.0)
        self.assertEqual(gate["max_cost_to_risk_ratio"], 0.30)
        self.assertEqual(gate["grammar_min_stop_bps"], 30.0)
        self.assertAlmostEqual(gate["stress_implied_min_stop_bps"],
                               25.0 / 0.30)
        self.assertAlmostEqual(gate["implied_min_stop_bps"], 25.0 / 0.30)
        self.assertEqual(gate["effective_min_stop_bps"],
                         gate["implied_min_stop_bps"])

    def test_a_tight_stop_cohort_is_widened_and_attributable(self):
        """The reconciled policy widens tight equity stops before sizing."""
        tight = [validate_rule_spec({**spec, "stop_atr": 1.0})
                 for spec in deterministic_cohort()][:2]
        report = run_cost_rerun(edge_corpus(25), runtime_config=self.config,
                                specs=tight, min_quotes_per_cell=50)
        widened = [item for item in report["results"]
                   if item["measured"]["stress_floor_bindings"] > 0]
        self.assertTrue(widened)
        for item in widened:
            self.assertGreater(item["measured"]["trades"], 0)
            self.assertEqual(item["measured"]["stressed_cost_rejections"], 0)
        self.assertIn("stops were widened to the effective policy floor",
                      render_text(report))

    def test_variants_are_labelled_by_what_they_changed(self):
        labels = {item["label"] for item in self.report["results"]}
        self.assertTrue(any("stop_atr" in label for label in labels))
        self.assertNotIn("", labels)

    def test_the_rendered_report_states_it_is_not_a_result(self):
        text = render_text(self.report)
        self.assertIn("diagnostic only", text)
        self.assertIn("admission control, not an expected cost", text)


if __name__ == "__main__":
    unittest.main()
