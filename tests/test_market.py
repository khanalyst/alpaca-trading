import time
import unittest

from agent.market import market_snapshot, symbol_snapshot
from tests.helpers import valid_config


class FakeMarketClient:
    def __init__(self):
        self.frames = {}

    @staticmethod
    def parse_timeframe(timeframe):
        return {"15m": 900, "1h": 3600, "4h": 14400}[timeframe]

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        duration = self.parse_timeframe(timeframe) * 1000
        now = int(time.time() * 1000) // duration * duration
        base = 100 if symbol.startswith("BTC") else 50
        rows = []
        for index in range(limit):
            close = base + index * (0.4 if symbol.startswith("BTC") else 0.2)
            volume = 400 if index >= limit - 4 else 100
            rows.append([
                now - (limit - index + 2) * duration,
                close - 0.1, close + 0.5, close - 0.5, close, volume,
            ])
        return rows

    def fetch_ticker(self, symbol):
        price = 148 if symbol.startswith("BTC") else 74
        return {
            "last": price, "percentage": 4.2, "quoteVolume": 100_000_000,
            "bid": price - 0.05, "ask": price + 0.05,
            "high": price + 5, "low": price - 5,
        }

    @staticmethod
    def fetch_funding_rate(symbol):
        return {"fundingRate": 0.0002}


class FakeExchange:
    def __init__(self):
        self.x = FakeMarketClient()

    @staticmethod
    def retry(fn, *args, **kwargs):
        return fn(*args, **kwargs)


class MarketSnapshotTests(unittest.TestCase):
    def test_symbol_snapshot_has_relative_and_regime_context(self):
        cfg = valid_config()
        snap = symbol_snapshot(FakeExchange(), "ETH/USDT:USDT", cfg)
        for field in ("spread_pct", "relative_volume_1h", "atr_1h_ratio",
                      "regime"):
            self.assertIn(field, snap)
        self.assertGreater(snap["relative_volume_1h"], 1)

    def test_btc_context_exists_even_when_btc_is_not_tradable(self):
        cfg = valid_config()
        snap = market_snapshot(FakeExchange(), ["ETH/USDT:USDT"], cfg)
        self.assertIn("ETH/USDT:USDT", snap)
        self.assertNotIn("BTC/USDT:USDT", snap)
        context = snap["_market_context"]
        self.assertEqual(context["benchmark"], "BTC/USDT:USDT")
        self.assertIsNotNone(context["regime"])
        self.assertIsNotNone(context["atr_1h_ratio"])
        self.assertIsNotNone(snap["ETH/USDT:USDT"]["corr_btc_1h_30"])


if __name__ == "__main__":
    unittest.main()
