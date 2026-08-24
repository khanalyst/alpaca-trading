"""Frozen stressed-cost counterfactual regressions."""

import unittest
from unittest.mock import patch

from research.cost_counterfactual import load_frozen_specs, run_counterfactual
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
                    "execution_disposition": "refused",
                    "signal_opportunity": True,
                    "no_trade": True,
                    "reject_reason": "stressed_cost_risk_limit",
                }]
            else:
                rows = [{
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
        self.assertEqual(result["difference"]["stressed_cost_rejections"], -1)
        self.assertEqual(result["difference"]["admitted_trades"], 1)
        self.assertEqual(result["only_changed_field"],
                         "risk.max_stressed_cost_to_risk_ratio")

    def test_spec_loader_deduplicates_content_identity(self):
        spec = initial_hypotheses(1)[0].rule_spec
        loaded = load_frozen_specs({
            "reports": [{"variants": [
                {"rule_spec": spec}, {"rule_spec": dict(spec)},
            ]}],
        })
        self.assertEqual(loaded, [spec])

    def test_counterfactual_rejects_equal_or_invalid_ratios(self):
        spec = initial_hypotheses(1)[0].rule_spec
        for baseline, alternative in ((.3, .3), (0, .6), (.3, float("nan"))):
            with self.subTest(baseline=baseline, alternative=alternative), \
                    self.assertRaises(ValueError):
                run_counterfactual(
                    [], specs=[spec], runtime_config={},
                    baseline_ratio=baseline, alternative_ratio=alternative)


if __name__ == "__main__":
    unittest.main()
