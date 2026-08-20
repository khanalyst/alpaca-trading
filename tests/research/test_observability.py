"""Policy-neutral observability reports for research evidence."""

from pathlib import Path
import tempfile
import unittest

from research.factory_ledger import FactoryLedger
from research.gates import gate_dependence_report
from research.stats import cross_family_dependence_report


class ObservabilityTests(unittest.TestCase):
    def test_fdr_state_reports_locked_budget_and_non_mutating_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = FactoryLedger(Path(directory) / "edge.sqlite3")
            scope = "shadow-confirmation-v4:equity"
            state = ledger.fdr_state(scope)
            self.assertEqual(state["alpha"], .05)
            self.assertEqual(state["tests"], 0)
            self.assertEqual(state["discoveries"], 0)
            self.assertAlmostEqual(state["next_allocated_alpha"], .025)
            self.assertTrue(state["alpha_immutable"])
            self.assertTrue(state["resolution_available"])
            self.assertFalse(state["depleted"])
            ledger.record_fdr_decision(scope, "candidate-a", .001)
            state = ledger.fdr_state(scope)
            self.assertEqual(state["tests"], 1)
            self.assertEqual(state["discoveries"], 1)
            self.assertAlmostEqual(state["alpha_spent"], .025)
            self.assertAlmostEqual(state["next_preview"]["allocated_alpha"],
                                   .025 + .05 / 6)

    def test_gate_dependence_groups_shared_statistics_without_redeciding(self):
        report = gate_dependence_report({
            "checks": {"heldout_p_significant": True,
                       "family_fdr_significant": False,
                       "global_fdr_significant": False},
            "statistics": {"p_value": .01, "family_q_value": .02,
                            "q_value": .2, "alpha": .05},
        })
        self.assertTrue(report["available"])
        self.assertTrue(report["policy_neutral"])
        self.assertEqual(report["checks"]["family_fdr_significant"]["decision"],
                         False)
        self.assertIn("statistics.alpha", report["shared_source_statistics"])
        self.assertTrue(any(set(("family_fdr_significant",
                                 "global_fdr_significant")) <= set(group)
                            for group in report["dependent_checks"]))

    def test_cross_family_report_is_deterministic_and_pairwise(self):
        vectors = {
            "breakout": {"s1": 1.0, "s2": 2.0, "s3": 3.0},
            "reversion": {"s1": 2.0, "s2": 4.0, "s3": 6.0},
            "independent": {"s1": 3.0, "s2": 1.0, "s3": 2.0},
        }
        first = cross_family_dependence_report(vectors)
        second = cross_family_dependence_report(vectors)
        self.assertEqual(first, second)
        self.assertTrue(first["available"])
        self.assertEqual(first["complete_session_count"], 3)
        self.assertAlmostEqual(first["correlations"]["breakout|reversion"], 1.0)
        self.assertEqual(first["dependence"]["strong_pairs"],
                         [["breakout", "reversion"]])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
