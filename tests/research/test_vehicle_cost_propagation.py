"""Focused regressions for per-vehicle research cost propagation."""

from unittest import TestCase
from unittest.mock import patch

from agent.config import validate_config
from research import strategy_factory
from research.calibration import calibrate
from research.costs import CostModel
from research.edge_discovery_core import _effective_ibr_config


class VehicleCostPropagationTests(TestCase):
    def setUp(self):
        self.config = validate_config({
            "costs": {
                "spread_bps": 2.0,
                "slippage_bps": 3.0,
                "vehicles": {"option": {"spread_bps": 11.0}},
            },
        })

    def test_effective_ibr_selects_distinct_vehicle_models_and_keeps_schedule(self):
        equity, effective_equity = _effective_ibr_config(
            self.config, {}, vehicle="equity")
        option, effective_option = _effective_ibr_config(
            self.config, {}, vehicle="option")
        self.assertEqual(equity.costs.spread_bps, 2.0)
        self.assertEqual(option.costs.spread_bps, 11.0)
        self.assertEqual(effective_equity["costs"]["vehicles"]["option"]["spread_bps"], 11.0)
        self.assertEqual(effective_option["costs"]["spread_bps"], 11.0)

    def test_factory_resolver_is_given_the_vehicle(self):
        calls = []
        original = CostModel.from_config

        def resolve(config, **kwargs):
            calls.append(kwargs.get("vehicle"))
            return original(config, **kwargs)

        # Stop before corpus loading; model selection happens first in the
        # factory boundary, so this isolates the routing assertion.
        with patch.object(strategy_factory.CostModel, "from_config",
                          side_effect=resolve), \
             patch.object(strategy_factory, "_read_discovery_rows",
                          side_effect=RuntimeError("stop after resolve")):
            with self.assertRaisesRegex(RuntimeError, "stop after resolve"):
                strategy_factory._run_factory(
                    [], vehicle="option", runtime_config=self.config,
                    _resources=[], strategies=1, variants_per_strategy=2,
                    workers=1)
        self.assertIn("option", calls)

    def test_vehicle_filtered_calibration_uses_nested_schedule(self):
        report = calibrate([], self.config, vehicle="option")
        self.assertEqual(report["vehicle"], "option")
        self.assertEqual(report["model"]["spread_bps"], 11.0)

    def test_flat_config_retains_legacy_effective_cost_shape(self):
        config = validate_config({"costs": {"spread_bps": 7.0}})
        cfg, effective = _effective_ibr_config(config, {})
        self.assertEqual(cfg.costs.spread_bps, 7.0)
        self.assertEqual(effective["costs"], cfg.costs.as_dict())
        self.assertNotIn("vehicles", effective["costs"])


if __name__ == "__main__":
    import unittest

    unittest.main()
