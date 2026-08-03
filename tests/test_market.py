import json
import time
import unittest

from agent.market import (_basis_pct, build_universe, market_snapshot,
                          quote_volume_usd, select_universe, symbol_snapshot)
from tests.helpers import valid_config


class FakeMarketClient:
    def __init__(self):
        self.frames = {}
        self.mark_price_calls = []
        self.markets = {
            "BTC/USDT:USDT": {
                "id": "BTC-USDT-SWAP",
                "swap": True, "settle": "USDT", "active": True,
                "linear": True, "contractSize": 0.01,
            },
            "ETH/USDT:USDT": {
                "id": "ETH-USDT-SWAP",
                "swap": True, "settle": "USDT", "active": True,
                "linear": True, "contractSize": 0.1,
            },
        }

    def market(self, symbol):
        return self.markets[symbol]

    @staticmethod
    def parse_timeframe(timeframe):
        return {"1m": 60, "15m": 900, "1h": 3600, "4h": 14400}[timeframe]

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
            "timestamp": int(time.time() * 1000),
        }

    @staticmethod
    def fetch_funding_rate(symbol):
        now = int(time.time() * 1000)
        return {
            "fundingRate": 0.0002, "fundingTimestamp": now,
            "nextFundingTimestamp": now + 4 * 3_600_000,
        }

    def fetch_mark_prices(self, symbols=None):
        symbols = list(symbols or self.markets)
        self.mark_price_calls.append(symbols)
        now = int(time.time() * 1000)
        prices = {
            "BTC/USDT:USDT": 100.2,
            "ETH/USDT:USDT": 50.1,
        }
        return {
            symbol: {
                "symbol": symbol,
                "timestamp": now,
                "markPrice": prices[symbol],
                "info": {"markPx": str(prices[symbol]), "ts": str(now)},
            }
            for symbol in symbols
        }

    @staticmethod
    def fetch_funding_rate_history(symbol, since, limit):
        now = int(time.time() * 1000)
        return [
            {"fundingRate": 0.00005 + index * 0.000001,
             "timestamp": now - (limit - index) * 8 * 3_600_000}
            for index in range(limit)
        ]

    @staticmethod
    def fetch_open_interest(symbol):
        return {"openInterestValue": 250_000_000}

    def fetch_tickers(self, symbols=None):
        return {
            symbol: self.fetch_ticker(symbol)
            for symbol in (symbols or self.markets)
        }


class FakeExchange:
    def __init__(self):
        self.x = FakeMarketClient()
        self.public_calls = []

    @staticmethod
    def retry(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def account_swap_instruments(self, refresh=False):
        return {
            symbol: {
                "instId": market["id"],
                "instType": "SWAP",
                "settleCcy": "USDT",
                "state": "live",
                "instCategory": str(
                    (market.get("info") or {}).get("instCategory", "1")),
            }
            for symbol, market in self.x.markets.items()
            if (market.get("info") or {}).get("accountAvailable", True)
        }

    @staticmethod
    def taker_fee_pct(symbol):
        return 0.07

    def public_call(self, method, params):
        self.public_calls.append((method, params))
        if method != "publicGetMarketIndexTickers":
            return []
        now = str(int(time.time() * 1000))
        return [
            {"instId": "BTC-USDT", "idxPx": "100.0", "ts": now},
            {"instId": "ETH-USDT", "idxPx": "50.0", "ts": now},
        ]


class MarketSnapshotTests(unittest.TestCase):
    def test_basis_calculation_rejects_overflow(self):
        self.assertIsNone(_basis_pct(10**400, 1))

    def test_symbol_snapshot_has_relative_and_regime_context(self):
        cfg = valid_config()
        snap = symbol_snapshot(FakeExchange(), "ETH/USDT:USDT", cfg)
        for field in ("spread_pct", "relative_volume_1h", "atr_1h_ratio",
                      "regime", "funding_interval_hours",
                      "next_funding_minutes", "funding_percentile_30",
                      "perp_index_basis_pct", "open_interest_musd",
                      "taker_fee_pct_per_side", "signal_ts",
                      "setup_evidence"):
            self.assertIn(field, snap)
        self.assertGreater(snap["relative_volume_1h"], 1)
        self.assertEqual(snap["fee_rate_source"], "okx_account")

    def test_missing_funding_is_unavailable_instead_of_fabricated_zero(self):
        exchange = FakeExchange()
        exchange.x.fetch_funding_rate = lambda symbol: {}

        snap = symbol_snapshot(
            exchange, "ETH/USDT:USDT", valid_config())

        self.assertIsNone(snap["funding_rate_pct"])
        self.assertEqual(snap["funding_samples_30"], 0)

    def test_symbol_snapshot_preserves_valid_adapter_basis_prices(self):
        exchange = FakeExchange()
        original = exchange.x.fetch_funding_rate

        def funding_with_prices(symbol):
            return {
                **original(symbol),
                "markPrice": "100.2",
                "indexPrice": "100.0",
            }

        exchange.x.fetch_funding_rate = funding_with_prices

        snap = symbol_snapshot(
            exchange, "ETH/USDT:USDT", valid_config())

        self.assertEqual(snap["perp_index_basis_pct"], 0.2)

    def test_market_snapshot_uses_one_batched_public_basis_read(self):
        exchange = FakeExchange()
        snap = market_snapshot(
            exchange,
            ["BTC/USDT:USDT", "ETH/USDT:USDT"],
            valid_config(),
        )

        self.assertEqual(snap["BTC/USDT:USDT"]["perp_index_basis_pct"], 0.2)
        self.assertEqual(snap["ETH/USDT:USDT"]["perp_index_basis_pct"], 0.2)
        self.assertEqual(
            exchange.x.mark_price_calls,
            [["BTC/USDT:USDT", "ETH/USDT:USDT"]],
        )
        self.assertEqual(
            [call for call in exchange.public_calls
             if call[0] == "publicGetMarketIndexTickers"],
            [("publicGetMarketIndexTickers", {"quoteCcy": "USDT"})],
        )

    def test_market_snapshot_rejects_stale_index_basis(self):
        exchange = FakeExchange()
        stale = str(int((time.time() - 120) * 1000))

        def stale_index(method, params):
            if method == "publicGetMarketIndexTickers":
                return [{"instId": "ETH-USDT", "idxPx": "50", "ts": stale}]
            return []

        exchange.public_call = stale_index

        snap = market_snapshot(
            exchange, ["ETH/USDT:USDT"], valid_config())

        self.assertIsNone(snap["ETH/USDT:USDT"]["perp_index_basis_pct"])

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
        self.assertIsNotNone(
            snap["ETH/USDT:USDT"]["corr_btc_1h_72_shrunk"])
        self.assertGreaterEqual(
            snap["ETH/USDT:USDT"]["corr_btc_samples"], 24)

    def test_market_context_reports_setup_breadth(self):
        # How many instruments qualify at once is the difference between an
        # idiosyncratic setup and one leg of a correlated market-wide move,
        # and the model cannot see it from a per-symbol snapshot.
        cfg = valid_config()
        snap = market_snapshot(
            FakeExchange(), ["BTC/USDT:USDT", "ETH/USDT:USDT"], cfg)
        context = snap["_market_context"]
        scanned = context["instruments_scanned"]
        firing = context["instruments_with_a_valid_setup"]
        self.assertEqual(scanned, 2)
        self.assertGreaterEqual(firing, 0)
        self.assertLessEqual(firing, scanned)
        self.assertAlmostEqual(
            context["setup_breadth_pct"], firing / scanned * 100, places=1)

    def test_setup_breadth_counts_each_instrument_once(self):
        # An instrument satisfying two contracts at the same time is still one
        # instrument; double-counting would overstate breadth.
        cfg = valid_config()
        snap = market_snapshot(FakeExchange(), ["BTC/USDT:USDT"], cfg)
        context = snap["_market_context"]
        self.assertLessEqual(
            context["instruments_with_a_valid_setup"],
            context["instruments_scanned"])

    def test_all_up_history_reads_rsi_100_not_nan(self):
        # The fake client's closes rise monotonically: no down moves at all.
        # RSI must saturate at 100 instead of leaking NaN into the snapshot
        # JSON sent to the model.
        snap = symbol_snapshot(FakeExchange(), "BTC/USDT:USDT", valid_config())
        self.assertEqual(snap["rsi_1h"], 100.0)

    def test_non_finite_market_values_become_json_null(self):
        exchange = FakeExchange()
        original_ticker = exchange.x.fetch_ticker

        def malformed_ticker(symbol):
            return {**original_ticker(symbol), "percentage": float("nan")}

        exchange.x.fetch_ticker = malformed_ticker
        snap = symbol_snapshot(
            exchange, "BTC/USDT:USDT", valid_config())

        self.assertIsNone(snap["chg_24h_pct"])
        encoded = json.dumps(snap, allow_nan=False)
        self.assertNotIn("NaN", encoded)
        self.assertNotIn("Infinity", encoded)

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

    def test_non_crypto_and_account_unavailable_swaps_are_excluded(self):
        cfg = valid_config()
        cfg["universe"]["top_n"] = 2
        cfg["universe"]["min_24h_quote_volume_usd"] = 1
        exchange = FakeExchange()
        exchange.x.markets = {
            "CL/USDT:USDT": {
                "id": "CL-USDT-SWAP", "swap": True, "settle": "USDT",
                "active": True, "linear": True, "contractSize": 1,
                "info": {"instCategory": "4"},
            },
            "XAU/USDT:USDT": {
                "id": "XAU-USDT-SWAP", "swap": True, "settle": "USDT",
                "active": True, "linear": True, "contractSize": 1,
                "info": {"instCategory": "4"},
            },
            "BTC/USDT:USDT": {
                "id": "BTC-USDT-SWAP", "swap": True, "settle": "USDT",
                "active": True, "linear": True, "contractSize": 0.01,
                "info": {"instCategory": "1"},
            },
            "ETH/USDT:USDT": {
                "id": "ETH-USDT-SWAP", "swap": True, "settle": "USDT",
                "active": True, "linear": True, "contractSize": 0.1,
                "info": {"instCategory": "1", "accountAvailable": False},
            },
        }
        volumes = {
            "CL/USDT:USDT": 400_000_000,
            "XAU/USDT:USDT": 300_000_000,
            "ETH/USDT:USDT": 200_000_000,
            "BTC/USDT:USDT": 100_000_000,
        }
        exchange.x.fetch_ticker = lambda symbol: {
            "last": 100, "quoteVolume": volumes[symbol],
            "bid": 99.9, "ask": 100.1, "high": 105, "low": 95,
        }

        selected, audit = select_universe(exchange, cfg)

        self.assertEqual(selected, ["BTC/USDT:USDT"])
        reasons = {row["symbol"]: row["reason"]
                   for row in audit["candidates"]}
        self.assertEqual(reasons["CL/USDT:USDT"],
                         "non_crypto_category_4")
        self.assertEqual(reasons["XAU/USDT:USDT"],
                         "non_crypto_category_4")
        self.assertEqual(reasons["ETH/USDT:USDT"],
                         "not_available_to_account")

    def test_insufficient_history_does_not_consume_a_top_n_slot(self):
        cfg = valid_config()
        cfg["universe"]["top_n"] = 1
        cfg["universe"]["min_24h_quote_volume_usd"] = 1
        exchange = FakeExchange()
        exchange.x.markets["ORCL/USDT:USDT"] = {
            "id": "ORCL-USDT-SWAP", "swap": True, "settle": "USDT",
            "active": True, "linear": True, "contractSize": 1,
            "info": {"instCategory": "3"},
        }
        # Model the actual failure mode while keeping ORCL marked crypto here:
        # history, rather than category, must be what excludes this fixture.
        exchange.x.markets["ORCL/USDT:USDT"]["info"]["instCategory"] = "1"
        volumes = {
            "ORCL/USDT:USDT": 300_000_000,
            "BTC/USDT:USDT": 200_000_000,
            "ETH/USDT:USDT": 100_000_000,
        }
        exchange.x.fetch_ticker = lambda symbol: {
            "last": 100, "quoteVolume": volumes[symbol],
            "bid": 99.9, "ask": 100.1, "high": 105, "low": 95,
        }
        original = exchange.x.fetch_ohlcv
        calls = []

        def history(symbol, timeframe, since, limit):
            calls.append((symbol, timeframe))
            rows = original(symbol, timeframe, since, limit)
            return rows[-40:] if (
                symbol == "ORCL/USDT:USDT" and timeframe == "4h") else rows

        exchange.x.fetch_ohlcv = history

        selected, audit = select_universe(exchange, cfg)

        self.assertEqual(selected, ["BTC/USDT:USDT"])
        row = next(item for item in audit["candidates"]
                   if item["symbol"] == "ORCL/USDT:USDT")
        self.assertEqual(row["reason"], "insufficient_4h_history")
        self.assertEqual(calls.count(("ORCL/USDT:USDT", "4h")), 1)

    def test_missing_instrument_category_fails_closed(self):
        cfg = valid_config()
        cfg["universe"]["min_24h_quote_volume_usd"] = 1
        exchange = FakeExchange()
        rows = exchange.account_swap_instruments()
        rows["BTC/USDT:USDT"].pop("instCategory")
        exchange.account_swap_instruments = lambda refresh=False: rows

        selected, audit = select_universe(exchange, cfg)

        self.assertNotIn("BTC/USDT:USDT", selected)
        row = next(item for item in audit["candidates"]
                   if item["symbol"] == "BTC/USDT:USDT")
        self.assertEqual(row["reason"], "non_crypto_category_unknown")


if __name__ == "__main__":
    unittest.main()
