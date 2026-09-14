from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from agent.alpaca_domain import Bar
from agent.market_entry_risk import MarketEntryRiskMixin


UTC = timezone.utc
START = datetime(2026, 9, 14, 13, 30, tzinfo=UTC)


class _Collector(MarketEntryRiskMixin):
    def __init__(self) -> None:
        self.cfg = {"execution": {"max_market_data_age_seconds": 30}}


class _AttributeBar:
    __slots__ = ("symbol", "timestamp", "open", "high", "low", "close", "volume")

    def __init__(self, timestamp: datetime) -> None:
        self.symbol = "SPY"
        self.timestamp = timestamp
        self.open = 100.0
        self.high = 101.0
        self.low = 99.0
        self.close = 100.5
        self.volume = 1000.0


def _bar(timestamp: datetime, **metadata) -> dict:
    row = {
        "symbol": "SPY", "timestamp": timestamp,
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "volume": 1000.0,
    }
    row.update(metadata)
    return row


def _quote(timestamp: datetime) -> dict:
    return {"symbol": "SPY", "timestamp": timestamp,
            "bid": 100.4, "ask": 100.6}


class MarketEntryAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collector = _Collector()

    def _collect(self, bars, now):
        return self.collector._collect(
            ["SPY"], now,
            {"SPY": {"bars": bars, "quote": _quote(now)}})

    def test_completion_boundary_and_bar_age_remain_minute_based(self):
        bar = _bar(
            START, as_of=START + timedelta(minutes=1),
            observed_at=START + timedelta(minutes=1))

        exact = self._collect([bar], START + timedelta(minutes=1))
        self.assertEqual(len(exact["SPY"]["bars"]), 1)
        self.assertEqual(exact["SPY"]["bars"][0]["bar_age_seconds"], 0.0)
        self.assertEqual(exact["SPY"]["bars"][0]["bid"], 100.4)
        self.assertEqual(exact["SPY"]["bars"][0]["ask"], 100.6)

        half_minute_late = self._collect([bar], START + timedelta(minutes=1, seconds=30))
        self.assertEqual(
            half_minute_late["SPY"]["bars"][0]["bar_age_seconds"], 30.0)

    def test_late_bar_is_visible_only_at_recorded_availability(self):
        available_at = START + timedelta(minutes=2)
        bar = _bar(START, as_of=available_at, observed_at=available_at)

        early = self._collect([bar], START + timedelta(minutes=1, seconds=10))
        self.assertEqual(early["SPY"]["bars"], [])

        late = self._collect([bar], available_at)
        self.assertEqual(len(late["SPY"]["bars"]), 1)
        self.assertEqual(late["SPY"]["bars"][0]["bar_age_seconds"], 60.0)

    def test_future_earlier_bar_cannot_contaminate_completed_history(self):
        earlier = _bar(
            START, as_of=START + timedelta(minutes=1),
            observed_at=START + timedelta(minutes=2, seconds=11))
        newest = _bar(
            START + timedelta(minutes=1),
            as_of=START + timedelta(minutes=2),
            observed_at=START + timedelta(minutes=2, seconds=5))

        # Both bars have completed by this point.  The older row must still
        # be excluded because its observation boundary is future-dated; this
        # catches implementations that only reject an unfinished last row.
        result = self._collect(
            [newest, earlier], START + timedelta(minutes=2, seconds=10))
        self.assertEqual(
            [row["timestamp"] for row in result["SPY"]["bars"]],
            [START + timedelta(minutes=1)])

    def test_future_nan_naive_and_malformed_metadata_are_unavailable(self):
        now = START + timedelta(minutes=2)
        cases = (
            ({"as_of": now + timedelta(seconds=1)}, "bar_as_of_future"),
            ({"observed_at": now + timedelta(seconds=1)},
             "bar_observed_at_future"),
            ({"as_of": float("nan")}, "bar_as_of_malformed"),
            ({"observed_at": datetime(2026, 9, 14, 13, 31)},
             "bar_observed_at_malformed"),
            ({"as_of": "not-a-timestamp"}, "bar_as_of_malformed"),
        )
        for metadata, expected_reason in cases:
            with self.subTest(metadata=metadata):
                _available_at, reason = self.collector._observation_availability(
                    _bar(START, **metadata), now, kind="bar")
                self.assertIsNone(_available_at)
                self.assertEqual(reason, expected_reason)
                row = self._collect([_bar(START, **metadata)], now)
                self.assertEqual(row["SPY"]["bars"], [])

    def test_missing_metadata_is_allowed_for_mapping_and_attribute_dtos(self):
        now = START + timedelta(minutes=1)
        mapping_result = self._collect([_bar(START)], now)
        self.assertEqual(len(mapping_result["SPY"]["bars"]), 1)

        attribute_result = self._collect([_AttributeBar(START)], now)
        self.assertEqual(len(attribute_result["SPY"]["bars"]), 1)
        self.assertEqual(attribute_result["SPY"]["bars"][0]["symbol"], "SPY")

        # This is the provider-neutral DTO returned by the real Alpaca
        # boundary.  It intentionally has no availability metadata.
        provider_dto = Bar(
            symbol="SPY", timestamp=START, open=Decimal("100"),
            high=Decimal("101"), low=Decimal("99"), close=Decimal("100.5"),
            volume=Decimal("1000"))
        provider_result = self._collect([provider_dto], now)
        self.assertEqual(len(provider_result["SPY"]["bars"]), 1)

    def test_quote_observation_boundaries_are_causal_and_well_formed(self):
        now = START + timedelta(minutes=1)
        for field in ("as_of", "observed_at"):
            for value in (now + timedelta(seconds=1), float("nan"),
                          now.replace(tzinfo=None), "not-a-timestamp", True):
                with self.subTest(field=field, value=value):
                    quote = {**_quote(now), field: value}
                    result = self.collector._collect(["SPY"], now, {
                        "SPY": {"bars": [_bar(START)], "quote": quote}})
                    self.assertNotIn("SPY", result)
            with self.subTest(field=field, boundary="exact"):
                quote = {**_quote(now), field: now}
                result = self.collector._collect(["SPY"], now, {
                    "SPY": {"bars": [_bar(START)], "quote": quote}})
                self.assertEqual(result["SPY"]["quote"][field], now)
                self.assertEqual(result["SPY"]["quote_age_seconds"], 0.0)

    def test_unavailable_quote_cannot_replace_available_executable_prices(self):
        now = START + timedelta(minutes=1)
        available = _quote(now - timedelta(seconds=1))
        unavailable = {**_quote(now), "bid": 199.0, "ask": 201.0,
                       "observed_at": now + timedelta(seconds=1)}
        result = self.collector._collect(["SPY"], now, {"SPY": {
            "bars": [_bar(START)], "quotes": [available, unavailable]}})
        self.assertEqual(result["SPY"]["quote"]["bid"], 100.4)
        self.assertEqual(result["SPY"]["quote"]["ask"], 100.6)
        self.assertEqual(result["SPY"]["bars"][0]["bid"], 100.4)
        self.assertEqual(result["SPY"]["quote_age_seconds"], 1.0)


if __name__ == "__main__":
    unittest.main()
