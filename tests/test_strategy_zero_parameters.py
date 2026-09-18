"""Explicit zero thresholds must retain their validated strategy meaning."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from agent.config import ConfigError, validate_config
from agent.contracts.ibr import build_ibr_range, evaluate_ibr_breakout
from agent.contracts.rule import rule_variant_id, validate_rule_spec
from agent.strategy import build_setup_plan
from agent.variants import apply, load_registry


START = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _ibr_config(**parameters):
    return validate_config({"strategy": {
        "id": "ibr", "version": "v1", "variant_id": "ibr.baseline",
        **parameters,
    }})


def _opening(cfg):
    return build_ibr_range([
        {"timestamp": START + timedelta(minutes=index), "open": 99.5,
         "high": 100.0, "low": 99.0, "close": 99.5, "volume": 1000.0}
        for index in range(15)
    ], config=cfg)


def _snapshot(cfg, *, close=100.1, relative_volume=2.0, spread_bps=1.0):
    return {
        "price": close, "entry_price": close, "close": close,
        "signal_ts": (START + timedelta(minutes=16)).timestamp(),
        "session": START.date().isoformat(), "ibr_range": _opening(cfg),
        "relative_volume": relative_volume, "spread_bps": spread_bps,
        "stale": False, "quote_stale": False,
    }


class StrategyZeroParameterTests(unittest.TestCase):
    def test_setup_rejects_malformed_nested_and_flat_ibr_ranges(self):
        cfg = _ibr_config()
        decision = {"symbol": "SPY", "direction": "long"}
        nested = _snapshot(cfg, close=1.1)
        nested["ibr_range"].update(high=1.0, low=0.5, volume_mean=10.0)
        self.assertIsNotNone(build_setup_plan(decision, nested, cfg)[0])
        nested["ibr_range"]["high"] = True
        nested["ibr_high"] = 101.0
        nested["ibr_low"] = 99.0
        plan, reason = build_setup_plan(decision, nested, cfg)
        self.assertIsNone(plan)
        self.assertEqual(reason, "IBR range is incomplete")

        flat = _snapshot(cfg, close=1.1)
        flat.pop("ibr_range")
        flat["ibr_high"] = 1.0
        flat["ibr_low"] = 0.5
        self.assertIsNotNone(build_setup_plan(decision, flat, cfg)[0])
        flat["ibr_high"] = True
        plan, reason = build_setup_plan(decision, flat, cfg)
        self.assertIsNone(plan)
        self.assertEqual(reason, "IBR range is incomplete")

    def test_optional_max_ibr_width_pct_validates_without_changing_omission(self):
        omitted = _ibr_config()
        self.assertNotIn("max_ibr_width_pct", omitted["strategy"])
        explicit_zero = _ibr_config(max_ibr_width_pct=0)
        self.assertEqual(explicit_zero["strategy"]["max_ibr_width_pct"], 0.0)
        for value in (True, -1, float("nan"), float("inf"), "2"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                _ibr_config(max_ibr_width_pct=value)

    def test_registered_zero_buffer_signal_and_setup_agree_in_both_directions(self):
        variant = load_registry(ROOT / "research" / "variants.yaml")["ibr.buffer.0bps"]
        cfg = apply(variant, _ibr_config())
        self.assertEqual(cfg["strategy"]["breakout_buffer_bps"], 0.0)
        for direction, close in (("long", 100.02), ("short", 98.98)):
            with self.subTest(direction=direction):
                bar = {"timestamp": START + timedelta(minutes=15),
                       "open": close, "high": close + .02, "low": close - .02,
                       "close": close, "volume": 2000.0, "atr": 1.0,
                       "spread_bps": 1.0, "data_age_seconds": 0.0}
                signal = evaluate_ibr_breakout(
                    _opening(cfg), bar, config=cfg, symbol="SPY",
                    now=START + timedelta(minutes=16))
                self.assertIsNotNone(signal)
                self.assertEqual(signal["direction"], direction)
                plan, reason = build_setup_plan(signal, _snapshot(cfg, close=close), cfg)
                self.assertIsNone(reason)
                self.assertEqual(plan["variant_id"], variant.variant_id)
                self.assertAlmostEqual(plan["stop_price"], signal["stop_price"])
                self.assertAlmostEqual(plan["target_price"], signal["target_price"])

    def test_default_buffer_still_rejects_near_range_close(self):
        cfg = _ibr_config()
        plan, reason = build_setup_plan(
            {"symbol": "SPY", "direction": "long"}, _snapshot(cfg, close=100.02), cfg)
        self.assertIsNone(plan)
        self.assertEqual(reason, "IBR close did not break the upper range")

    def test_explicit_zero_relative_volume_accepts_observed_zero_not_missing(self):
        cfg = _ibr_config(min_relative_volume=0.0)
        snapshot = _snapshot(cfg, relative_volume=0.0)
        plan, reason = build_setup_plan({"symbol": "SPY", "direction": "long"}, snapshot, cfg)
        self.assertIsNone(reason)
        self.assertIsNotNone(plan)
        snapshot.pop("relative_volume")
        plan, reason = build_setup_plan({"symbol": "SPY", "direction": "long"}, snapshot, cfg)
        self.assertIsNone(plan)
        self.assertEqual(reason, "relative volume is unavailable")

    def test_default_relative_volume_still_rejects_observed_zero(self):
        cfg = _ibr_config()
        plan, reason = build_setup_plan(
            {"symbol": "SPY", "direction": "long"},
            _snapshot(cfg, relative_volume=0.0), cfg)
        self.assertIsNone(plan)
        self.assertEqual(reason, "relative volume is below the IBR threshold")

    def test_zero_spread_limit_is_enforced_for_ibr(self):
        cfg = _ibr_config(max_spread_bps=0.0)
        for spread, expected in ((0.0, None), (0.1, "spread is too wide"),
                                 (None, "spread is unavailable")):
            with self.subTest(spread=spread):
                plan, reason = build_setup_plan(
                    {"symbol": "SPY", "direction": "long"},
                    _snapshot(cfg, spread_bps=spread), cfg)
                self.assertEqual(reason, expected)
                self.assertEqual(plan is not None, expected is None)

    def test_zero_spread_limit_is_enforced_for_rule(self):
        spec = validate_rule_spec({"family": "momentum_continuation"})
        cfg = validate_config({"strategy": {
            "id": "rule", "version": "v1", "variant_id": rule_variant_id(spec),
            "rule_spec": spec, "max_spread_bps": 0.0,
        }})
        signal = {"symbol": "SPY", "direction": "long", "setup_type": "rule_signal",
                  "entry_price": 100.0, "stop_price": 99.0, "target_price": 102.0,
                  "signal_ts": (START + timedelta(minutes=16)).timestamp(),
                  "session": START.date().isoformat(),
                  "force_flat_at": (START + timedelta(hours=6)).isoformat()}
        for spread, allowed in ((0.0, True), (0.1, False), (None, False)):
            with self.subTest(spread=spread):
                plan, reason = build_setup_plan(signal, {
                    "price": 100.0, "spread_bps": spread,
                    "stale": False, "quote_stale": False,
                }, cfg)
                self.assertEqual(plan is not None, allowed)
                self.assertEqual(reason, None if allowed else "spread is unavailable or too wide")

    def test_absent_optional_parameters_keep_conservative_defaults(self):
        cfg = _ibr_config()
        for key in ("breakout_buffer_bps", "min_relative_volume", "max_spread_bps"):
            cfg["strategy"].pop(key)
        for snapshot, expected in (
                (_snapshot(cfg, close=100.02), "IBR close did not break the upper range"),
                (_snapshot(cfg, relative_volume=0.0), "relative volume is below the IBR threshold"),
                (_snapshot(cfg, spread_bps=26.0), "spread is too wide")):
            with self.subTest(expected=expected):
                plan, reason = build_setup_plan(
                    {"symbol": "SPY", "direction": "long"}, snapshot, cfg)
                self.assertIsNone(plan)
                self.assertEqual(reason, expected)


if __name__ == "__main__":
    unittest.main()
