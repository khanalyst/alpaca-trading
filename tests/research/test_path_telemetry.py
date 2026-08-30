from datetime import datetime, timedelta, timezone
import unittest

from research.path_telemetry import (aggregate_path_telemetry,
                                     compute_path_telemetry,
                                     render_path_telemetry_json,
                                     render_path_telemetry_svg,
                                     target_hold_reachability)


BASE = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)


def bars(values):
    rows = []
    for index, (opened, high, low, close) in enumerate(values):
        stamp = BASE + timedelta(minutes=index)
        rows.append({"symbol": "SPY", "timestamp": stamp.isoformat(),
                     "open": opened, "high": high, "low": low,
                     "close": close, "interval_seconds": 60})
    return rows


class PathTelemetryTests(unittest.TestCase):
    def test_long_and_short_directional_excursions(self):
        long_trade = {"symbol": "SPY", "direction": "long",
                      "entry_timestamp": BASE.isoformat(),
                      "exit_timestamp": (BASE + timedelta(minutes=3)).isoformat(),
                      "underlying_entry": 100.0, "stop_price": 95.0,
                      "target_r": 2.0, "max_hold_bars": 3,
                      "exit_reason": "target"}
        long_path = compute_path_telemetry(
            long_trade, bars([(100, 102, 99, 101), (101, 104, 100, 103),
                              (103, 110, 102, 109)]))
        self.assertEqual(long_path["observed_bars"], 3)
        self.assertAlmostEqual(long_path["mfe_bps"], 1000.0)
        self.assertAlmostEqual(long_path["mae_bps"], -100.0)
        self.assertAlmostEqual(long_path["mfe_r"], 2.0)
        self.assertAlmostEqual(long_path["mae_r"], -.2)
        self.assertEqual(long_path["mfe_at_exit_r"], long_path["mfe_r"])
        self.assertEqual(long_path["mae_at_exit_r"], long_path["mae_r"])

        short_trade = {**long_trade, "direction": "short", "underlying_entry": 100.0,
                       "stop_price": 105.0, "exit_reason": "stop"}
        short_path = compute_path_telemetry(short_trade, bars(
            [(100, 101, 98, 99), (99, 100, 96, 97), (97, 98, 90, 91)]))
        self.assertAlmostEqual(short_path["mfe_bps"], 1000.0)
        self.assertAlmostEqual(short_path["mae_bps"], -100.0)

    def test_option_underlying_path_uses_underlying_stop_risk_unit(self):
        trade = {"symbol": "SPY", "direction": "long",
                 "entry_timestamp": BASE.isoformat(),
                 "exit_timestamp": (BASE + timedelta(minutes=1)).isoformat(),
                 "underlying_entry": 100.0, "stop_distance": 2.0,
                 "realized_risk_per_unit": 250.0, "max_hold_bars": 1,
                 "exit_reason": "time"}
        result = compute_path_telemetry(
            trade, bars([(100, 104, 99, 102)]))
        self.assertEqual(result["risk_unit"], 2.0)
        self.assertEqual(result["mfe_r"], 2.0)

    def test_gap_is_observed_and_right_censored(self):
        trade = {"symbol": "SPY", "direction": "long",
                 "entry_timestamp": BASE.isoformat(),
                 "deadline_timestamp": (BASE + timedelta(minutes=4)).isoformat(),
                 "underlying_entry": 100.0, "stop_price": 95.0,
                 "max_hold_bars": 4, "exit_reason": "time"}
        rows = bars([(100, 102, 99, 101), (101, 103, 100, 102)])
        rows[1]["timestamp"] = (BASE + timedelta(minutes=2)).isoformat()
        result = compute_path_telemetry(trade, rows)
        self.assertEqual(result["observed_bars"], 1)
        self.assertTrue(result["gap_detected"])
        self.assertTrue(result["right_censored"])
        self.assertEqual(result["censor_reason"], "internal_gap")

    def test_same_bar_stop_target_uses_conservative_reason(self):
        trade = {"symbol": "SPY", "direction": "long",
                 "entry_timestamp": BASE.isoformat(),
                 "exit_timestamp": (BASE + timedelta(minutes=1)).isoformat(),
                 "underlying_entry": 100.0, "stop_price": 95.0,
                 "target_price": 105.0, "target_r": 1.0,
                 "max_hold_bars": 1, "exit_reason": "stop", "tie_broken": True}
        result = compute_path_telemetry(
            trade, bars([(100, 106, 94, 100)]))
        self.assertEqual(result["exit_reason"], "stop")
        self.assertTrue(result["tie_broken"])
        self.assertAlmostEqual(result["mfe_bps"], 600.0)
        self.assertAlmostEqual(result["mae_bps"], -600.0)

    def test_aggregation_and_rendering_are_deterministic(self):
        trade = {"symbol": "SPY", "direction": "long",
                 "entry_timestamp": BASE.isoformat(),
                 "exit_timestamp": (BASE + timedelta(minutes=1)).isoformat(),
                 "underlying_entry": 100.0, "stop_price": 95.0,
                 "target_r": 2.0, "max_hold_bars": 1, "exit_reason": "target"}
        first = compute_path_telemetry(trade, bars([(100, 102, 99, 101)]))
        summary = aggregate_path_telemetry([first, {"path_telemetry": first}])
        self.assertEqual(summary["trade_count"], 2)
        self.assertEqual(len(summary["groups"]), 1)
        self.assertEqual(render_path_telemetry_json(summary),
                         render_path_telemetry_json(summary))
        self.assertIn("<svg", render_path_telemetry_svg(summary))

    def test_target_hold_reachability_is_fit_only_and_excludes_censoring(self):
        usable = [{"mfe_r": .5, "mae_r": -.2, "observed_bars": 90,
                   "exit_reason": "time_expiry", "right_censored": False,
                   "gap_detected": False} for _ in range(30)]
        future = {"mfe_r": 9.0, "mae_r": -.1, "observed_bars": 390,
                  "exit_reason": "target", "right_censored": False,
                  "gap_detected": False}
        censored = {"mfe_r": 9.0, "mae_r": -.1, "observed_bars": 90,
                    "exit_reason": "time_expiry", "right_censored": True,
                    "gap_detected": True}
        result = target_hold_reachability(
            [*usable, future, censored], target_r=2.0, max_hold_bars=90)
        self.assertEqual(result["total"], 32)
        self.assertEqual(result["usable"], 31)
        self.assertEqual(result["censored"], 1)
        self.assertEqual(result["status"], "recommendation")
        self.assertEqual(result["recommendation"]["target_r"], .5)

    def test_target_hold_reachability_is_underpowered_below_thirty_usable(self):
        rows = [{"mfe_r": .5, "mae_r": -.2, "observed_bars": 90,
                 "exit_reason": "time_expiry", "right_censored": False,
                 "gap_detected": False} for _ in range(29)]
        result = target_hold_reachability(rows, target_r=2.0,
                                          max_hold_bars=90)
        self.assertEqual(result["usable"], 29)
        self.assertFalse(result["adequate"])
        self.assertIsNone(result["recommendation"])


if __name__ == "__main__":
    unittest.main()
