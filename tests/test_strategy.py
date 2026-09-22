import unittest
from datetime import datetime, timedelta, timezone

from agent import strategy
from agent.config import validate_config
from agent.contracts.ibr import (build_ibr_range, evaluate_exit,
                                  evaluate_ibr_breakout, generate_ibr_signal,
                                  IBRConfig)
from agent.contracts.rule import (MIN_STOP_DISTANCE_FRACTION,
                                  generate_rule_signal, rule_variant_id,
                                  validate_rule_spec)


def bars(start, *, high=100.5, low=99.5, close=100.0, volume=10.0):
    return [{"timestamp": start + timedelta(minutes=i), "high": high,
             "low": low, "close": close, "volume": volume}
            for i in range(15)]


class IBRContractTests(unittest.TestCase):
    def test_direct_range_summary_rejects_malformed_metadata_and_intervals(self):
        start = datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc)
        opening = build_ibr_range(bars(start))
        candidate = {"timestamp": start + timedelta(minutes=15),
                     "high": 102, "low": 100, "close": 101, "volume": 20,
                     "relative_volume": 2.0}
        self.assertIsNotNone(evaluate_ibr_breakout(opening, candidate))
        for malformed in (
                {"high": True, "low": 0.5},
                {"high": 100.5, "low": 0},
                {"high": 100.5, "low": 99.5, "volume_mean": float("nan")},
                {"high": 100.5, "low": 99.5, "width_pct": -1},
                {"high": 100.5, "low": 99.5, "atr": "bad"},
        ):
            with self.subTest(malformed=malformed):
                supplied = dict(opening, **malformed)
                self.assertIsNone(evaluate_ibr_breakout(
                    supplied, candidate, config={"strategy": {
                        "min_relative_volume": 1}}))
        for interval in (True, 59, 61, "60"):
            with self.subTest(interval=interval):
                supplied = dict(candidate, interval_seconds=interval)
                self.assertIsNone(evaluate_ibr_breakout(opening, supplied))
        malformed_opening = [dict(row) for row in bars(start)]
        malformed_opening[0]["interval_seconds"] = 59
        self.assertIsNone(generate_ibr_signal(
            "SPY", malformed_opening + [candidate]))

    def test_direct_ibr_config_rejects_invalid_optional_width_pct(self):
        self.assertEqual(IBRConfig.from_mapping({}).max_ibr_width_pct,
                         float("inf"))
        self.assertEqual(IBRConfig.from_mapping({
            "strategy": {"max_ibr_width_pct": 0}}).max_ibr_width_pct, 0.0)
        for value in (True, float("nan"), float("inf"), -1, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                IBRConfig.from_mapping({"strategy": {
                    "max_ibr_width_pct": value}})

    def test_direct_contract_rejects_malformed_ohlcv_without_clamping(self):
        start = datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc)
        opening = build_ibr_range(bars(start))
        base = {"timestamp": start + timedelta(minutes=15),
                "high": 102, "low": 100, "close": 101, "volume": 20}
        self.assertIsNotNone(evaluate_ibr_breakout(opening, base))
        for field, value in (("high", 0), ("low", 0),
                             ("close", True), ("high", float("nan")),
                             ("volume", -1), ("high", 100),):
            with self.subTest(field=field, value=value):
                malformed = dict(base)
                malformed[field] = value
                if field == "high" and value == 100:
                    malformed["low"] = 101
                self.assertIsNone(evaluate_ibr_breakout(opening, malformed))

        negative_opening = bars(start)
        negative_opening[0] = dict(negative_opening[0], volume=-1)
        self.assertIsNone(build_ibr_range(negative_opening))

    def test_direct_contract_requires_causal_now_and_bounds_atr_history(self):
        start = datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc)
        opening = build_ibr_range(bars(start, high=100.5, low=99.5))
        candidate_at = start + timedelta(minutes=15)
        candidate = {
            "timestamp": candidate_at, "high": 102, "low": 100,
            "close": 101, "volume": 20,
            "history": [
                {"timestamp": candidate_at - timedelta(minutes=2),
                 "high": 101, "low": 100, "close": 100.5},
                {"timestamp": candidate_at - timedelta(minutes=1),
                 "high": 101, "low": 100, "close": 100.5},
                # A future wide bar must not change the candidate ATR.
                {"timestamp": candidate_at + timedelta(minutes=1),
                 "high": 200, "low": 1, "close": 100.0},
            ],
        }
        # The width band reads range width over a horizon-scaled ATR
        # (``atr * sqrt(range_minutes)``), so this fixture's unit-width range
        # against a unit ATR over a fifteen-minute window sits near 0.26.
        # The band is deliberately permissive here: this test is about causal
        # ``now`` handling and ATR-history bounding, not width selection.
        cfg = {"strategy": {"atr_period": 2,
                             "min_ibr_width_atr": 0.1,
                             "max_ibr_width_atr": 2.0,
                             "breakout_buffer_bps": 5,
                             "min_relative_volume": 1}}
        self.assertIsNotNone(evaluate_ibr_breakout(opening, candidate,
                                                    config=cfg))
        malformed_history = dict(candidate, history=[
            {"timestamp": candidate_at - timedelta(minutes=2),
             "high": 99, "low": 100, "close": 100.5},
            {"timestamp": candidate_at - timedelta(minutes=1),
             "high": 101, "low": 100, "close": 100.5},
        ])
        self.assertIsNone(evaluate_ibr_breakout(
            opening, malformed_history, config=cfg))
        delayed = dict(candidate, observed_at=candidate_at + timedelta(minutes=2))
        self.assertIsNone(evaluate_ibr_breakout(
            opening, delayed, config={"strategy": {
                **cfg["strategy"], "stale_minutes": 5.0}},
            now=candidate_at + timedelta(minutes=1)))
        self.assertIsNone(evaluate_ibr_breakout(opening, candidate,
                                                config=cfg, now="invalid"))
        self.assertIsNone(evaluate_ibr_breakout(opening, candidate,
                                                config=cfg,
                                                now=candidate_at.replace(tzinfo=None)))

    def test_generator_does_not_consume_future_opening_observation(self):
        start = datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc)
        opening = [dict(row) for row in bars(start)]
        opening[0]["observed_at"] = start + timedelta(minutes=16, seconds=1)
        candidate = {"timestamp": start + timedelta(minutes=15),
                     "high": 102, "low": 100, "close": 101, "volume": 20}
        self.assertIsNone(generate_ibr_signal(
            "SPY", [*opening, candidate], now=start + timedelta(minutes=16)))
        malformed = [dict(row) for row in bars(start)]
        malformed[0]["high"] = 0
        self.assertIsNone(generate_ibr_signal(
            "SPY", malformed + [candidate], now=start + timedelta(minutes=16)))

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

    def test_ibr_contract_and_runtime_plan_enforce_the_30bps_stop_floor(self):
        start = datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc)
        opening = build_ibr_range(bars(
            start, high=100.01, low=99.99, close=100.0))
        signal = evaluate_ibr_breakout(
            opening, {"timestamp": start + timedelta(minutes=15),
                      "high": 100.1, "low": 100.0, "close": 100.07,
                      "volume": 20})
        self.assertIsNotNone(signal)
        self.assertAlmostEqual(
            signal["stop_distance"],
            signal["entry_price"] * MIN_STOP_DISTANCE_FRACTION)

        cfg = {"strategy": {"id": "ibr", "version": "v1",
                            "target_r": 2.0, "breakout_buffer_bps": 5,
                            "min_relative_volume": 1}}
        snapshot = {
            "price": 100.07, "close": 100.07, "signal_ts": 1710164760,
            "ibr_range": {"high": 100.01, "low": 99.99, "width": .02,
                          "range_end_ts": 1710164700, "complete": True},
            "relative_volume": 2, "spread_bps": 10,
            "stale": False, "quote_stale": False,
            "session": "2024-03-11",
        }
        plan, why = strategy.build_setup_plan(
            {"symbol": "SPY", "direction": "long",
             "setup_type": "ibr_breakout"}, snapshot, cfg)
        self.assertIsNone(why)
        self.assertAlmostEqual(
            plan["stop_distance"],
            plan["entry_price"] * MIN_STOP_DISTANCE_FRACTION)

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

    def test_setup_plan_requires_both_freshness_flags(self):
        cfg = {"strategy": {"id": "ibr", "version": "v1",
                             "target_r": 2.0,
                             "breakout_buffer_bps": 5,
                             "min_relative_volume": 1}}
        snapshot = {
            "price": 101, "close": 101, "signal_ts": 1710164760,
            "ibr_range": {"high": 100.5, "low": 99.5, "width": 1,
                          "range_end_ts": 1710164700, "complete": True},
            "relative_volume": 2, "spread_bps": 10,
            "stale": False, "session": "2024-03-11",
        }
        plan, why = strategy.build_setup_plan(
            {"symbol": "SPY", "direction": "long",
             "setup_type": "ibr_breakout"}, snapshot, cfg)
        self.assertIsNone(plan)
        self.assertEqual(why, "market data freshness is unavailable")

        snapshot["quote_stale"] = False
        snapshot.pop("stale")
        plan, why = strategy.build_setup_plan(
            {"symbol": "SPY", "direction": "long",
             "setup_type": "ibr_breakout"}, snapshot, cfg)
        self.assertIsNone(plan)
        self.assertEqual(why, "market data freshness is unavailable")

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
            "force_flat_at": (base + timedelta(hours=8)).isoformat(),
            "force_flat_ts": (base + timedelta(hours=8)).timestamp(),
        }
        plan, why = strategy.build_setup_plan(signal, snapshot, cfg)
        self.assertIsNone(why)
        self.assertEqual(plan["strategy_id"], "rule")
        self.assertEqual(plan["variant_id"], variant_id)
        self.assertLess(plan["stop_price"], plan["entry_price"])
        self.assertGreater(plan["target_price"], plan["entry_price"])


if __name__ == "__main__":
    unittest.main()
