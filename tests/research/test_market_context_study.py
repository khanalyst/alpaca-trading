from datetime import datetime, timedelta, timezone
import unittest
from research.market_context_study import context_study, factor_beta


def session(day, count=390, symbol="SPY", price=100):
    opened = datetime(2026, 9, day, 13, 30, tzinfo=timezone.utc)
    return [{"symbol": symbol, "provider": "alpaca", "feed": "iex",
        "timestamp": (opened+timedelta(minutes=i)).isoformat(),
        "as_of": (opened+timedelta(minutes=i+1)).isoformat(),
        "observed_at": (opened+timedelta(minutes=i+1, seconds=1)).isoformat(),
        "session_open": opened.isoformat(), "session_close": (opened+timedelta(minutes=390)).isoformat(),
        "open": price, "high": price+1, "low": price-1, "close": price,
        "volume": 10, "source_mode": "forward_observed"} for i in range(count)]


class ContextStudyTests(unittest.TestCase):
    def test_current_day_never_enters_prior_close_and_thin_volume_is_unknown(self):
        result = context_study(session(8)+session(9, 5, price=101),
            as_of="2026-09-09T13:35:10+00:00", symbol="SPY", universe=["SPY", "QQQ"], feed="iex")
        self.assertEqual(result["prior_session"]["close"], 100)
        self.assertAlmostEqual(result["overnight_gap_bps"], 100)
        self.assertIsNone(result["same_clock_relative_volume"])
        self.assertEqual(result["universe_participation"]["missing_symbols"], ["QQQ"])

    def test_late_received_prior_history_is_not_forward_context(self):
        previous = session(8)
        for row in previous:
            row.update(observed_at="2026-09-10T00:00:00+00:00", source_mode="historical_backfill")
        args = dict(as_of="2026-09-09T13:35:10+00:00", symbol="SPY", universe=["SPY"], feed="iex")
        self.assertIsNone(context_study(previous+session(9, 5), **args)["prior_session"])
        self.assertIsNotNone(context_study(previous+session(9, 5), allow_backfill=True, **args)["prior_session"])

    def test_beta_uses_synchronized_within_session_returns(self):
        subject, benchmark = [], []
        for day in range(8, 13):
            a, b = session(day, 50, price=100), session(day, 50, symbol="QQQ", price=100)
            x = y = 100
            for i, (left, right) in enumerate(zip(a, b)):
                move = .001 if i % 2 else -.0005
                x *= 1 + move
                y *= 1 + 2 * move
                left["close"], right["close"] = x, y
            benchmark.extend(a)
            subject.extend(b)
        result = factor_beta(subject, benchmark)
        self.assertAlmostEqual(result["beta"], 2, places=8)
        self.assertIsNone(factor_beta(subject[:3], benchmark)["beta"])


if __name__ == "__main__":
    unittest.main()
