"""Counterfactual accounting recognizes both real cost-admission stages."""
from unittest import TestCase
from unittest.mock import patch

from research.cost_counterfactual import _terminal_error, run_counterfactual
from research.factory_core import initial_hypotheses


class CounterfactualGeometryStageTests(TestCase):
    def test_only_canonical_cost_reason_stage_pairs_are_accepted(self):
        row = {"execution_disposition": "refused", "signal_opportunity": True,
               "no_trade": True, "reject_reason": "stressed_cost_risk_limit"}
        for stage in ("risk_geometry", "cost_stress"):
            self.assertIsNone(_terminal_error({**row, "reject_stage": stage}))
        for stage in ("", "open_risk_limit", "arbitrary"):
            self.assertEqual(_terminal_error({**row, "reject_stage": stage}),
                             "cost_gate_stage_mismatch")
        self.assertEqual(_terminal_error({**row, "reject_stage": "cost_stress",
                                         "reject_reason": "other_reason"}),
                         "cost_stage_reason_mismatch")

    def test_geometry_refusals_remain_in_signal_and_pairing_counts(self):
        row = {"opportunity_id": "same-opportunity", "session_date": "2026-01-02",
               "execution_disposition": "refused", "signal_opportunity": True,
               "no_trade": True, "reject_reason": "stressed_cost_risk_limit",
               "reject_stage": "risk_geometry"}
        config = {"broker": {"data_feed": "iex"}, "risk": {
            "stressed_cost_scenario_bps": 25.0, "max_stressed_cost_to_risk_ratio": .30}}
        with patch("research.cost_counterfactual._read_discovery_rows",
                   return_value=([{"kind": "bar"}], [], {}, [])), \
                patch("research.cost_counterfactual.simulate_account",
                      return_value={"rows": [row]}):
            result = run_counterfactual(
                [], specs=[initial_hypotheses(1)[0].rule_spec], runtime_config=config,
                baseline_ratio=.30, alternative_ratio=1.0, bootstrap_draws=20)
        for arm in result["arms"].values():
            self.assertEqual(arm["summary"]["malformed_rows"], 0)
            self.assertEqual(arm["summary"]["signal_opportunities"], 1)
            self.assertEqual(arm["summary"]["stressed_cost_risk_limit"], 1)
            self.assertEqual(arm["summary"]["trades"], 0)
        self.assertTrue(result["pairing"]["complete_pairing"])
