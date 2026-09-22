"""The replay lane and the live contract must admit the same candidate bars.

Before the runtime admission filters were mapped into ``research.ibr``, the
two lanes shared only ``range_minutes``, ``target_r`` and
``breakout_buffer_bps``.  A replayed IBR result therefore described a
different strategy from the one the runtime would execute, and the only
positive IBR result the project ever recorded came from the permissive lane.
These tests pin the two admission decisions to one another.
"""

from datetime import datetime, timedelta, timezone
import unittest

from agent.contracts.ibr import build_ibr_range, evaluate_ibr_breakout
from research.costs import ReplayPolicy
from research.ibr import IBRConfig, replay_ibr
from research.market_data import normalize_underlying_bar


OPEN = datetime(2026, 3, 11, 13, 30, tzinfo=timezone.utc)


def bar(index, *, high, low, close, volume=1_000.0, opened=None):
    stamp = OPEN + timedelta(minutes=index)
    return {"symbol": "SPY", "timestamp": stamp.isoformat(),
            "open": opened if opened is not None else close,
            "high": high, "low": low, "close": close, "volume": volume,
            "provider": "alpaca", "feed": "iex",
            "session_date": "2026-03-11"}


def session(*, breakout_close, breakout_volume, range_half_width=0.5):
    """Fifteen flat range minutes, then one bar that breaks the high."""
    rows = [bar(i, high=100 + range_half_width, low=100 - range_half_width,
                close=100.0) for i in range(15)]
    rows.append(bar(15, high=breakout_close + .05, low=99.9,
                    close=breakout_close, volume=breakout_volume))
    rows.extend(bar(i, high=breakout_close + .05, low=breakout_close - .05,
                    close=breakout_close) for i in range(16, 40))
    return rows


def contract_admits(rows, strategy):
    opening = build_ibr_range(rows[:15])
    candidate = dict(rows[15])
    candidate["timestamp"] = datetime.fromisoformat(candidate["timestamp"])
    candidate["history"] = [
        {**row, "timestamp": datetime.fromisoformat(row["timestamp"])}
        for row in rows[:16]]
    return evaluate_ibr_breakout(opening, candidate,
                                 config={"strategy": strategy},
                                 symbol="SPY") is not None


def replay_admits(rows, strategy):
    bars = [normalize_underlying_bar(row, provider="alpaca", feed="iex")
            for row in rows]
    # These fixtures carry no quotes: the subject here is admission, not fill
    # pricing, so the bar-fallback diagnostic policy keeps a strict-lane
    # ``no_quote_at_entry`` refusal from masking the filter decision.
    result = replay_ibr(bars, symbol="SPY", config=IBRConfig(
        policy=ReplayPolicy(strict_market_data=False),
        range_minutes=15, close_confirmed=True, range_stop=True,
        breakout_buffer_bps=float(strategy.get("breakout_buffer_bps", 5.0)),
        min_relative_volume=float(strategy.get("min_relative_volume", 0.0)),
        min_ibr_width_atr=float(strategy.get("min_ibr_width_atr", 0.0)),
        max_ibr_width_atr=float(strategy.get("max_ibr_width_atr", float("inf"))),
        atr_period=int(strategy.get("atr_period", 14)),
        max_entry_extension_r=float(
            strategy.get("max_entry_extension_r", float("inf"))),
    ))
    return bool(result.trades)


class IBRLaneParityTests(unittest.TestCase):
    def test_relative_volume_filter_reaches_both_lanes(self):
        strategy = {"range_minutes": 15, "breakout_buffer_bps": 5.0,
                    "min_relative_volume": 3.0, "atr_period": 14}
        thin = session(breakout_close=101.0, breakout_volume=1_000.0)
        self.assertFalse(contract_admits(thin, strategy))
        self.assertFalse(replay_admits(thin, strategy),
                         "replay ignored min_relative_volume")

    def test_entry_extension_filter_reaches_both_lanes(self):
        strategy = {"range_minutes": 15, "breakout_buffer_bps": 5.0,
                    "min_relative_volume": 0.0, "atr_period": 14,
                    "max_entry_extension_r": 0.5}
        # Range is 1.00 wide; closing at 103 is three ranges beyond the high.
        far = session(breakout_close=103.0, breakout_volume=5_000.0)
        self.assertFalse(contract_admits(far, strategy))
        self.assertFalse(replay_admits(far, strategy),
                         "replay ignored max_entry_extension_r")

    def test_an_admissible_breakout_is_taken_by_both_lanes(self):
        strategy = {"range_minutes": 15, "breakout_buffer_bps": 5.0,
                    "min_relative_volume": 1.0, "atr_period": 14,
                    "max_entry_extension_r": 5.0}
        good = session(breakout_close=101.0, breakout_volume=5_000.0)
        self.assertTrue(contract_admits(good, strategy))
        self.assertTrue(replay_admits(good, strategy),
                        "replay refused a breakout the contract admits")

    def test_width_band_is_horizon_scaled_in_both_lanes(self):
        # The band divides range width by ``atr * sqrt(range_minutes)``.  A
        # raw one-minute ATR quotient would exceed any authored upper bound.
        strategy = {"range_minutes": 15, "breakout_buffer_bps": 5.0,
                    "min_relative_volume": 0.0, "atr_period": 14,
                    "min_ibr_width_atr": 0.0, "max_ibr_width_atr": 0.01}
        rows = session(breakout_close=101.0, breakout_volume=5_000.0)
        self.assertFalse(contract_admits(rows, strategy))
        self.assertFalse(replay_admits(rows, strategy),
                         "replay ignored the width/ATR band")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
