import unittest
from research.factory_core import diagnose


class EconomicDiagnosisTests(unittest.TestCase):
    def test_mixed_data_and_cost_refusals_are_not_no_signals(self):
        result = diagnose([
            {"no_trade": True, "execution_disposition": "refused",
             "signal_opportunity": False, "reject_reason": "no_contiguous_feature_window"},
            {"no_trade": True, "execution_disposition": "refused",
             "signal_opportunity": True, "reject_reason": "stressed_cost_risk_limit"},
            {"no_trade": True, "execution_disposition": "no_signal"},
        ])
        self.assertEqual(result["evidence_status"], "data_and_execution_blocked")
        self.assertEqual(result["primary_failure"], "execution_blocked")
        self.assertEqual(result["data_rejection_count"], 1)
        self.assertEqual(result["signal_execution_rejection_count"], 1)
        self.assertEqual(result["no_signal_count"], 1)
        self.assertIsNone(result["win_rate"])
        self.assertIsNone(result["measured_net_expectancy"])

    def test_payoff_can_support_low_win_rate(self):
        values = [-1] * 8 + [6] * 2
        result = diagnose([{"session_date": f"2026-08-{i+1:02d}",
                            "net_pnl": pnl, "gross_pnl": pnl + .1}
                           for i, pnl in enumerate(values)])
        self.assertEqual(result["primary_failure"], "none")
        self.assertEqual(result["win_rate"], .2)
        self.assertEqual(result["net_payoff_ratio"], 6)
        self.assertAlmostEqual(result["break_even_nonflat_win_rate"], 1/7)
        self.assertAlmostEqual(result["fees_after_fill_prices"], 1)
        self.assertEqual(result["evidence_status"], "positive_point_estimate")
        self.assertFalse(result["edge_proven"])

    def test_high_win_rate_does_not_hide_negative_net_expectancy(self):
        result = diagnose([{"net_pnl": pnl} for pnl in [1] * 9 + [-20]])
        self.assertEqual(result["primary_failure"], "negative_expectancy")
        self.assertIsNone(result["gross_pnl"])

    def test_diagnostic_evidence_does_not_become_authorizing(self):
        rows = [{"net_pnl": 10, "directional_authorizing": False}]
        result = diagnose(rows)
        self.assertEqual(result["evidence_status"], "diagnostic_evidence_only")
        self.assertEqual(result["trades"], 0)
        self.assertFalse(result["authorizing"])
        self.assertEqual(diagnose(rows, diagnostic_only=True)["measured_net_expectancy"], 10)
