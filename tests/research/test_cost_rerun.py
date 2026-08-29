"""The cost re-run must isolate the cost model and nothing else.

Both arms replay the same corpus, policy, specs and sizing; only the cost
schedule differs.  These tests pin that, pin the R decomposition against the
replay's own P&L, and pin that a zero-trade row stays attributable to the
control that caused it.
"""

import json
import unittest

from agent.contracts.rule import validate_rule_spec
from research.cost_rerun import (deterministic_cohort, render_text,
                                 run_cost_rerun)
from tests.research.test_factory_end_to_end import edge_corpus


def _config() -> dict:
    config = json.loads(open("config.yaml", encoding="utf-8").read())
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
            edge_corpus(12), runtime_config=cls.config, specs=cls.cohort,
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

    def test_only_the_cost_model_differs_between_arms(self):
        self.assertTrue(self.traded)
        for item in self.traded:
            with self.subTest(variant=item["label"]):
                # Same specs, corpus and sizing: the trade population is
                # identical and only what each fill costs changes.
                self.assertEqual(item["configured"]["trades"],
                                 item["measured"]["trades"])
                self.assertAlmostEqual(item["configured"]["reference_r"],
                                       item["measured"]["reference_r"], places=9)
                self.assertLess(item["measured"]["drag_r"],
                                item["configured"]["drag_r"])
                self.assertGreater(item["measured"]["net_pnl"],
                                   item["configured"]["net_pnl"])

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
        self.assertAlmostEqual(gate["implied_min_stop_bps"], 25.0 / 0.30)

    def test_a_gate_blocked_cohort_is_attributable_not_silently_empty(self):
        """A tight-stop cohort trades nothing; the reason must be visible."""
        tight = [validate_rule_spec({**spec, "stop_atr": 1.0})
                 for spec in deterministic_cohort()][:2]
        report = run_cost_rerun(edge_corpus(6), runtime_config=self.config,
                                specs=tight, min_quotes_per_cell=50)
        blocked = [item for item in report["results"]
                   if item["measured"]["stressed_cost_rejections"] > 0]
        self.assertTrue(blocked)
        for item in blocked:
            self.assertEqual(item["measured"]["trades"], 0)
        self.assertIn("refused by the stressed-cost gate", render_text(report))

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
