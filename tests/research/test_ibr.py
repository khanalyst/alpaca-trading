from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from research.costs import CostModel, ReplayPolicy
from research.ibr import IBRConfig, ReplayError, replay_ibr, replay_ibr_vehicles
from research.costs import BAR, QUOTE
from research.market_data import (normalize_option_snapshot, normalize_quote,
                                  normalize_underlying_bar)


# A zero-cost model isolates the fill geometry under test from the expected
# cost model, which has its own tests.
FREE = CostModel(spread_bps=0, slippage_bps=0, fee_bps=0,
                 option_fee_per_contract_side=0)
PERMISSIVE_POLICY = ReplayPolicy(strict_market_data=False)


def permissive_config(**kwargs):
    """Keep legacy bar-only fixtures explicit under the safe replay default."""
    kwargs.setdefault("policy", PERMISSIVE_POLICY)
    return IBRConfig(**kwargs)


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
    def test_omitted_policy_is_strict_for_bar_only_equity(self):
        result = replay_ibr(bars_for_day(), config=IBRConfig(
            stop_pct=.01, target_pct=.02, costs=FREE))
        self.assertEqual(result.trades, [])

    def test_range_is_completed_and_entry_is_next_bar(self):
        result = replay_ibr(bars_for_day(), config=permissive_config(
            stop_pct=.01, target_pct=.02, costs=FREE))
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
        result = replay_ibr(bars, config=permissive_config(
            stop_pct=.01, target_pct=.02, costs=FREE))
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
        result = replay_ibr(bars, config=permissive_config(
            stop_pct=.01, target_pct=.02, costs=FREE))
        self.assertTrue(result.trades[0].gap_fill)
        self.assertEqual(result.trades[0].exit_price, 95)

    def test_levels_are_anchored_to_the_signal_close_not_the_entry_gap(self):
        # The runtime submits its bracket from the completed breakout bar's
        # close (101), before the gapped entry bar (open 103) exists.
        for gap in (False, True):
            with self.subTest(gap=gap):
                bars = bars_for_day(gap=gap)
                percent = replay_ibr(bars, config=permissive_config(
                    stop_pct=.01, target_pct=.02, costs=FREE)).trades[0]
                self.assertAlmostEqual(percent.stop_price, 99.99, places=9)
                self.assertAlmostEqual(percent.target_price, 103.02, places=9)
                ranged = replay_ibr(bars, config=permissive_config(
                    range_stop=True, target_r=2.0, stop_pct=.01, target_pct=.02,
                    costs=FREE)).trades[0]
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
            "underlying_price": 101, "bid_size": 5, "ask_size": 5,
            "volume": 20, "open_interest": 100,
            "provider": "alpaca", "feed": "opra",
        })
        exit_quote = normalize_option_snapshot({
            **contract, "timestamp": exit_time.isoformat(),
            "bid": 3.0, "ask": 3.1, "last": 3.05,
            "underlying_price": 103, "bid_size": 5, "ask_size": 5,
            "volume": 20, "open_interest": 100,
            "provider": "alpaca", "feed": "opra",
        })
        results = replay_ibr_vehicles(
            bars, config=permissive_config(stop_pct=.01, target_pct=.02, costs=FREE),
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
        result = replay_ibr(bars_to_close(), config=permissive_config(
            stop_pct=.01, target_pct=.02, costs=FREE))
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "force_flat")
        local = result.trades[0].exit_timestamp.astimezone(ZoneInfo("America/New_York"))
        self.assertEqual(local.hour, 15)
        self.assertEqual(local.minute, 55)


def equity_quote(minute, bid, ask):
    """A quote at the given offset from the 09:30 session open."""
    ts = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return normalize_quote({
        "symbol": "SPY", "timestamp": ts.isoformat(), "bid": bid, "ask": ask,
        "provider": "alpaca", "feed": "sip",
    })


def option_quote(timestamp, bid=2.0, ask=2.1, *, expiration="2024-02-16",
                 bid_size=5, ask_size=5, volume=20, open_interest=100):
    expiry = expiration[:10].replace("-", "")
    return normalize_option_snapshot({
        "symbol": f"SPY{expiry[2:]}C00101000", "underlying": "SPY",
        "expiration": expiration, "strike": 101, "right": "call",
        "timestamp": timestamp.isoformat(), "bid": bid, "ask": ask,
        "last": (bid + ask) / 2, "underlying_price": 101,
        "bid_size": bid_size, "ask_size": ask_size, "volume": volume,
        "open_interest": open_interest, "provider": "alpaca", "feed": "opra",
    })


class IBRQuoteFillTests(unittest.TestCase):
    """A recorded quote at the fill instant beats a bar-derived reference."""

    def test_the_entry_uses_the_recorded_ask_and_records_the_source(self):
        # The entry bar (minute 31) opens at 101; the book at that instant is
        # 100.98 x 101.06, so the marketable buy lifts 101.06.
        trade = replay_ibr(bars_for_day(), config=permissive_config(
            stop_pct=.01, target_pct=.02, costs=FREE),
            quotes=[equity_quote(31, 100.98, 101.06)]).trades[0]
        self.assertEqual(trade.entry_fill_source, QUOTE)
        self.assertAlmostEqual(trade.entry_reference, 101.06, places=9)
        self.assertAlmostEqual(trade.entry_price, 101.06, places=9)

    def test_option_liquidity_and_dte_match_runtime_acceptance_boundary(self):
        bars = bars_for_day()
        entry_time = bars[31].timestamp
        exit_time = bars[31].end
        cases = (
            # volume, open interest, or both displayed sides can establish
            # liquidity; one-sided size metadata cannot.
            ({"bid_size": None, "ask_size": None, "volume": 1,
              "open_interest": None}, True),
            ({"bid_size": 1, "ask_size": 1, "volume": None,
              "open_interest": None}, True),
            ({"bid_size": 1, "ask_size": None, "volume": None,
              "open_interest": None}, False),
            # Runtime rejects 0DTE even if options_min_dte is configured to 0.
            ({"expiration": "2024-01-02"}, False),
        )
        for metadata, accepted in cases:
            with self.subTest(metadata=metadata):
                entry = option_quote(entry_time, **metadata)
                exit_quote = option_quote(exit_time, **metadata)
                result = replay_ibr_vehicles(
                    bars, config=permissive_config(stop_pct=.01, target_pct=.02,
                                                   costs=FREE,
                                                   policy=ReplayPolicy(
                                                       strict_market_data=False,
                                                       options_min_dte=0)),
                    vehicles=("option",),
                    option_snapshots={entry_time: entry, exit_time: exit_quote})
                self.assertEqual(bool(result["option"].trades), accepted)

    def test_a_missing_quote_falls_back_to_the_bar_and_says_so(self):
        trade = replay_ibr(bars_for_day(), config=permissive_config(
            stop_pct=.01, target_pct=.02, costs=FREE)).trades[0]
        self.assertEqual(trade.entry_fill_source, BAR)
        self.assertEqual(trade.exit_fill_source, BAR)
        self.assertAlmostEqual(trade.entry_reference, 101, places=9)

    def test_a_quote_after_the_fill_instant_is_not_used(self):
        trade = replay_ibr(bars_for_day(), config=permissive_config(
            stop_pct=.01, target_pct=.02, costs=FREE),
            quotes=[equity_quote(32, 100.98, 101.06)]).trades[0]
        self.assertEqual(trade.entry_fill_source, BAR)

    def test_a_gap_exit_is_priced_from_the_quote_at_the_gap_open(self):
        bars = bars_for_day()
        bars[31] = normalize_underlying_bar({
            "symbol": "SPY", "timestamp": bars[31].timestamp.isoformat(),
            "open": 101, "high": 101.5, "low": 100, "close": 101,
            "volume": 1, "provider": "alpaca", "feed": "sip"})
        bars[32] = normalize_underlying_bar({
            "symbol": "SPY", "timestamp": bars[32].timestamp.isoformat(),
            "open": 95, "high": 96, "low": 94, "close": 95,
            "volume": 1, "provider": "alpaca", "feed": "sip"})
        trade = replay_ibr(bars, config=permissive_config(
            stop_pct=.01, target_pct=.02, costs=FREE),
            quotes=[equity_quote(32, 94.9, 95.1)]).trades[0]
        self.assertTrue(trade.gap_fill)
        self.assertEqual(trade.exit_fill_source, QUOTE)
        # A long exit hits the bid, not the gapped print and not the ask.
        self.assertAlmostEqual(trade.exit_reference, 94.9, places=9)

    def test_a_level_exit_keeps_the_bar_because_it_has_no_fill_instant(self):
        # The stop triggers somewhere inside a bar, so a boundary quote is not
        # the fill's price and must not replace the level.
        bars = bars_for_day()
        bars[31] = normalize_underlying_bar({
            "symbol": "SPY", "timestamp": bars[31].timestamp.isoformat(),
            "open": 101, "high": 101.5, "low": 100, "close": 101,
            "volume": 1, "provider": "alpaca", "feed": "sip"})
        bars[32] = normalize_underlying_bar({
            "symbol": "SPY", "timestamp": bars[32].timestamp.isoformat(),
            "open": 101, "high": 101.5, "low": 99, "close": 100,
            "volume": 1, "provider": "alpaca", "feed": "sip"})
        trade = replay_ibr(bars, config=permissive_config(
            stop_pct=.01, target_pct=.02, costs=FREE),
            quotes=[equity_quote(32, 80.0, 80.1)]).trades[0]
        self.assertEqual(trade.exit_reason, "stop")
        self.assertEqual(trade.exit_fill_source, BAR)
        self.assertAlmostEqual(trade.exit_reference, trade.stop_price, places=9)
