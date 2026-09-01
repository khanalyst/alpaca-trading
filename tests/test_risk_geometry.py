import math
import unittest

from agent.contracts.risk_geometry import (
    RiskGeometryError,
    effective_stop_distance,
    effective_stop_floor_bps,
    required_stop_distance_bps,
)


class RiskGeometryTests(unittest.TestCase):
    def test_preregistered_stress_ladder_maps_to_expected_stop_floors(self):
        expected = {9.0: 30.0, 15.0: 50.0, 25.0: 250.0 / 3.0,
                    50.0: 500.0 / 3.0}
        for scenario, floor in expected.items():
            with self.subTest(scenario=scenario):
                self.assertAlmostEqual(
                    required_stop_distance_bps(scenario, .30), floor)

    def test_effective_floor_never_undercuts_the_grammar_floor(self):
        self.assertEqual(effective_stop_floor_bps(30.0, 9.0, .30), 30.0)
        self.assertAlmostEqual(
            effective_stop_floor_bps(30.0, 25.0, .30), 250.0 / 3.0)

    def test_effective_distance_widens_only_when_policy_requires_it(self):
        distance, floor = effective_stop_distance(
            100.0, .40, base_floor_bps=30.0,
            scenario_bps=25.0, max_cost_to_risk_ratio=.30)
        self.assertAlmostEqual(floor, 250.0 / 3.0)
        self.assertAlmostEqual(distance, 5.0 / 6.0)

        distance, floor = effective_stop_distance(
            100.0, 1.25, base_floor_bps=30.0,
            scenario_bps=25.0, max_cost_to_risk_ratio=.30)
        self.assertAlmostEqual(distance, 1.25)
        self.assertAlmostEqual(floor, 250.0 / 3.0)

    def test_zero_ratio_is_an_explicit_kill_switch(self):
        self.assertTrue(math.isinf(required_stop_distance_bps(25.0, 0.0)))
        with self.assertRaisesRegex(RiskGeometryError, "no finite stop"):
            effective_stop_distance(
                100.0, .30, base_floor_bps=30.0,
                scenario_bps=25.0, max_cost_to_risk_ratio=0.0)

    def test_minimum_increment_rounds_distance_outward(self):
        distance, floor = effective_stop_distance(
            100.0, .3001, base_floor_bps=30.0,
            scenario_bps=9.0, max_cost_to_risk_ratio=.30,
            minimum_increment=.01)
        self.assertEqual(distance, .31)
        self.assertEqual(floor, 30.0)

    def test_invalid_increment_fails_closed(self):
        for increment in (0, -0.01, True, float("inf")):
            with self.subTest(increment=increment), self.assertRaises(
                    RiskGeometryError):
                effective_stop_distance(
                    100.0, 1.0, base_floor_bps=30.0,
                    scenario_bps=25.0, max_cost_to_risk_ratio=.30,
                    minimum_increment=increment)

    def test_invalid_inputs_fail_closed(self):
        for values in ((True, .30), (25.0, True), (-1.0, .30),
                       (25.0, -1.0), (float("nan"), .30)):
            with self.subTest(values=values), self.assertRaises(RiskGeometryError):
                required_stop_distance_bps(*values)


if __name__ == "__main__":
    unittest.main()
