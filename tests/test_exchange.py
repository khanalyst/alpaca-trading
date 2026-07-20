import unittest

import ccxt

from agent.exchange import Exchange
from tests.helpers import valid_config


class FillClient:
    def __init__(self):
        self.create_calls = 0

    @staticmethod
    def market(symbol):
        return {"contractSize": 1}

    @staticmethod
    def fetch_order(order_id, symbol, params=None):
        if order_id is None and not (params or {}).get("trigger"):
            raise ccxt.OrderNotFound("not a regular order")
        if order_id is None:
            return {"id": "recovered", "status": "closed", "filled": 1,
                    "average": 100}
        return {
            "id": order_id,
            "status": "closed",
            "filled": 3,
            "average": 101,
            "fee": {"currency": "USDT", "cost": -0.5},
            "info": {},
        }

    def create_order(self, *args, **kwargs):
        self.create_calls += 1
        raise ccxt.RequestTimeout("ambiguous")


class FillVerificationTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Exchange.__new__(Exchange)
        self.exchange.cfg = valid_config()
        self.exchange.alerts = None
        self.exchange.x = FillClient()

    def test_actual_partial_fill_fee_and_slippage_are_recorded(self):
        result = self.exchange.verify_fill(
            {"id": "order-1", "status": "open"},
            "BTC/USDT:USDT", requested=5, expected_price=100)
        self.assertTrue(result["partial"])
        self.assertEqual(result["filled"], 3)
        self.assertEqual(result["average"], 101)
        self.assertEqual(result["fee_usd"], 0.5)
        self.assertEqual(result["slippage_usd"], 3)

    def test_ambiguous_create_is_recovered_without_resubmission(self):
        result = self.exchange._create_order_once(
            "BTC/USDT:USDT", "market", "buy", 1, None, {}, "test")
        self.assertEqual(result["id"], "recovered")
        self.assertEqual(self.exchange.x.create_calls, 1)

    def test_fee_is_not_double_counted_when_ccxt_exposes_both_shapes(self):
        fee = {"currency": "USDT", "cost": 0.5}
        self.assertEqual(
            self.exchange._fee_usd({"fee": fee, "fees": [fee]}), 0.5)


class ProtectionClient:
    def __init__(self):
        self.queries = []

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        self.queries.append(params)
        if (params or {}).get("ordType") == "oco":
            return [{
                "id": "oco-1", "amount": 2,
                "info": {"slTriggerPx": "95", "tpTriggerPx": "110"},
            }]
        return []


class ProtectionDiscoveryTests(unittest.TestCase):
    def test_all_okx_protective_algo_types_are_queried(self):
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = ProtectionClient()
        orders = exchange.protective_orders("BTC/USDT:USDT")
        self.assertEqual([order["id"] for order in orders], ["oco-1"])
        self.assertEqual(
            exchange.x.queries,
            [None, {"ordType": "conditional"}, {"ordType": "oco"},
             {"ordType": "trigger"}],
        )


if __name__ == "__main__":
    unittest.main()
