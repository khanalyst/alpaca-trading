from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from research.ibr import IBRConfig, ReplayError, replay_ibr, replay_ibr_vehicles
from research.market_data import normalize_option_snapshot, normalize_underlying_bar


def bars_for_day(*, breakout=True, gap=False):
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    rows = []
    for i in range(30):
        rows.append((start + timedelta(minutes=i), 100, 101, 99, 100))
    # Completed breakout bar; it is observed before the following entry bar.
    rows.append((start + timedelta(minutes=30), 100, 102 if breakout else 100,
                 99, 101))
    rows.append((start + timedelta(minutes=31), 103 if gap else 101,
                 104, 100, 103))
    rows.append((start + timedelta(minutes=32), 103, 105, 103, 104))
    return [normalize_underlying_bar({
        "symbol": "SPY", "timestamp": ts.isoformat(), "open": o,
        "high": h, "low": l, "close": c, "volume": 1,
        "provider": "alpaca", "feed": "sip",
    }) for ts, o, h, l, c in rows]


def bars_to_close():
    rows = bars_for_day()
    # Keep the breakout alive but do not hit either level, then provide the
    # explicit 15:55 boundary bar.
    rows[31] = normalize_underlying_bar({
        "symbol": "SPY", "timestamp": rows[31].timestamp.isoformat(),
        "open": 101, "high": 101.5, "low": 100, "close": 101,
        "volume": 1, "provider": "alpaca", "feed": "sip",
    })
    rows[32] = normalize_underlying_bar({
        "symbol": "SPY", "timestamp": rows[32].timestamp.isoformat(),
        "open": 101, "high": 101.5, "low": 100, "close": 101,
        "volume": 1, "provider": "alpaca", "feed": "sip",
    })
    while len(rows) < 386:  # 09:30 through 15:55 inclusive
        ts = rows[-1].timestamp + timedelta(minutes=1)
        rows.append(normalize_underlying_bar({
            "symbol": "SPY", "timestamp": ts.isoformat(), "open": 101,
            "high": 101.5, "low": 100, "close": 101, "volume": 1,
            "provider": "alpaca", "feed": "sip",
        }))
    return rows


class IBRReplayTests(unittest.TestCase):
    def test_range_is_completed_and_entry_is_next_bar(self):
        result = replay_ibr(bars_for_day(), config=IBRConfig(
            stop_pct=.01, target_pct=.02, spread_bps=0, slippage_bps=0, fee_bps=0))
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.signal_timestamp, trade.entry_timestamp)
        self.assertEqual(trade.exit_reason, "target")

    def test_same_bar_stop_and_target_is_stop_first(self):
        bars = bars_for_day()
        # Replace post-entry bar with one spanning both levels.
        original = bars[-1]
        bars[31] = normalize_underlying_bar({
            "symbol": "SPY", "timestamp": bars[31].timestamp.isoformat(),
            "open": 101, "high": 104, "low": 98, "close": 100,
            "volume": 1, "provider": "alpaca", "feed": "sip",
        })
        result = replay_ibr(bars, config=IBRConfig(
            stop_pct=.01, target_pct=.02, spread_bps=0, slippage_bps=0, fee_bps=0))
        self.assertEqual(result.trades[0].exit_reason, "stop")
        self.assertTrue(result.trades[0].tie_broken)

    def test_gap_through_stop_records_open_fill(self):
        bars = bars_for_day()
        bars[31] = normalize_underlying_bar({
            "symbol": "SPY", "timestamp": bars[31].timestamp.isoformat(),
            "open": 101, "high": 101.5, "low": 100, "close": 101,
            "volume": 1, "provider": "alpaca", "feed": "sip",
        })
        bars[32] = normalize_underlying_bar({
            "symbol": "SPY", "timestamp": bars[32].timestamp.isoformat(),
            "open": 95, "high": 96, "low": 94, "close": 95,
            "volume": 1, "provider": "alpaca", "feed": "sip",
        })
        result = replay_ibr(bars, config=IBRConfig(
            stop_pct=.01, target_pct=.02, spread_bps=0, slippage_bps=0, fee_bps=0))
        self.assertTrue(result.trades[0].gap_fill)
        self.assertEqual(result.trades[0].exit_price, 95)

    def test_levels_are_anchored_to_the_signal_close_not_the_entry_gap(self):
        # The runtime submits its bracket from the completed breakout bar's
        # close (101), before the gapped entry bar (open 103) exists.
        for gap in (False, True):
            with self.subTest(gap=gap):
                bars = bars_for_day(gap=gap)
                percent = replay_ibr(bars, config=IBRConfig(
                    stop_pct=.01, target_pct=.02, spread_bps=0,
                    slippage_bps=0, fee_bps=0)).trades[0]
                self.assertAlmostEqual(percent.stop_price, 99.99, places=9)
                self.assertAlmostEqual(percent.target_price, 103.02, places=9)
                ranged = replay_ibr(bars, config=IBRConfig(
                    range_stop=True, target_r=2.0, stop_pct=.01, target_pct=.02,
                    spread_bps=0, slippage_bps=0, fee_bps=0)).trades[0]
                self.assertAlmostEqual(ranged.stop_price, 99.0, places=9)
                self.assertAlmostEqual(ranged.target_price, 105.0, places=9)
                self.assertEqual(percent.entry_reference, 103 if gap else 101)

    def test_equity_and_option_results_are_not_pooled(self):
        bars = bars_for_day()
        contract = {
            "symbol": "SPY240216C00101000", "underlying": "SPY",
            "expiration": "2024-02-16", "strike": 101,
            "right": "call", "multiplier": 100,
        }
        entry_time = bars[31].timestamp
        exit_time = bars[31].end
        entry = normalize_option_snapshot({
            **contract, "timestamp": entry_time.isoformat(),
            "bid": 1.9, "ask": 2.0, "last": 1.95,
            "underlying_price": 101, "provider": "alpaca", "feed": "opra",
        })
        exit_quote = normalize_option_snapshot({
            **contract, "timestamp": exit_time.isoformat(),
            "bid": 3.0, "ask": 3.1, "last": 3.05,
            "underlying_price": 103, "provider": "alpaca", "feed": "opra",
        })
        results = replay_ibr_vehicles(
            bars, config=IBRConfig(stop_pct=.01, target_pct=.02,
                                   spread_bps=0, slippage_bps=0, fee_bps=0),
            vehicles=("equity", "option"),
            option_snapshots={entry_time: entry, exit_time: exit_quote})
        self.assertEqual(set(results), {"equity", "option"})
        self.assertEqual(results["equity"].vehicle, "equity")
        self.assertEqual(results["option"].vehicle, "option")
        self.assertEqual(len(results["option"].trades), 1)
        self.assertEqual(results["option"].trades[0].contract_multiplier, 100)
        self.assertAlmostEqual(results["option"].net_pnl, 100.0)
        self.assertNotEqual(results["equity"].net_pnl,
                            results["option"].net_pnl)

    def test_missing_immediate_next_bar_has_no_trade(self):
        bars = bars_for_day()
        bars.pop(31)
        self.assertEqual(len(replay_ibr(bars).trades), 0)

    def test_invalid_vehicle_rejected(self):
        with self.assertRaises(ReplayError):
            replay_ibr(bars_for_day(), vehicle="pooled")

    def test_reversed_input_is_rejected_instead_of_sorted(self):
        bars = bars_for_day()
        with self.assertRaises(ReplayError):
            replay_ibr([bars[1], bars[0]])

    def test_force_flat_uses_boundary_open_not_intrabar_range(self):
        result = replay_ibr(bars_to_close(), config=IBRConfig(
            stop_pct=.01, target_pct=.02, spread_bps=0, slippage_bps=0, fee_bps=0))
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "force_flat")
        local = result.trades[0].exit_timestamp.astimezone(ZoneInfo("America/New_York"))
        self.assertEqual(local.hour, 15)
        self.assertEqual(local.minute, 55)
