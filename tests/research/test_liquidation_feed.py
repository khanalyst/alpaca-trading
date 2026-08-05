"""The filled-liquidation feed behind flush-fade's forced-flow mechanism.

flush-fade claims a liquidation cascade, and v1 tested that claim through a
four-hour open-interest proxy: OI falling is *consistent with* forced
deleveraging but cannot tell it apart from a voluntary close. OKX publishes
the events themselves, so the claim can be measured directly.

ccxt does not map ``/api/v5/public/liquidation-orders`` - it has no
liquidation path for OKX at all - so agent/exchange.py binds the endpoint
onto its own client. That registration and this parser are exactly the kind
of code whose failure is invisible: the fields degrade to None by design, so
a typo would leave the feed permanently empty with nothing complaining. These
tests hold both ends of it, with every network call mocked.
"""

import os
import time
import unittest
from unittest.mock import Mock, patch

import ccxt

from agent import market
from agent.exchange import Exchange
from tests.helpers import valid_config


INST_ID = "BTC-USDT-SWAP"
SYMBOL = "BTC/USDT:USDT"
CONTRACT_SIZE = 0.01


def detail(ts_ms, size, price, pos_side="short", side="buy"):
    """One row shaped exactly like OKX's ``data[].details[]`` entries."""
    return {"bkLoss": "0", "bkPx": str(price), "ccy": "",
            "posSide": pos_side, "side": side, "sz": str(size),
            "time": ts_ms, "ts": str(ts_ms)}


def payload(details, inst_id=INST_ID):
    return [{"instFamily": "BTC-USDT", "instId": inst_id, "instType": "SWAP",
             "totalLoss": "0", "uly": "BTC-USDT", "details": details}]


class FakeExchange:
    """Mirrors tests/test_market_series.py: a recorder around public_call."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.x = Mock()
        self.x.market.return_value = {
            "info": {"instId": INST_ID}, "contractSize": CONTRACT_SIZE}

    def public_call(self, method, params):
        self.calls.append((method, params))
        return self.rows


class EndpointRegistrationTests(unittest.TestCase):
    """The endpoint must reach OKX through ccxt, never a hand-built URL."""

    def test_stock_ccxt_does_not_expose_the_endpoint(self):
        # The reason agent/exchange.py registers it at all. If a later ccxt
        # ships the path, this test fails and the registration can go.
        self.assertFalse(
            callable(getattr(ccxt.okx(), "publicGetPublicLiquidationOrders",
                             None)))

    def test_the_client_signs_the_documented_okx_url(self):
        client = ccxt.okx()
        client.load_markets = lambda *a, **kw: {}
        client.fetch_time = lambda *a, **kw: time.time() * 1000
        requested = {}

        def fake_fetch(url, method="GET", headers=None, body=None):
            requested["url"] = url
            return {"code": "0", "data": payload([detail(1, 1, 1)])}

        client.fetch = fake_fetch
        credentials = {"OKX_API_KEY": "k", "OKX_API_SECRET": "s",
                       "OKX_API_PASSPHRASE": "p"}
        with patch.dict(os.environ, credentials), \
                patch("agent.exchange.ccxt.okx", return_value=client):
            exchange = Exchange(valid_config(), validate_account=False)

        rows = exchange.public_call(
            "publicGetPublicLiquidationOrders",
            {"instType": "SWAP", "uly": "BTC-USDT", "state": "filled"})

        self.assertEqual(len(rows), 1)
        # The hostname template is expanded because ccxt expanded it.
        self.assertEqual(
            requested["url"],
            "https://www.okx.com/api/v5/public/liquidation-orders"
            "?instType=SWAP&uly=BTC-USDT&state=filled")

    def test_market_asks_for_the_endpoint_the_client_registers(self):
        exchange = FakeExchange(payload([]))
        with self.assertLogs("market", level="WARNING"):
            market._okx_liquidations(exchange, SYMBOL)

        method, params = exchange.calls[0]
        self.assertEqual(method, "publicGetPublicLiquidationOrders")
        # OKX rejects instId on its own here, so the underlying is sent.
        self.assertEqual(params["uly"], "BTC-USDT")
        self.assertEqual(params["state"], "filled")


class LiquidationFlowTests(unittest.TestCase):
    def setUp(self):
        market._series_cache.clear()

    def test_a_real_shaped_nested_payload_becomes_notional_and_count(self):
        now_ms = market.time.time() * 1000
        rows = payload([
            detail(now_ms - 60_000, 11.49, 64_198.6),
            detail(now_ms - 600_000, 0.31, 64_138.4, "long", "sell"),
            # Older than the window: still a real liquidation, just not this
            # hour's forced flow.
            detail(now_ms - 7_200_000, 500, 64_000.0),
        ])
        out = market._liquidation_flow(FakeExchange(rows), SYMBOL)

        expected = (11.49 * CONTRACT_SIZE * 64_198.6
                    + 0.31 * CONTRACT_SIZE * 64_138.4)
        self.assertEqual(out["liq_count_1h"], 2)
        self.assertAlmostEqual(out["liq_notional_1h_usd"], expected, places=2)

    def test_other_instruments_under_the_same_underlying_are_excluded(self):
        # The query is by underlying because OKX requires it; the measurement
        # is per instrument.
        now_ms = market.time.time() * 1000
        rows = (payload([detail(now_ms, 10, 64_000.0)])
                + payload([detail(now_ms, 999, 64_000.0)],
                          inst_id="BTC-USDT-250926"))
        out = market._liquidation_flow(FakeExchange(rows), SYMBOL)

        self.assertEqual(out["liq_count_1h"], 1)
        self.assertAlmostEqual(
            out["liq_notional_1h_usd"], 10 * CONTRACT_SIZE * 64_000.0,
            places=2)

    def test_a_quiet_hour_is_zero_flow_and_not_a_missing_reading(self):
        rows = payload([detail(market.time.time() * 1000 - 7_200_000,
                               10, 64_000.0)])
        out = market._liquidation_flow(FakeExchange(rows), SYMBOL)

        self.assertEqual(out, {"liq_notional_1h_usd": 0.0, "liq_count_1h": 0})

    def test_a_failed_call_degrades_to_none_and_says_so_in_the_logs(self):
        # public_call returns [] on any failure, so an outage would otherwise
        # look exactly like a market in which nobody was liquidated.
        exchange = FakeExchange([])
        with self.assertLogs("market", level="WARNING") as logged:
            out = market._liquidation_flow(exchange, SYMBOL)

        self.assertEqual(
            out, {"liq_notional_1h_usd": None, "liq_count_1h": None})
        self.assertIn(INST_ID, "".join(logged.output))

    def test_a_malformed_payload_degrades_to_none(self):
        for rows in (
                [{"instId": INST_ID, "details": [{"sz": "x", "bkPx": "1",
                                                  "ts": "1"}]}],
                [{"instId": INST_ID, "details": [{"posSide": "short"}]}],
                [{"instId": INST_ID, "details": ["not-a-mapping"]}],
                [{"instId": INST_ID, "details": None}],
                ["not-a-mapping"],
                None,
        ):
            with self.subTest(rows=rows):
                market._series_cache.clear()
                with self.assertLogs("market", level="WARNING"):
                    out = market._liquidation_flow(FakeExchange(rows), SYMBOL)
                self.assertEqual(
                    out, {"liq_notional_1h_usd": None, "liq_count_1h": None})

    def test_an_unreadable_market_degrades_to_none(self):
        exchange = FakeExchange(payload([]))
        exchange.x.market.side_effect = ccxt.BadSymbol("unknown")

        self.assertEqual(
            market._liquidation_flow(exchange, SYMBOL),
            {"liq_notional_1h_usd": None, "liq_count_1h": None})


if __name__ == "__main__":
    unittest.main()
