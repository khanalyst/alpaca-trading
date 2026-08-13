from datetime import datetime, timezone
from decimal import Decimal
import unittest

from research.market_data import (
    EventIdentity,
    NormalizationError,
    OptionContract,
    UnderlyingBar,
    normalize_option_contract,
    normalize_option_snapshot,
    normalize_quote,
    normalize_underlying_bar,
    parse_timestamp,
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

    def test_non_new_york_session_timezone_is_rejected(self):
        with self.assertRaisesRegex(NormalizationError, "America/New_York"):
            normalize_underlying_bar({
                "symbol": "SPY", "timestamp": "2024-03-11T13:30:00Z",
                "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1,
                "provider": "alpaca", "feed": "iex", "timezone": "UTC",
            })

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

    def test_option_snapshot_preserves_liquidity_metadata(self):
        snapshot = normalize_option_snapshot({
            "symbol": "SPY240119C00500000", "underlying": "SPY",
            "expiration": "2024-01-19", "strike": 500, "right": "C",
            "timestamp": "2024-01-02T14:30:00Z", "bid": 1.0, "ask": 1.1,
            "bid_size": 3, "ask_size": 4, "volume": 20,
            "open_interest": 100, "provider": "alpaca", "feed": "opra",
        })
        self.assertEqual((snapshot.bid_size, snapshot.ask_size,
                          snapshot.volume, snapshot.open_interest),
                         (3.0, 4.0, 20.0, 100.0))

    def test_option_snapshot_legacy_payload_defaults_and_aliases_are_safe(self):
        # Existing recorded fixtures omit the newer recorder measurements;
        # normalization remains compatible while preserving explicit aliases.
        base = {
            "symbol": "SPY240119C00500000", "underlying": "SPY",
            "expiration": "2024-01-19", "strike": 500, "right": "C",
            "timestamp": "2024-01-02T14:30:00Z", "bid": 1.0, "ask": 1.1,
            "provider": "alpaca", "feed": "opra",
        }
        legacy = normalize_option_snapshot(base)
        self.assertEqual((legacy.bid_size, legacy.ask_size,
                          legacy.volume, legacy.open_interest),
                         (None, None, None, None))
        aliased = normalize_option_snapshot({
            **base, "bs": 3, "as": 4, "day_volume": 20, "oi": 100,
        })
        self.assertEqual((aliased.bid_size, aliased.ask_size,
                          aliased.volume, aliased.open_interest),
                         (3.0, 4.0, 20.0, 100.0))

    def test_option_snapshot_rejects_negative_liquidity_metadata(self):
        payload = {
            "symbol": "SPY240119C00500000", "underlying": "SPY",
            "expiration": "2024-01-19", "strike": 500, "right": "C",
            "timestamp": "2024-01-02T14:30:00Z", "bid": 1.0, "ask": 1.1,
            "provider": "alpaca", "feed": "opra", "volume": -1,
        }
        with self.assertRaisesRegex(NormalizationError, "non-negative"):
            normalize_option_snapshot(payload)

    def test_crypto_and_unknown_asset_classes_are_rejected(self):
        base = {
            "symbol": "BTC/USD", "timestamp": "2024-01-02T14:30:00Z",
            "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1,
            "provider": "alpaca", "feed": "iex",
        }
        with self.assertRaisesRegex(NormalizationError, "slash pair"):
            normalize_underlying_bar(base)
        base["symbol"] = "SPY"
        base["asset_class"] = "crypto"
        with self.assertRaisesRegex(NormalizationError, "crypto"):
            normalize_underlying_bar(base)
        base["asset_class"] = "unknown"
        with self.assertRaisesRegex(NormalizationError, "unsupported asset class"):
            normalize_underlying_bar(base)

    def test_option_contract_requires_listed_occ_identity(self):
        payload = {
            "symbol": "SPY-CALL", "underlying": "SPY",
            "expiration": "2024-01-19", "strike": 500, "right": "C",
            "provider": "alpaca", "feed": "opra",
        }
        with self.assertRaisesRegex(NormalizationError, "OCC"):
            normalize_option_contract(payload)
        payload.update(symbol="BTC/USD", underlying="BTC/USD")
        with self.assertRaisesRegex(NormalizationError, "slash pair"):
            normalize_option_contract(payload)

    def test_timestamp_parser_wraps_boolean_and_nonfinite_inputs(self):
        for value in (True, False, float("nan"), float("inf"), Decimal("NaN")):
            with self.subTest(value=value):
                with self.assertRaises(NormalizationError):
                    parse_timestamp(value)

    def test_direct_bar_constructor_still_fails_closed(self):
        identity = EventIdentity(
            provider="alpaca", feed="iex",
            as_of=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
            observed_at=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
            session_date=datetime(2024, 1, 2).date())
        with self.assertRaisesRegex(NormalizationError, "timezone"):
            UnderlyingBar("SPY", datetime(2024, 1, 2, 14, 30),
                          1, 2, 1, 1.5, 10, identity)
        with self.assertRaisesRegex(NormalizationError, "finite"):
            UnderlyingBar("SPY", datetime(2024, 1, 2, 14, 30,
                                           tzinfo=timezone.utc),
                          1, 2, 1, float("nan"), 10, identity)

    def test_supplied_option_contract_does_not_bypass_payload_scope(self):
        contract = OptionContract(
            symbol="SPY240119C00500000", underlying="SPY",
            expiration=datetime(2024, 1, 19).date(), strike=500,
            right="call", multiplier=100, currency="USD",
            provider="alpaca", feed="opra")
        payload = {
            "timestamp": "2024-01-02T14:30:00Z", "bid": 1, "ask": 1.1,
            "asset_class": "crypto", "symbol": "BTC/USD",
            "provider": "alpaca", "feed": "opra",
        }
        with self.assertRaisesRegex(NormalizationError, "crypto"):
            normalize_option_snapshot(payload, contract=contract)

    def test_option_multiplier_errors_use_normalization_contract(self):
        with self.assertRaisesRegex(NormalizationError, "multiplier"):
            normalize_option_contract({
                "symbol": "SPY240119C00500000", "underlying": "SPY",
                "expiration": "2024-01-19", "strike": 500, "right": "C",
                "multiplier": "many", "provider": "alpaca", "feed": "opra",
            })

    def test_option_occ_identity_matches_expiration_and_strike(self):
        base = {
            "symbol": "SPY240119C00500000", "underlying": "SPY",
            "expiration": "2024-01-20", "strike": 500, "right": "C",
            "provider": "alpaca", "feed": "opra",
        }
        with self.assertRaisesRegex(NormalizationError, "expiration"):
            normalize_option_contract(base)
        base.update(expiration="2024-01-19", strike=501)
        with self.assertRaisesRegex(NormalizationError, "strike"):
            normalize_option_contract(base)
        with self.assertRaisesRegex(NormalizationError, "expiration"):
            OptionContract(
                symbol="SPY240119C00500000", underlying="SPY",
                expiration=datetime(2024, 1, 20).date(), strike=500,
                right="call", multiplier=100, currency="USD",
                provider="alpaca", feed="opra")
