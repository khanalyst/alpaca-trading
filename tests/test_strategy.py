import unittest
from datetime import datetime, timedelta, timezone

from agent import strategy
from agent.config import validate_config
from agent.contracts.ibr import build_ibr_range, evaluate_exit, evaluate_ibr_breakout
from agent.contracts.rule import generate_rule_signal, rule_variant_id, validate_rule_spec


def bars(start, *, high=100.5, low=99.5, close=100.0, volume=10.0):
    return [{"timestamp": start + timedelta(minutes=i), "high": high,
             "low": low, "close": close, "volume": volume}
            for i in range(15)]


class IBRContractTests(unittest.TestCase):
    def test_range_uses_new_york_session_across_dst(self):
        # 09:30 New York is 13:30 UTC in March and 14:30 UTC in November.
        for start in (datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc),
                      datetime(2024, 11, 4, 14, 30, tzinfo=timezone.utc)):
            with self.subTest(start=start):
                result = build_ibr_range(bars(start), config={
                    "timezone": "America/New_York"})
                self.assertIsNotNone(result)
                self.assertEqual(result["bars"], 15)
                self.assertIn("09:30", result["range_start"])

    def test_incomplete_range_is_not_tradable(self):
        start = datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc)
        self.assertIsNone(build_ibr_range(bars(start)[:-1]))

    def test_next_bar_and_completed_close_reject_lookahead_and_wick(self):
        start = datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc)
        opening = build_ibr_range(bars(start))
        same_range_bar = {"timestamp": start + timedelta(minutes=14),
                          "high": 103, "low": 99, "close": 100,
                          "volume": 20}
        self.assertIsNone(evaluate_ibr_breakout(opening, same_range_bar))
        wick_only = {"timestamp": start + timedelta(minutes=15),
                     "high": 103, "low": 99, "close": 100.2,
                     "volume": 20}
        self.assertIsNone(evaluate_ibr_breakout(opening, wick_only))

    def test_long_and_short_are_mirrors(self):
        start = datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc)
        opening = build_ibr_range(bars(start))
        long_signal = evaluate_ibr_breakout(
            opening, {"timestamp": start + timedelta(minutes=15),
                      "high": 102, "low": 100, "close": 101, "volume": 20})
        short_signal = evaluate_ibr_breakout(
            opening, {"timestamp": start + timedelta(minutes=15),
                      "high": 100, "low": 98, "close": 99, "volume": 20})
        self.assertEqual(long_signal["direction"], "long")
        self.assertEqual(short_signal["direction"], "short")
        self.assertEqual(long_signal["stop_price"], opening["low"])
        self.assertEqual(short_signal["stop_price"], opening["high"])
        self.assertEqual(long_signal["target_r"], short_signal["target_r"])

    def test_one_signal_per_symbol_session(self):
        start = datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc)
        state = {}
        opening = build_ibr_range(bars(start))
        bar = {"timestamp": start + timedelta(minutes=15), "high": 102,
               "low": 100, "close": 101, "volume": 20}
        self.assertIsNotNone(evaluate_ibr_breakout(
            opening, bar, symbol="SPY", session_state=state))
        self.assertIsNone(evaluate_ibr_breakout(
            opening, bar, symbol="SPY", session_state=state))

    def test_stop_first_tie_metadata(self):
        result = evaluate_exit("long", {"high": 105, "low": 95},
                               stop_price=95, target_price=105)
        self.assertEqual(result["exit_reason"], "stop")
        self.assertTrue(result["stop_first"])
        self.assertTrue(result["tie"])

    def test_setup_plan_contains_fixed_target_and_force_flat(self):
        cfg = {"strategy": {"id": "ibr", "version": "v1",
                             "target_r": 2.0,
                             "breakout_buffer_bps": 5,
                             "min_relative_volume": 1}}
        snapshot = {"price": 101, "close": 101, "signal_ts": 1710164760,
                    "ibr_range": {"high": 100.5, "low": 99.5,
                                   "width": 1, "range_end_ts": 1710164700,
                                   "complete": True},
                    "relative_volume": 2, "spread_bps": 10,
                    "stale": False, "quote_stale": False,
                    "session": "2024-03-11"}
        plan, why = strategy.build_setup_plan(
            {"symbol": "SPY", "direction": "long",
             "setup_type": "ibr_breakout"}, snapshot, cfg)
        self.assertIsNone(why)
        self.assertAlmostEqual(plan["target_price"], 104)
        self.assertEqual(plan["stop_price"], 99.5)
        self.assertTrue(plan["force_flat"])
        self.assertIsNotNone(plan["force_flat_at"])

    def test_validated_rule_signal_uses_the_same_runtime_plan_boundary(self):
        spec = validate_rule_spec({
            "family": "momentum_continuation", "lookback": 3,
            "slow_lookback": 8, "atr_period": 3,
            "threshold_bps": 1.0, "confirmation": "none",
        })
        variant_id = rule_variant_id(spec)
        cfg = validate_config({"strategy": {
            "id": "rule", "version": "v1", "variant_id": variant_id,
            "rule_spec": spec,
        }})
        base = datetime.now(timezone.utc) - timedelta(minutes=12)
        market_bars = []
        price = 100.0
        for index in range(10):
            opened = price
            price += .2
            market_bars.append({
                "symbol": "SPY", "timestamp": base + timedelta(minutes=index),
                "open": opened, "high": price + .05, "low": opened - .05,
                "close": price, "volume": 1000 + index * 10,
            })
        signal = generate_rule_signal(
            "SPY", market_bars, config=cfg, now=datetime.now(timezone.utc))
        self.assertIsNotNone(signal)
        snapshot = {
            "price": signal["entry_price"], "signal_ts": signal["signal_ts"],
            "session": signal["session"], "spread_bps": 1.0,
            "stale": False, "quote_stale": False,
        }
        plan, why = strategy.build_setup_plan(signal, snapshot, cfg)
        self.assertIsNone(why)
        self.assertEqual(plan["strategy_id"], "rule")
        self.assertEqual(plan["variant_id"], variant_id)
        self.assertLess(plan["stop_price"], plan["entry_price"])
        self.assertGreater(plan["target_price"], plan["entry_price"])


if __name__ == "__main__":
    unittest.main()
