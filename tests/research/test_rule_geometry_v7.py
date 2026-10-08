"""Authored diagnostic geometry changes identity without relaxing admission."""
from copy import deepcopy
import unittest

from agent.contracts.rule import (
    RULE_SCHEMA_V6, RULE_SCHEMA_V7, RuleSpecError, causal_maturity_bars,
    evaluate_rule_signal, evaluate_rule_signal_metadata, feature_window_bars,
    generate_rule_signal,
    rule_semantic_distance, rule_semantic_signature, rule_spec_json_schema,
    rule_variant_id, rule_vehicle_executable, validate_rule_spec,
)
from research.costs import ReplayPolicy
from research.factory_core import _simulate_trade
from tests.research.test_rule_context_v6 import BASE, bars
from tests.test_exit_contract import _bars
from agent.market_entry_risk import MarketEntryRiskMixin
from datetime import datetime, timezone


class DiagnosticGeometryV7Tests(unittest.TestCase):
    def test_diagnostic_specs_cannot_generate_live_signals_or_risk_orders(self):
        class Harness(MarketEntryRiskMixin):
            def __init__(self, cfg):
                self.cfg, self.events = cfg, []
            def _number(self, value):
                return float(value) if value is not None else None
            def _event(self, kind, payload):
                self.events.append((kind, payload))
        rows = bars([100 + i * .01 for i in range(20)])
        for schema in (RULE_SCHEMA_V6, RULE_SCHEMA_V7):
            spec = validate_rule_spec({**BASE, "schema": schema})
            cfg = {"strategy": {"id": "rule", "execution_mode": "shares",
                                "rule_spec": spec}}
            self.assertIsNotNone(evaluate_rule_signal(rows, spec))
            self.assertIsNone(generate_rule_signal("SPY", rows, config=cfg))
            harness = Harness(cfg)
            # Even an externally supplied signal with its schema stripped
            # cannot bypass the configured diagnostic-rule risk boundary.
            for signal in ({"rule_schema": schema}, {}):
                self.assertIsNone(harness._risk_order(
                    "SPY", signal, {}, {"equity": 100000}, [],
                    datetime.now(timezone.utc)))
            self.assertTrue(all(event[1]["reason"] == "diagnostic_rule_not_executable"
                                for event in harness.events))
    def test_neutral_floor_preserves_v6_semantics_but_not_storage_identity(self):
        v6 = validate_rule_spec(BASE)
        v7 = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V7})
        self.assertEqual(v7["min_stop_distance_bps"], 30.0)
        self.assertEqual(rule_semantic_signature(v6), rule_semantic_signature(v7))
        self.assertEqual(rule_semantic_distance(v6, v7), 0.0)
        self.assertNotEqual(rule_variant_id(v6), rule_variant_id(v7))
        for vehicle in ("equity", "option"):
            self.assertFalse(rule_vehicle_executable(v7, vehicle))
        self.assertEqual(feature_window_bars(v6), feature_window_bars(v7))
        self.assertEqual(causal_maturity_bars(v6), causal_maturity_bars(v7))

    def test_floor_is_versioned_finite_and_bounded(self):
        for floor in (True, None, float("nan"), float("inf"), 29.0, 1001.0):
            with self.subTest(floor=floor), self.assertRaises(RuleSpecError):
                validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V7,
                                    "min_stop_distance_bps": floor})
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({**BASE, "min_stop_distance_bps": 100.0})
        for branch in rule_spec_json_schema(RULE_SCHEMA_V7)["oneOf"]:
            self.assertIn("min_stop_distance_bps", branch["required"])
            self.assertEqual(set(branch["required"]), set(branch["properties"]))

    def test_new_authored_floor_and_prior_atr_are_visible_in_metadata(self):
        rows = bars([100 + i * .01 for i in range(20)])
        v6 = validate_rule_spec(BASE)
        v7 = validate_rule_spec({**BASE, "schema": RULE_SCHEMA_V7,
                                "min_stop_distance_bps": 100.0})
        old = evaluate_rule_signal(rows, v6)
        new = evaluate_rule_signal_metadata(rows, v7)
        self.assertAlmostEqual(old["stop_distance"] / old["entry_price"] * 10000, 30.0)
        self.assertAlmostEqual(new["stop_distance"] / new["entry_price"] * 10000, 100.0)
        self.assertTrue(new["floor_binding"])
        self.assertEqual(new["min_stop_distance_bps"], 100.0)
        self.assertGreater(rule_semantic_distance(v6, v7), 0)
        changed = deepcopy(rows)
        changed[-1].update(high=150, low=50)
        self.assertEqual(new["stop_distance"],
                         evaluate_rule_signal(changed, v7)["stop_distance"])

    def test_unchanged_stress_veto_refuses_30_and_accepts_authored_100(self):
        rows = _bars([100 + i * .01 for i in range(20)])
        # Completed feature fixture; executable pricing uses explicit bar-only
        # test policy, so this test isolates stop geometry from quote coverage.
        spec = validate_rule_spec({**BASE, "max_hold_bars": 3})
        policy = ReplayPolicy(strict_market_data=False,
            stressed_cost_scenario_bps=25.0, max_stressed_cost_to_risk_ratio=.30)
        old = _simulate_trade(rows, spec, [], "equity", policy=policy)
        new = _simulate_trade(rows, {**spec, "schema": RULE_SCHEMA_V7,
            "min_stop_distance_bps": 100.0}, [], "equity", policy=policy)
        self.assertEqual(old["unpriced_reason"], "stressed_cost_risk_limit")
        self.assertNotIn("unpriced_reason", new)
        self.assertTrue(new["diagnostic_only"])
        self.assertFalse(new["authorizing"])
        self.assertFalse(new["rule_vehicle_executable"])
        self.assertEqual(policy.stressed_cost_scenario_bps, 25.0)
        self.assertEqual(policy.max_stressed_cost_to_risk_ratio, .30)


if __name__ == "__main__":
    unittest.main()
