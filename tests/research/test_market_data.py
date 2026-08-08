from datetime import datetime, timezone
import unittest

from research.market_data import (
    NormalizationError,
    normalize_option_contract,
    normalize_quote,
    normalize_underlying_bar,
)


class MarketDataNormalizationTests(unittest.TestCase):
    def test_bar_carries_feed_asof_and_new_york_session(self):
        bar = normalize_underlying_bar({
            "symbol": "SPY",
            "timestamp": "2024-11-04T14:30:00Z",
            "open": 500, "high": 501, "low": 499, "close": 500.5,
            "volume": 10,
            "provider": "alpaca", "feed": "sip",
            "as_of": "2024-11-04T14:30:00Z",
        })
        self.assertEqual(bar.session_date.isoformat(), "2024-11-04")
        self.assertEqual(bar.provider, "alpaca")
        self.assertEqual(bar.feed, "sip")
        self.assertEqual(bar.as_of.tzinfo, timezone.utc)

    def test_dst_session_date_uses_event_timezone_not_utc_date(self):
        # 14:30 UTC is 09:30 EST in winter and 10:30 EDT in summer; both are
        # still the same New York session date.
        bar = normalize_underlying_bar({
            "symbol": "QQQ", "timestamp": "2024-03-11T13:30:00Z",
            "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1,
            "provider": "alpaca", "feed": "iex",
        })
        self.assertEqual(bar.session_date.isoformat(), "2024-03-11")

    def test_naive_timestamp_and_future_asof_fail_closed(self):
        payload = {
            "symbol": "SPY", "timestamp": "2024-01-02T14:30:00",
            "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1,
            "provider": "alpaca", "feed": "sip",
        }
        with self.assertRaises(NormalizationError):
            normalize_underlying_bar(payload)
        payload["timestamp"] += "+00:00"
        payload["as_of"] = "2024-01-02T14:31:00Z"
        with self.assertRaises(NormalizationError):
            normalize_underlying_bar(payload)

    def test_quote_requires_ordered_positive_market(self):
        payload = {
            "symbol": "SPY", "timestamp": "2024-01-02T14:30:00Z",
            "bid": 500, "ask": 500.01,
            "provider": "alpaca", "feed": "sip",
        }
        quote = normalize_quote(payload)
        self.assertLessEqual(quote.bid, quote.ask)
        payload["ask"] = 499
        with self.assertRaises(NormalizationError):
            normalize_quote(payload)

    def test_option_contract_normalizes_right_and_multiplier(self):
        contract = normalize_option_contract({
            "symbol": "SPY240119C00500000", "underlying": "SPY",
            "expiration": "2024-01-19", "strike": 500, "right": "C",
            "provider": "alpaca", "feed": "opra",
        })
        self.assertEqual(contract.right, "call")
        self.assertEqual(contract.multiplier, 100)

