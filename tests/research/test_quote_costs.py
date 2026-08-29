"""The fitted cost schedule must report what the corpus contains.

Every assertion here is against a corpus whose true spread and depth are known
by construction, so the fit is checked rather than trusted.  The point of this
module is to replace an assumption with a measurement; a measurement that
cannot be verified is just a second assumption.
"""

from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from research.costs import CostError
from research.quote_costs import (QUOTE_COST_SCHEMA, QuoteCostError,
                                  bucket_label, cost_model_from_schedule,
                                  measure_quote_costs, schedule_costs_block)

NEW_YORK = ZoneInfo("America/New_York")


def _quotes(symbol: str, *, spread_bps: float, price: float = 100.0,
            size: float = 1000.0, minutes: int = 120, sessions: int = 2,
            start_minute: int = 0, feed: str = "iex") -> list[dict]:
    """Quotes with an exactly known spread, one per minute from *start_minute*."""
    rows = []
    base = datetime(2026, 1, 5, 9, 30, tzinfo=NEW_YORK)
    for day in range(sessions):
        opening = base + timedelta(days=day)
        for index in range(start_minute, start_minute + minutes):
            stamp = (opening + timedelta(minutes=index)).astimezone(timezone.utc)
            half = price * spread_bps / 10_000.0 / 2.0
            rows.append({
                "kind": "quote", "symbol": symbol, "feed": feed,
                "provider": "test", "timestamp": stamp.isoformat(),
                "bid": price - half, "ask": price + half,
                "bid_size": size, "ask_size": size,
            })
    return rows


class MeasurementTests(unittest.TestCase):
    def test_the_measured_spread_matches_the_corpus(self):
        schedule = measure_quote_costs(_quotes("SPY", spread_bps=0.18) +
                                       _quotes("XLE", spread_bps=1.1))
        self.assertEqual(schedule["schema"], QUOTE_COST_SCHEMA)
        for symbol, expected in (("SPY", 0.18), ("XLE", 1.1)):
            measured = schedule["symbols"][symbol]["spread_bps"]
            self.assertAlmostEqual(measured["mean"], expected, places=6)
            # Percentiles are histogram bin edges, so they land within one bin.
            self.assertLess(abs(measured["median"] - expected), 0.05)

    def test_depth_is_reported_in_shares_from_the_thinner_side(self):
        rows = _quotes("SPY", spread_bps=1.0, size=800.0)
        for row in rows:
            row["ask_size"] = 5_000.0
        schedule = measure_quote_costs(rows)
        shares = schedule["symbols"]["SPY"]["touch_shares"]
        self.assertLess(abs(shares["median"] - 800.0), 40.0)

    def test_time_of_day_buckets_capture_a_widening_open(self):
        # The first half hour is wide; everything after it is tight.
        wide = _quotes("SPY", spread_bps=6.0, minutes=30, sessions=40)
        tight = _quotes("SPY", spread_bps=0.5, minutes=90, sessions=40,
                        start_minute=30)
        self.assertEqual(bucket_label(0), "m000_030")
        buckets = measure_quote_costs(wide + tight)["symbols"]["SPY"]["buckets"]
        self.assertGreater(buckets["m000_030"]["spread_bps"]["mean"], 5.0)
        self.assertLess(buckets["m030_060"]["spread_bps"]["mean"], 1.0)

    def test_malformed_quotes_are_rejected_not_absorbed(self):
        rows = _quotes("SPY", spread_bps=1.0, minutes=10)
        rows += [{"kind": "quote", "symbol": "SPY", "timestamp":
                  rows[0]["timestamp"], "bid": 100.0, "ask": 99.0},
                 {"kind": "quote", "symbol": "SPY", "timestamp":
                  rows[0]["timestamp"], "bid": 0.0, "ask": 1.0}]
        schedule = measure_quote_costs(rows)
        self.assertEqual(schedule["measured"]["quote_rows_rejected"], 2)
        self.assertAlmostEqual(
            schedule["symbols"]["SPY"]["spread_bps"]["mean"], 1.0, places=6)

    def test_an_empty_corpus_fails_closed(self):
        with self.assertRaises(QuoteCostError):
            measure_quote_costs([])

    def test_a_feed_mismatch_is_refused(self):
        with self.assertRaises(QuoteCostError):
            measure_quote_costs(_quotes("SPY", spread_bps=1.0, feed="sip"),
                                feed="iex")

    def test_sparse_buckets_are_excluded_and_counted(self):
        schedule = measure_quote_costs(
            _quotes("SPY", spread_bps=1.0, minutes=120, sessions=1),
            min_quotes_per_cell=1_000)
        entry = schedule["symbols"]["SPY"]
        self.assertEqual(entry["buckets"], {})
        self.assertGreater(entry["sparse_buckets"], 0)


class CostModelConstructionTests(unittest.TestCase):
    def setUp(self):
        self.schedule = measure_quote_costs(
            _quotes("SPY", spread_bps=0.18, size=2_000.0) +
            _quotes("XLE", spread_bps=1.1, size=600.0))

    def test_a_model_built_from_the_schedule_carries_traceable_provenance(self):
        model = cost_model_from_schedule(self.schedule, symbol="SPY")
        self.assertTrue(model.provenance.startswith("measured:"))
        self.assertIn("symbol:SPY", model.provenance)
        self.assertIn("p75", model.provenance)

    def test_the_measured_round_trip_is_far_below_the_shipped_constants(self):
        """The shipped 17 bps is the number this exists to replace."""
        model = cost_model_from_schedule(self.schedule, symbol="SPY")
        shipped = 2 * (4.0 / 2 + 6.0) + 2 * 0.5
        measured = 2 * model.entry_cost_bps + 2 * model.fee_bps
        self.assertLess(measured, shipped / 5.0)

    def test_a_wider_symbol_gets_a_wider_model(self):
        tight = cost_model_from_schedule(self.schedule, symbol="SPY")
        wide = cost_model_from_schedule(self.schedule, symbol="XLE")
        self.assertGreater(wide.spread_bps, tight.spread_bps)

    def test_size_beyond_displayed_depth_is_charged(self):
        inside = cost_model_from_schedule(self.schedule, symbol="XLE",
                                          order_shares=100.0)
        outside = cost_model_from_schedule(self.schedule, symbol="XLE",
                                           order_shares=6_000.0)
        self.assertEqual(inside.slippage_bps, 0.0)
        self.assertGreater(outside.slippage_bps, 0.0)

    def test_the_impact_charge_is_bounded(self):
        huge = cost_model_from_schedule(self.schedule, symbol="XLE",
                                        order_shares=10_000_000.0,
                                        max_impact_half_spreads=4.0)
        self.assertAlmostEqual(huge.slippage_bps, huge.spread_bps / 2.0 * 4.0)

    def test_a_higher_percentile_is_never_cheaper(self):
        median = cost_model_from_schedule(self.schedule, symbol="XLE",
                                          percentile="median")
        p95 = cost_model_from_schedule(self.schedule, symbol="XLE",
                                       percentile="p95")
        self.assertGreaterEqual(p95.spread_bps, median.spread_bps)

    def test_a_measured_model_still_obeys_the_runtime_caps(self):
        wide = measure_quote_costs(_quotes("ZZZ", spread_bps=400.0))
        with self.assertRaises(CostError):
            cost_model_from_schedule(wide, symbol="ZZZ")

    def test_a_foreign_schema_is_refused(self):
        with self.assertRaises(QuoteCostError):
            cost_model_from_schedule({"schema": "something-else.v1"})

    def test_the_costs_block_round_trips_into_a_replay_config(self):
        from research.costs import CostModel
        block = schedule_costs_block(self.schedule, symbol="SPY")
        rebuilt = CostModel.from_config({"costs": block})
        self.assertAlmostEqual(rebuilt.spread_bps,
                               self.schedule["symbols"]["SPY"]["spread_bps"]["p75"])


if __name__ == "__main__":
    unittest.main()
