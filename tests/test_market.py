import time
import unittest

from agent.market import (build_universe, market_snapshot, quote_volume_usd,
                          symbol_snapshot)
from tests.helpers import valid_config


class FakeMarketClient:
    def __init__(self):
        self.frames = {}
        self.markets = {
            "BTC/USDT:USDT": {
                "swap": True, "settle": "USDT", "active": True,
                "linear": True, "contractSize": 0.01,
            },
            "ETH/USDT:USDT": {
                "swap": True, "settle": "USDT", "active": True,
                "linear": True, "contractSize": 0.1,
            },
        }

    def market(self, symbol):
        return self.markets[symbol]

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
        now = int(time.time() * 1000)
        return {
            "fundingRate": 0.0002, "fundingTimestamp": now,
            "nextFundingTimestamp": now + 4 * 3_600_000,
        }

    def fetch_tickers(self):
        return {symbol: self.fetch_ticker(symbol) for symbol in self.markets}


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
                      "regime", "funding_interval_hours",
                      "next_funding_minutes"):
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

    def test_all_up_history_reads_rsi_100_not_nan(self):
        # The fake client's closes rise monotonically: no down moves at all.
        # RSI must saturate at 100 instead of leaking NaN into the snapshot
        # JSON sent to the model.
        snap = symbol_snapshot(FakeExchange(), "BTC/USDT:USDT", valid_config())
        self.assertEqual(snap["rsi_1h"], 100.0)

    def test_okx_swap_contract_volume_is_converted_with_contract_size(self):
        market = {"swap": True, "settle": "USDT", "contractSize": 0.01}
        ticker = {
            "last": 100_000, "quoteVolume": None,
            "baseVolume": 120_000,
            "info": {"vol24h": "120000", "volCcy24h": "1200"},
        }
        self.assertEqual(quote_volume_usd(ticker, market), 120_000_000)

    def test_universe_uses_normalized_swap_quote_volume(self):
        cfg = valid_config()
        cfg["universe"]["min_24h_quote_volume_usd"] = 100_000_000
        client = FakeMarketClient()
        client.fetch_ticker = lambda symbol: {
            "last": 100_000, "quoteVolume": None, "baseVolume": 120_000,
            "info": {"volCcy24h": "1200"},
        }
        ex = FakeExchange()
        ex.x = client
        self.assertEqual(
            build_universe(ex, cfg),
            ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        )


if __name__ == "__main__":
    unittest.main()
