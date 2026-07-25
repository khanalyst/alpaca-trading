import unittest
import time
from unittest.mock import Mock, patch

import ccxt

from agent.exchange import (CredentialError, EntryLiquidityRejected,
                            EntryOrderRejected, Exchange)
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
            "BTC/USDT:USDT", requested=5, expected_price=100, side="buy")
        self.assertTrue(result["partial"])
        self.assertEqual(result["filled"], 3)
        self.assertEqual(result["average"], 101)
        self.assertEqual(result["fee_usd"], 0.5)
        self.assertEqual(result["slippage_usd"], 3)
        self.assertEqual(result["adverse_slippage_usd"], 3)

    def test_favorable_fill_is_negative_shortfall_not_a_cost(self):
        result = self.exchange.verify_fill(
            {"id": "order-1", "status": "open"},
            "BTC/USDT:USDT", requested=5, expected_price=102, side="buy")
        self.assertEqual(result["slippage_usd"], -3)
        self.assertEqual(result["adverse_slippage_usd"], 0)

    def test_ambiguous_create_is_recovered_without_resubmission(self):
        result = self.exchange._create_order_once(
            "BTC/USDT:USDT", "market", "buy", 1, None, {}, "test")
        self.assertEqual(result["id"], "recovered")
        self.assertEqual(self.exchange.x.create_calls, 1)

    def test_fee_is_not_double_counted_when_ccxt_exposes_both_shapes(self):
        fee = {"currency": "USDT", "cost": 0.5}
        self.assertEqual(
            self.exchange._fee_usd({"fee": fee, "fees": [fee]}), 0.5)

    def test_unverified_fill_error_keeps_client_submission_audit(self):
        with patch.object(
                self.exchange.x, "fetch_order",
                return_value={
                    "id": "order-2", "status": "canceled", "filled": 0,
                    "average": None, "info": {},
                }):
            with self.assertRaisesRegex(
                    RuntimeError, "no verified fill") as raised:
                self.exchange.verify_fill(
                    {
                        "id": "order-2",
                        "status": "open",
                        "_submission_audit": {
                            "client_order_id": "client-2",
                            "submission_count": 1,
                        },
                    },
                    "BTC/USDT:USDT",
                    requested=1,
                    expected_price=100,
                    side="buy",
                )

        self.assertEqual(
            raised.exception._order_audit["client_order_id"], "client-2")
        self.assertEqual(
            raised.exception._order_audit["outcome"], "fill_unverified")


class ProtectionClient:
    def __init__(self):
        self.queries = []

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        self.queries.append(params)
        if (params or {}).get("ordType") == "oco":
            return [{
                "id": "oco-1", "amount": 2, "side": "sell",
                "reduceOnly": True,
                "info": {"slTriggerPx": "95", "tpTriggerPx": "110",
                         "posSide": "net"},
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


class StaticOrdersClient:
    def __init__(self, orders):
        self.orders = orders

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        return self.orders if params is None else []


class ProtectionValidationTests(unittest.TestCase):
    @staticmethod
    def _exchange(orders):
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = StaticOrdersClient(orders)
        return exchange

    def test_wrong_side_and_non_reduce_only_triggers_do_not_count(self):
        orders = [
            {"id": "wrong-side", "side": "buy", "reduceOnly": True,
             "amount": 2, "triggerPrice": 95, "info": {"posSide": "net"}},
            {"id": "opens-risk", "side": "sell", "reduceOnly": False,
             "amount": 2, "triggerPrice": 95, "info": {"posSide": "net"}},
        ]
        status = self._exchange(orders).protection_status(
            "BTC/USDT:USDT", 2, "long", 100)
        self.assertFalse(status["stop_loss"])

    def test_matching_reduce_only_trigger_counts_as_stop(self):
        order = {
            "id": "valid-stop", "side": "sell", "reduceOnly": True,
            "amount": 2, "triggerPrice": 95, "info": {"posSide": "net"},
        }
        status = self._exchange([order]).protection_status(
            "BTC/USDT:USDT", 2, "long", 100)
        self.assertTrue(status["stop_loss"])


class DepthClient:
    @staticmethod
    def fetch_order_book(symbol, limit):
        return {
            "timestamp": int(time.time() * 1000),
            "bids": [[99.95, 1], [99.8, 2]],
            "asks": [[100.05, 1], [100.2, 2]],
        }

    @staticmethod
    def price_to_precision(symbol, price):
        return f"{price:.2f}"

    @staticmethod
    def market(symbol):
        return {"contractSize": 0.1}


class EntryGuardTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Exchange.__new__(Exchange)
        self.exchange.cfg = valid_config()
        self.exchange.alerts = None
        self.exchange.x = DepthClient()

    def test_sufficient_depth_returns_a_hard_ioc_limit(self):
        result = self.exchange.guarded_entry_limit(
            "BTC/USDT:USDT", "buy", 2, 0.15, 0.35, 10)
        self.assertEqual(result["limit_price"], 100.35)
        self.assertLess(result["estimated_slippage_pct"], 0.35)

    def test_insufficient_depth_inside_cap_rejects_entry(self):
        with self.assertRaisesRegex(
                EntryLiquidityRejected, "only .* contracts") as raised:
            self.exchange.guarded_entry_limit(
                "BTC/USDT:USDT", "buy", 10, 0.15, 0.35, 10)
        details = raised.exception.details
        self.assertEqual(details["requested_contracts"], 10)
        self.assertEqual(details["available_contracts"], 3)
        self.assertAlmostEqual(details["requested_notional_usdt"], 100)
        self.assertAlmostEqual(details["available_notional_usdt"], 30.045)


class ProtectedEntryTests(unittest.TestCase):
    def test_set_leverage_auth_failure_remains_a_credential_failure(self):
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = Mock()
        exchange.x.set_leverage.side_effect = ccxt.AuthenticationError(
            "bad key")

        with self.assertRaises(CredentialError):
            exchange.open_position(
                "BTC/USDT:USDT", "buy", 2, 3, 95, 110,
                expected_price=100, entry_limit_price=100.3,
            )

    def test_rejected_attached_protection_never_retries_a_naked_entry(self):
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = Mock()
        exchange.x.price_to_precision.side_effect = lambda symbol, value: str(value)
        exchange._create_order_once = Mock(
            side_effect=ccxt.ExchangeError("attached orders unsupported"))

        with self.assertRaisesRegex(
                EntryOrderRejected, "no unprotected fallback") as raised:
            exchange.open_position(
                "BTC/USDT:USDT", "buy", 2, 3, 95, 110,
                expected_price=100, entry_limit_price=100.3,
            )

        self.assertEqual(exchange._create_order_once.call_count, 1)
        self.assertEqual(raised.exception.details["classification"],
                         "permanent")
        self.assertIn("attached orders unsupported",
                      raised.exception.details["error_message"])
        params = exchange._create_order_once.call_args.args[5]
        self.assertIn("stopLoss", params)
        self.assertIn("takeProfit", params)

    def test_okx_code_and_message_are_preserved_without_raw_response(self):
        error = ccxt.ExchangeError(
            'okx {"code":"1","msg":"","data":[{"sCode":"51001",'
            '"subCode":"51001A","sMsg":"Instrument does not exist",'
            '"clOrdId":"client-a","inTime":"1","outTime":"2"}]}')

        rejection = Exchange._entry_order_rejection(
            "CL/USDT:USDT", "attached_entry", error)

        self.assertEqual(rejection.details["error_code"], "51001")
        self.assertEqual(rejection.details["error_message"],
                         "Instrument does not exist")
        self.assertEqual(rejection.details["classification"], "permanent")
        self.assertEqual(
            rejection.details["result_rows"][0]["sub_code"], "51001A")
        self.assertEqual(
            rejection.details["result_rows"][0]["client_order_id"], "client-a")
        self.assertIn("51001", str(rejection))


class MarginRiskTests(unittest.TestCase):
    def test_margin_usage_uses_okx_adjusted_equity_and_imr(self):
        client = Mock()
        client.fetch_balance.return_value = {
            "info": {"data": [{"adjEq": "10000", "imr": "1250",
                                "mmr": "250", "mgnRatio": "40", "details": [
                                    {"ccy": "USDT", "eq": "10000"},
                                ]}]},
            "USDT": {"used": 9000, "total": 10000},
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client
        self.assertEqual(exchange.margin_usage_pct(), 12.5)
        metrics = exchange.account_risk_metrics()
        self.assertEqual(metrics["maintenance_margin_ratio"], 40)

    def test_margin_usage_does_not_use_non_usdt_equity(self):
        client = Mock()
        client.fetch_balance.return_value = {
            "info": {"data": [{"adjEq": "18000", "imr": "1000",
                                "mmr": "200", "mgnRatio": "90",
                                "details": [
                                    {"ccy": "USDT", "eq": "10000"},
                                    {"ccy": "OKB", "eq": "100",
                                     "eqUsd": "8000",
                                     "collateralEnabled": True},
                                ]}]},
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client

        self.assertEqual(exchange.margin_usage_pct(), 10)

    def test_single_currency_mode_uses_usdt_detail_imr_and_mmr(self):
        client = Mock()
        client.fetch_balance.return_value = {
            "info": {"data": [{
                "totalEq": "10000",
                "imr": "",
                "mmr": "",
                "mgnRatio": "",
                "details": [{
                    "ccy": "USDT",
                    "eq": "10000",
                    "imr": "1000",
                    "mmr": "250",
                    "mgnRatio": "40",
                }],
            }]},
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client

        metrics = exchange.account_risk_metrics()

        self.assertEqual(metrics["initial_margin_usage_pct"], 10)
        self.assertEqual(metrics["maintenance_margin_ratio"], 40)
        self.assertEqual(metrics["risk_scope"], "usdt_currency")

    def test_non_finite_margin_measurement_fails_closed(self):
        client = Mock()
        client.fetch_balance.return_value = {
            "info": {"data": [{"adjEq": "10000", "imr": "nan",
                                "details": [
                                    {"ccy": "USDT", "eq": "10000"},
                                ]}]},
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client
        with self.assertRaisesRegex(RuntimeError, "not a finite value"):
            exchange.margin_usage_pct()

    def test_missing_mmr_with_open_margin_fails_closed(self):
        client = Mock()
        client.fetch_balance.return_value = {
            "info": {"data": [{"adjEq": "10000", "imr": "1000",
                                "details": [
                                    {"ccy": "USDT", "eq": "10000"},
                                ]}]},
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client

        with self.assertRaisesRegex(
                RuntimeError, "no maintenance-margin measurement"):
            exchange.account_risk_metrics()


class AccountInstrumentTests(unittest.TestCase):
    def test_private_account_instruments_are_mapped_to_ccxt_symbols(self):
        client = Mock()
        client.markets = {
            "BTC/USDT:USDT": {
                "id": "BTC-USDT-SWAP", "swap": True,
            },
            "CL/USDT:USDT": {
                "id": "CL-USDT-SWAP", "swap": True,
            },
        }
        client.private_get_account_instruments.return_value = {
            "code": "0",
            "data": [
                {"instId": "BTC-USDT-SWAP", "instType": "SWAP",
                 "settleCcy": "USDT", "state": "live",
                 "instCategory": "1"},
                {"instId": "CL-USDT-SWAP", "instType": "SWAP",
                 "settleCcy": "USDT", "state": "live",
                 "instCategory": "4"},
            ],
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client

        rows = exchange.account_swap_instruments(refresh=True)

        self.assertEqual(rows["BTC/USDT:USDT"]["instCategory"], "1")
        self.assertEqual(rows["CL/USDT:USDT"]["instCategory"], "4")
        client.private_get_account_instruments.assert_called_once_with(
            {"instType": "SWAP"})

    def test_account_taker_fee_is_read_and_cached(self):
        client = Mock()
        client.market.return_value = {"id": "BTC-USDT-SWAP"}
        client.private_get_account_trade_fee.return_value = {
            "code": "0",
            "data": [{"taker": "-0.0007"}],
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client

        first = exchange.taker_fee_pct("BTC/USDT:USDT")
        second = exchange.taker_fee_pct("BTC/USDT:USDT")

        self.assertAlmostEqual(first, 0.07)
        self.assertEqual(second, first)
        client.private_get_account_trade_fee.assert_called_once_with({
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
        })


class AccountValueValidationTests(unittest.TestCase):
    def test_equity_uses_only_usdt_currency_row(self):
        client = Mock()
        client.fetch_balance.return_value = {
            "info": {"data": [{
                "totalEq": "80384.99",
                "details": [
                    {"ccy": "USDT", "eq": "72232.06",
                     "eqUsd": "72232.06"},
                    {"ccy": "OKB", "eq": "100", "eqUsd": "8152.93",
                     "collateralEnabled": False},
                ],
            }]},
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client

        self.assertEqual(exchange.equity_usdt(), 72232.06)

    def test_non_finite_equity_fails_closed(self):
        client = Mock()
        client.fetch_balance.return_value = {
            "info": {"data": [{"totalEq": "10000", "details": [
                {"ccy": "USDT", "eq": "nan"},
            ]}]},
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client
        with self.assertRaisesRegex(RuntimeError, "positive finite"):
            exchange.equity_usdt()

    def test_missing_usdt_row_fails_closed(self):
        client = Mock()
        client.fetch_balance.return_value = {
            "info": {"data": [{"totalEq": "8152.93", "details": [
                {"ccy": "OKB", "eq": "100", "eqUsd": "8152.93"},
            ]}]},
        }
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client

        with self.assertRaisesRegex(RuntimeError, "no USDT currency equity"):
            exchange.equity_usdt()


class EmergencyHedgeCloseTests(unittest.TestCase):
    def test_close_carries_pos_side_when_emergency_account_is_in_hedge_mode(self):
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = Mock()
        exchange._create_order_once = Mock(return_value={"id": "close"})
        exchange.verify_fill = Mock(return_value={"filled": 2})
        exchange.position = Mock(return_value=None)
        exchange.positions = Mock(return_value=[])
        exchange.cancel_symbol = Mock()
        pos = {
            "symbol": "BTC/USDT:USDT", "side": "short", "contracts": 2,
            "markPrice": 100, "info": {"posSide": "short"},
        }

        result = exchange.close_position(pos)

        params = exchange._create_order_once.call_args.args[5]
        self.assertEqual(params["posSide"], "short")
        self.assertTrue(result["fully_closed"])


class CancellationClient:
    def __init__(self, stubborn=False):
        self.orders = [{"id": "entry-1", "symbol": "BTC/USDT:USDT"}]
        self.stubborn = stubborn

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return [] if params else list(self.orders)

    def cancel_order(self, order_id, symbol, params=None):
        if not self.stubborn:
            self.orders = [order for order in self.orders
                           if order["id"] != order_id]


class KillCancellationTests(unittest.TestCase):
    @patch("agent.exchange.time.sleep")
    def test_kill_cancellation_is_verified(self, sleep):
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = CancellationClient()
        exchange.cancel_everything()
        self.assertEqual(exchange.x.orders, [])

    @patch("agent.exchange.time.sleep")
    def test_remaining_order_makes_flatten_incomplete(self, sleep):
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = CancellationClient(stubborn=True)
        with self.assertRaisesRegex(RuntimeError, "still open: entry-1"):
            exchange.cancel_everything()


if __name__ == "__main__":
    unittest.main()
