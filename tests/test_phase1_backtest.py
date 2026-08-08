import unittest

import numpy as np
import pandas as pd

from research.legacy.phase1_v2_backtest import (
    BAR_MS,
    SCENARIOS,
    execution_fill,
    windowed_ewm_last,
)


class WindowedIndicatorTests(unittest.TestCase):
    def test_windowed_ema_matches_repository_pandas_semantics(self):
        values = np.random.default_rng(7).lognormal(4, 0.02, 300)
        actual = windowed_ewm_last(values, 2 / 21, 120)
        expected = np.full(len(values), np.nan)
        for index in range(119, len(values)):
            expected[index] = (
                pd.Series(values[index - 119:index + 1])
                .ewm(span=20, adjust=False)
                .mean()
                .iloc[-1]
            )
        np.testing.assert_allclose(
            actual[119:], expected[119:], rtol=0, atol=1e-12,
        )


class ExecutionReplayTests(unittest.TestCase):
    def test_entry_uses_next_bar_and_same_bar_ambiguity_is_stop_first(self):
        class FakeMarket:
            fast = pd.DataFrame([
                {
                    "timestamp_ms": 0,
                    "open": 100,
                    "high": 100,
                    "low": 100,
                    "close": 100,
                },
                {
                    "timestamp_ms": BAR_MS,
                    "open": 100,
                    "high": 103,
                    "low": 98,
                    "close": 101,
                },
            ])

            @staticmethod
            def funding_return_pct(entry_ts, exit_ts, direction):
                return 0.0

        candidate = {
            "index": 0,
            "signal_ts": 0,
            "symbol": "BTC/USDT:USDT",
            "direction": "long",
            "setup_type": "range_breakout",
            "funding_rate_pct": 0,
            "plans": {
                "fixed_rr": {"stop_pct": 1.0, "take_pct": 2.0},
            },
        }
        trade = execution_fill(
            FakeMarket(), candidate, SCENARIOS["frictionless"], "fixed_rr",
        )
        self.assertEqual(trade["entry_ts"], BAR_MS)
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertEqual(trade["exit_price"], 99.0)
        self.assertEqual(trade["bars_held"], 1)


if __name__ == "__main__":
    unittest.main()
