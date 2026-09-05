"""The fitted cost schedule must report what the corpus contains.

Every assertion here is against a corpus whose true spread and depth are known
by construction, so the fit is checked rather than trusted.  The point of this
module is to replace an assumption with a measurement; a measurement that
cannot be verified is just a second assumption.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from agent.config import ConfigError, validate_config
from research.costs import CostError
from research.edge_discovery_core import _effective_ibr_config
from research.edge_identity import candidate_assumptions
from research.edge_ledger import content_hash
from research.quote_costs import (QUOTE_COST_SCHEMA, QuoteCostError,
                                  bucket_label, cost_model_from_schedule,
                                  cost_resolver_setup, measure_quote_costs,
                                  measured_cost_resolver,
                                  schedule_costs_block,
                                  validate_measured_quote_config)

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
        self.assertEqual(len(buckets["m000_030"]["sessions"]), 40)
        self.assertEqual(buckets["m000_030"]["sessions"][0], "2026-01-05")

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

    def test_a_usable_quote_without_provider_is_refused(self):
        rows = _quotes("SPY", spread_bps=1.0)
        rows[0].pop("provider")
        with self.assertRaises(QuoteCostError):
            measure_quote_costs(rows)

    def test_a_usable_quote_without_feed_is_refused(self):
        rows = _quotes("SPY", spread_bps=1.0)
        rows[0].pop("feed")
        with self.assertRaises(QuoteCostError):
            measure_quote_costs(rows)

    def test_measurement_and_content_hash_are_input_order_independent(self):
        rows = (_quotes("SPY", spread_bps=0.18, minutes=35) +
                _quotes("XLE", spread_bps=1.1, minutes=35))
        forward = measure_quote_costs(rows, min_quotes_per_cell=1)
        reverse = measure_quote_costs(reversed(rows), min_quotes_per_cell=1)
        self.assertEqual(forward, reverse)

    def test_sparse_buckets_are_excluded_and_counted(self):
        schedule = measure_quote_costs(
            _quotes("SPY", spread_bps=1.0, minutes=120, sessions=1),
            min_quotes_per_cell=1_000)
        entry = schedule["symbols"]["SPY"]
        self.assertEqual(entry["buckets"], {})
        self.assertGreater(entry["sparse_buckets"], 0)

    def test_a_requested_sparse_bucket_fails_closed_instead_of_using_symbol_aggregate(self):
        schedule = measure_quote_costs(
            _quotes("SPY", spread_bps=1.0, minutes=30, sessions=20) +
            _quotes("SPY", spread_bps=9.0, minutes=30, sessions=1,
                    start_minute=30),
            min_quotes_per_cell=500)
        self.assertIn("m000_030", schedule["symbols"]["SPY"]["buckets"])
        self.assertNotIn("m030_060", schedule["symbols"]["SPY"]["buckets"])
        with self.assertRaisesRegex(
                QuoteCostError,
                r"SPY/m030_060.*unavailable.*under-covered.*symbol-wide"):
            cost_model_from_schedule(schedule, symbol="SPY",
                                     bucket="m030_060")

    def test_a_dense_requested_bucket_is_used_and_provenance_is_specific(self):
        schedule = measure_quote_costs(
            _quotes("SPY", spread_bps=1.0, minutes=30, sessions=20),
            min_quotes_per_cell=500)
        model = cost_model_from_schedule(schedule, symbol="SPY",
                                         bucket="m000_030")
        self.assertAlmostEqual(model.spread_bps, 1.0, delta=0.05)
        self.assertIn("symbol_bucket:SPY:m000_030", model.provenance)

    def test_a_bucket_request_without_a_symbol_fails_closed(self):
        schedule = measure_quote_costs(
            _quotes("SPY", spread_bps=1.0, minutes=30, sessions=20),
            min_quotes_per_cell=500)
        with self.assertRaisesRegex(
                QuoteCostError,
                r"m000_030.*has no symbol.*universe fallback"):
            cost_model_from_schedule(schedule, bucket="m000_030")


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

    def test_a_tampered_schedule_hash_is_refused(self):
        tampered = deepcopy(self.schedule)
        tampered["symbols"]["SPY"]["spread_bps"]["p75"] = 99.0
        with self.assertRaisesRegex(QuoteCostError, "hash"):
            cost_model_from_schedule(tampered, symbol="SPY")

    def test_measured_resolver_rejects_option_vehicle(self):
        with self.assertRaisesRegex(QuoteCostError, "equity only"):
            measured_cost_resolver(self.schedule, vehicle="option")

    def test_measured_resolver_rejects_unparsable_supplied_timestamps(self):
        resolver = measured_cost_resolver(self.schedule)
        for field in ("cost_timestamp", "entry_timestamp", "timestamp"):
            for value in ("not-a-timestamp", " "):
                with self.subTest(field=field, value=repr(value)):
                    with self.assertRaisesRegex(QuoteCostError, "timestamp"):
                        resolver({"symbol": "SPY", field: value})

    def test_measured_resolver_keeps_symbol_fallback_without_timestamp(self):
        model = measured_cost_resolver(self.schedule)({"symbol": "SPY"})
        self.assertIn("symbol:SPY", model.provenance)

    def test_the_costs_block_round_trips_into_a_replay_config(self):
        from research.costs import CostModel
        block = schedule_costs_block(self.schedule, symbol="SPY")
        rebuilt = CostModel.from_config({"costs": block})
        self.assertAlmostEqual(rebuilt.spread_bps,
                               self.schedule["symbols"]["SPY"]["spread_bps"]["p75"])


class MeasuredConfigIntegrationTests(unittest.TestCase):
    """The validated schedule is shared by every authorizing replay lane."""

    @classmethod
    def setUpClass(cls):
        cls.schedule = measure_quote_costs(
            _quotes("SPY", spread_bps=1.0, minutes=30, sessions=20),
            feed="iex", provider="test")

    def _config(self, measured=None):
        return validate_config({
            "broker": {"data_feed": "iex"},
            "costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                      "fee_bps": 0.5,
                      "measured_quote": (measured or {
                          "enabled": True,
                          "schedule": self.schedule,
                          "percentile": "p75",
                          "depth_percentile": "p25",
                          "min_quotes_per_cell": 500,
                          "coverage_policy": "strict",
                      })},
        })

    def test_enabled_config_embeds_schedule_and_resolver_provenance(self):
        config = self._config()
        block = config["costs"]["measured_quote"]
        self.assertEqual(block["schedule_hash"], self.schedule["schedule_hash"])
        self.assertNotIn("schedule_path", block)
        setup = cost_resolver_setup(config, vehicle="equity")
        model = setup.resolver({
            "vehicle": "equity", "symbol": "SPY", "shares": 10,
            "cost_leg": "entry", "cost_timestamp": "2026-01-05T14:45:00+00:00",
        })
        self.assertIn("symbol_bucket:SPY:m000_030", model.provenance)
        self.assertIn("spread-p75", model.provenance)
        self.assertIn("depth-p25", model.provenance)
        self.assertIn("feed-iex:provider-test:coverage-strict", model.provenance)

    def test_declared_hash_and_provider_feed_are_checked_at_config_boundary(self):
        bad_hash = {"enabled": True, "schedule": self.schedule,
                    "schedule_hash": "not-the-schedule-hash",
                    "provider": "test"}
        with self.assertRaisesRegex(ConfigError, "declared schedule_hash"):
            self._config(bad_hash)
        with self.assertRaisesRegex(ConfigError, "schedule feed"):
            validate_config({
                "broker": {"data_feed": "sip"},
                "costs": {"measured_quote": {
                    "enabled": True, "schedule": self.schedule,
                    "provider": "test"}},
            })

    def test_disabled_path_is_static_fallback_without_loading_filesystem(self):
        block = validate_measured_quote_config({
            "enabled": False,
            "schedule_path": "/a/path/that/must/not/be/opened.json",
        })
        self.assertFalse(block["enabled"])
        self.assertEqual(block["schedule_path"],
                         "/a/path/that/must/not/be/opened.json")

    def test_disabled_placeholder_does_not_bind_the_active_broker_feed(self):
        config = validate_config({
            "broker": {"data_feed": "sip"},
            "costs": {"measured_quote": {
                "enabled": False, "feed": "iex", "provider": "alpaca",
            }},
        })
        self.assertEqual(config["broker"]["data_feed"], "sip")
        self.assertIsNone(cost_resolver_setup(config).resolver)

    def test_schedule_path_is_verified_once_and_embedded_for_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            path.write_text(json.dumps(self.schedule), encoding="utf-8")
            config = self._config({
                "enabled": True, "schedule_path": str(path),
                "schedule_hash": self.schedule["schedule_hash"],
                "min_quotes_per_cell": 500,
            })
            block = config["costs"]["measured_quote"]
            self.assertNotIn("schedule_path", block)
            self.assertEqual(block["schedule"], self.schedule)
            # Later path mutation cannot change replay economics or identity.
            path.write_text("{}", encoding="utf-8")
            setup = cost_resolver_setup(config)
            self.assertEqual(
                setup.measured["schedule_hash"], self.schedule["schedule_hash"])

    def test_disabled_measurement_does_not_create_a_new_candidate_identity(self):
        base = validate_config({})
        disabled = validate_config({"costs": {"measured_quote": {
            "enabled": False, "feed": "iex", "provider": "alpaca",
        }}})
        left = candidate_assumptions(
            base, vehicle="equity", strategy_id="ibr",
            variant_id="ibr.baseline")
        right = candidate_assumptions(
            disabled, vehicle="equity", strategy_id="ibr",
            variant_id="ibr.baseline")
        self.assertEqual(content_hash(left), content_hash(right))

    def test_effective_config_and_candidate_identity_retain_measured_economics(self):
        config = self._config()
        _cfg, effective = _effective_ibr_config(config, {})
        self.assertEqual(
            effective["costs"]["measured_quote"]["schedule_hash"],
            self.schedule["schedule_hash"])
        assumptions = candidate_assumptions(
            effective, vehicle="equity", strategy_id="ibr",
            variant_id="ibr.baseline")
        self.assertEqual(
            assumptions["costs"]["measured_quote"]["schedule_hash"],
            self.schedule["schedule_hash"])
        changed = dict(assumptions)
        changed["costs"] = dict(assumptions["costs"])
        changed["costs"]["measured_quote"] = dict(
            assumptions["costs"]["measured_quote"])
        changed["costs"]["measured_quote"]["percentile"] = "median"
        self.assertNotEqual(content_hash(assumptions), content_hash(changed))


if __name__ == "__main__":
    unittest.main()
