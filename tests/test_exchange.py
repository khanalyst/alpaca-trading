import unittest
import time
from unittest.mock import Mock, patch

import ccxt

from agent.exchange import (CredentialError, EntryLiquidityRejected,
                            EntryOrderRejected, Exchange,
                            OrderSubmissionAmbiguousError)
from tests.helpers import valid_config


class FillClient:
    def __init__(self):
        self.create_calls = 0

    @staticmethod
    def market(symbol):
        return {"id": "BTC-USDT-SWAP", "contractSize": 1}

    @staticmethod
    def fetch_order(order_id, symbol, params=None):
        if order_id is None and not (params or {}).get("trigger"):
            raise ccxt.OrderNotFound("not a regular order")
        if order_id is None:
            client_id = (params or {}).get("clientOrderId")
            return {"id": "recovered", "status": "closed", "filled": 1,
                    "average": 100, "clientOrderId": client_id,
                    "symbol": symbol, "info": {"clOrdId": client_id,
                                                 "instId": "BTC-USDT-SWAP"}}
        return {
            "id": order_id,
            "symbol": symbol,
            "clientOrderId": "client-1",
            "status": "canceled",
            "filled": 3,
            "remaining": 2,
            "average": 101,
            "fee": {"currency": "USDT", "cost": -0.5},
            "info": {"clOrdId": "client-1", "instId": "BTC-USDT-SWAP"},
        }

    def create_order(self, *args, **kwargs):
        self.create_calls += 1
        raise ccxt.RequestTimeout("ambiguous")


class FundingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Exchange.__new__(Exchange)
        self.exchange.cfg = valid_config()

    def test_empty_history_is_unknown_not_zero_funding(self):
        self.exchange.x = Mock()
        self.exchange.x.fetch_funding_history.return_value = []
        self.assertIsNone(self.exchange.funding_since("BTC/USDT:USDT", 1))

    def test_explicit_zero_funding_row_remains_a_verified_zero(self):
        self.exchange.x = Mock()
        self.exchange.x.fetch_funding_history.return_value = [{"amount": 0}]
        self.assertEqual(self.exchange.funding_since("BTC/USDT:USDT", 1), 0)


class FillVerificationTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Exchange.__new__(Exchange)
        self.exchange.cfg = valid_config()
        self.exchange.alerts = None
        self.exchange.x = FillClient()

    @staticmethod
    def identified_order(**values):
        order = {
            "id": "order-1", "symbol": "BTC/USDT:USDT",
            "clientOrderId": "client-1",
            "info": {"clOrdId": "client-1",
                     "instId": "BTC-USDT-SWAP"},
            "_submission_audit": {"client_order_id": "client-1"},
        }
        order.update(values)
        return order

    def test_actual_partial_fill_fee_and_slippage_are_recorded(self):
        result = self.exchange.verify_fill(
            self.identified_order(status="open"),
            "BTC/USDT:USDT", requested=5, expected_price=100, side="buy")
        self.assertTrue(result["partial"])
        self.assertEqual(result["filled"], 3)
        self.assertEqual(result["average"], 101)
        self.assertEqual(result["fee_usd"], 0.5)
        self.assertEqual(result["slippage_usd"], 3)
        self.assertEqual(result["adverse_slippage_usd"], 3)

    def test_favorable_fill_is_negative_shortfall_not_a_cost(self):
        result = self.exchange.verify_fill(
            self.identified_order(status="open"),
            "BTC/USDT:USDT", requested=5, expected_price=102, side="buy")
        self.assertEqual(result["slippage_usd"], -3)
        self.assertEqual(result["adverse_slippage_usd"], 0)

    def test_ambiguous_create_is_recovered_without_resubmission(self):
        result = self.exchange._create_order_once(
            "BTC/USDT:USDT", "market", "buy", 1, None, {}, "test")
        self.assertEqual(result["id"], "recovered")
        self.assertEqual(self.exchange.x.create_calls, 1)

    def test_recovery_rejects_mismatched_or_absent_client_identity(self):
        for recovered in (
                {"id": "other", "clientOrderId": "wrong"},
                {"id": "other", "info": {}},
                {"id": "other", "clientOrderId": "target",
                 "info": {"clOrdId": "wrong"}},
        ):
            exchange = Exchange.__new__(Exchange)
            exchange.x = Mock()
            exchange.x.market.return_value = {"id": "BTC-USDT-SWAP"}
            exchange.x.fetch_order.return_value = recovered
            exchange.x.fetch_open_orders.return_value = []
            with patch("agent.exchange.time.sleep"):
                self.assertIsNone(exchange._recover_order(
                    "BTC/USDT:USDT", "target"))

    def test_recovery_accepts_only_an_exact_client_identity(self):
        exchange = Exchange.__new__(Exchange)
        exchange.x = Mock()
        recovered = {
            "id": "recovered", "clientOrderId": "target",
            "symbol": "BTC/USDT:USDT",
            "info": {"clOrdId": "target", "instId": "BTC-USDT-SWAP"},
        }
        exchange.x.market.return_value = {"id": "BTC-USDT-SWAP"}
        exchange.x.fetch_order.return_value = recovered

        self.assertIs(exchange._recover_order(
            "BTC/USDT:USDT", "target"), recovered)

    def test_recovery_rejects_wrong_symbol_with_correct_client_identity(self):
        exchange = Exchange.__new__(Exchange)
        exchange.x = Mock()
        exchange.x.market.return_value = {"id": "BTC-USDT-SWAP"}
        exchange.x.fetch_order.return_value = {
            "id": "recovered", "symbol": "ETH/USDT:USDT",
            "clientOrderId": "target",
            "info": {"clOrdId": "target", "instId": "ETH-USDT-SWAP"},
        }
        exchange.x.fetch_open_orders.return_value = []

        with patch("agent.exchange.time.sleep"):
            self.assertIsNone(exchange._recover_order(
                "BTC/USDT:USDT", "target"))

    def test_empty_recovery_target_makes_no_exchange_call(self):
        exchange = Exchange.__new__(Exchange)
        exchange.x = Mock()

        self.assertIsNone(exchange._recover_order("BTC/USDT:USDT", ""))
        exchange.x.fetch_order.assert_not_called()
        exchange.x.fetch_open_orders.assert_not_called()

    def test_recovery_never_swallows_authentication_failure(self):
        exchange = Exchange.__new__(Exchange)
        exchange.x = Mock()
        exchange.x.fetch_order.side_effect = ccxt.AuthenticationError(
            "bad credentials")

        with self.assertRaises(ccxt.AuthenticationError):
            exchange._recover_order("BTC/USDT:USDT", "target")

    def test_unrecovered_ambiguous_create_has_a_narrow_type(self):
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = Mock()
        exchange.x.create_order.side_effect = ccxt.RequestTimeout("timeout")
        exchange._recover_order = Mock(return_value=None)

        with self.assertRaises(OrderSubmissionAmbiguousError):
            exchange._create_order_once(
                "BTC/USDT:USDT", "limit", "buy", 1, 100, {}, "test")

    def test_fee_is_not_double_counted_when_ccxt_exposes_both_shapes(self):
        fee = {"currency": "USDT", "cost": 0.5}
        self.assertEqual(
            self.exchange._fee_usd({"fee": fee, "fees": [fee]}), 0.5)

    def test_unverified_fill_error_keeps_client_submission_audit(self):
        with patch.object(
                self.exchange.x, "fetch_order",
                return_value={
                    "id": "order-2", "status": "canceled", "filled": 0,
                    "average": None, "remaining": 1,
                    "symbol": "BTC/USDT:USDT",
                    "clientOrderId": "client-2",
                    "info": {"clOrdId": "client-2",
                             "instId": "BTC-USDT-SWAP"},
                }):
            with self.assertRaisesRegex(
                    RuntimeError, "no verified fill") as raised:
                self.exchange.verify_fill(
                    {
                        "id": "order-2",
                        "status": "open",
                        "symbol": "BTC/USDT:USDT",
                        "clientOrderId": "client-2",
                        "info": {"clOrdId": "client-2",
                                 "instId": "BTC-USDT-SWAP"},
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

    def test_full_terminal_statuses_require_and_accept_full_quantity(self):
        for status in ("filled", "closed"):
            with self.subTest(status=status):
                result = self.exchange.verify_fill(
                    self.identified_order(
                        id="full-1", status=status, filled=5,
                        remaining=0, average=100),
                    "BTC/USDT:USDT", requested=5)
                self.assertFalse(result["partial"])
                self.assertEqual(result["filled"], 5)

    def test_canceled_spellings_accept_explicit_partial_and_zero(self):
        for status in ("canceled", "cancelled", "expired", "rejected"):
            with self.subTest(status=status):
                result = self.exchange.verify_fill(
                    self.identified_order(
                        id="partial-1", status=status, filled=2,
                        remaining=3, average=101),
                    "BTC/USDT:USDT", requested=5)
                self.assertTrue(result["partial"])
                self.assertEqual(result["filled"], 2)
        with self.assertRaisesRegex(RuntimeError, "no verified fill"):
            self.exchange.verify_fill(
                self.identified_order(
                    id="zero-1", status="canceled", filled=0,
                    remaining=5, average=None),
                "BTC/USDT:USDT", requested=5)

    def test_open_partial_with_cancel_or_post_cancel_read_failure_is_ambiguous(self):
        for failure_at in ("cancel", "read"):
            with self.subTest(failure_at=failure_at):
                exchange = Exchange.__new__(Exchange)
                exchange.cfg = valid_config()
                exchange.cfg["execution"]["fill_timeout_seconds"] = 0
                exchange.alerts = None
                exchange.x = Mock()
                exchange.x.market.return_value = {
                    "id": "BTC-USDT-SWAP", "contractSize": 1}
                if failure_at == "cancel":
                    exchange.x.cancel_order.side_effect = ccxt.RequestTimeout(
                        "cancel timeout")
                else:
                    exchange.x.fetch_order.side_effect = ccxt.RequestTimeout(
                        "read timeout")
                exchange.retry = lambda fn, *args, **kwargs: fn(*args, **kwargs)

                with self.assertRaises(OrderSubmissionAmbiguousError):
                    exchange.verify_fill(
                        self.identified_order(
                            id="open-1", status="open", filled=2,
                            remaining=3, average=100),
                        "BTC/USDT:USDT", requested=5)

    def test_missing_or_mismatched_exchange_id_is_ambiguous(self):
        with self.assertRaises(OrderSubmissionAmbiguousError):
            self.exchange.verify_fill(
                self.identified_order(
                    id=None, status="closed", filled=1, average=100),
                "BTC/USDT:USDT", requested=1)

        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.cfg["execution"]["fill_timeout_seconds"] = 0
        exchange.alerts = None
        exchange.x = Mock()
        exchange.x.market.return_value = {
            "id": "BTC-USDT-SWAP", "contractSize": 1}
        exchange.x.cancel_order.return_value = {}
        exchange.x.fetch_order.return_value = {
            "id": "different", "status": "canceled", "filled": 0,
            "remaining": 1, "symbol": "BTC/USDT:USDT",
            "clientOrderId": "client-1",
            "info": {"clOrdId": "client-1",
                     "instId": "BTC-USDT-SWAP"},
        }
        exchange.retry = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        with self.assertRaises(OrderSubmissionAmbiguousError):
            exchange.verify_fill(
                self.identified_order(id="expected", status="open"),
                "BTC/USDT:USDT", requested=1)

    def test_malformed_or_inconsistent_fill_evidence_is_ambiguous(self):
        cases = (
            ({"id": "x", "status": "closed", "average": 100}, 1),
            ({"id": "x", "status": "closed", "filled": float("nan"),
              "average": 100}, 1),
            ({"id": "x", "status": "closed", "filled": 2,
              "average": 100}, 1),
            ({"id": "x", "status": "canceled", "filled": 0.5,
              "remaining": 0.25, "average": 100}, 1),
            ({"id": "x", "status": "open", "filled": 1,
              "remaining": 0, "average": 100}, 1),
            ({"id": "x", "status": True, "filled": 1,
              "remaining": 0, "average": 100}, 1),
            ({"id": "x", "status": "closed", "filled": 1,
              "remaining": 0, "average": 100,
              "info": {"realizedPnl": "nan"}}, 1),
            ({"id": "x", "status": "closed", "filled": 1,
              "remaining": 0, "average": 100,
              "fee": {"cost": "bad"}}, 1),
        )
        for order, requested in cases:
            with self.subTest(order=order):
                with self.assertRaises(OrderSubmissionAmbiguousError):
                    self.exchange.verify_fill(
                        order, "BTC/USDT:USDT", requested=requested)
        for requested in (0, -1, float("inf"), float("nan")):
            with self.subTest(requested=requested):
                with self.assertRaises(OrderSubmissionAmbiguousError):
                    self.exchange.verify_fill(
                        {"id": "x", "status": "closed", "filled": 1,
                         "average": 100},
                        "BTC/USDT:USDT", requested=requested)

    def test_only_cumulative_fill_fields_are_used(self):
        with self.assertRaises(OrderSubmissionAmbiguousError):
            self.exchange.verify_fill(
                {"id": "x", "status": "closed", "average": 100,
                 "info": {"fillSz": "1", "avgPx": "100"}},
                "BTC/USDT:USDT", requested=1)

    def test_poll_and_cancel_credentials_propagate(self):
        for failure_at in ("poll", "cancel"):
            with self.subTest(failure_at=failure_at):
                exchange = Exchange.__new__(Exchange)
                exchange.cfg = valid_config()
                exchange.alerts = None
                exchange.x = Mock()
                exchange.x.market.return_value = {
                    "id": "BTC-USDT-SWAP", "contractSize": 1}
                if failure_at == "poll":
                    exchange.cfg["execution"]["fill_timeout_seconds"] = 1
                    exchange.retry = Mock(
                        side_effect=CredentialError("bad credentials"))
                else:
                    exchange.cfg["execution"]["fill_timeout_seconds"] = 0
                    exchange.x.cancel_order.side_effect = CredentialError(
                        "bad credentials")
                with self.assertRaises(CredentialError):
                    exchange.verify_fill(
                        self.identified_order(id="x", status="open"),
                        "BTC/USDT:USDT", requested=1)

    def test_okx_order_without_client_echo_uses_exchange_id_and_symbol(self):
        order = self.identified_order(
            id="okx-order-1", status="closed", filled="1", remaining="0",
            average="100.5", clientOrderId=None,
            info={"instId": "BTC-USDT-SWAP", "state": "filled"})

        result = self.exchange.verify_fill(
            order, "BTC/USDT:USDT", requested=1)

        self.assertEqual(result["client_order_id"], "client-1")
        self.assertEqual(result["symbol"], "BTC/USDT:USDT")

    def test_present_mismatched_client_or_wrong_symbol_is_ambiguous(self):
        cases = (
            self.identified_order(
                status="closed", filled=1, remaining=0, average=100,
                clientOrderId="wrong",
                info={"clOrdId": "wrong", "instId": "BTC-USDT-SWAP"}),
            self.identified_order(
                status="closed", filled=1, remaining=0, average=100,
                symbol="ETH/USDT:USDT",
                info={"clOrdId": "client-1", "instId": "ETH-USDT-SWAP"}),
        )
        for order in cases:
            with self.subTest(order=order):
                with self.assertRaises(OrderSubmissionAmbiguousError):
                    self.exchange.verify_fill(
                        order, "BTC/USDT:USDT", requested=1)


class PostFillSafetyTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Exchange.__new__(Exchange)
        self.exchange.alerts = None
        self.exchange.x = Mock()
        self.exchange.x.market.return_value = {
            "id": "BTC-USDT-SWAP", "contractSize": 1}
        self.exchange.position = Mock()
        self.exchange.ensure_protection = Mock(return_value={
            "stop_loss": True, "take_profit": True,
        })
        self.fill = {
            "symbol": "BTC/USDT:USDT", "order_id": "entry-1",
            "client_order_id": "client-1",
            "status": "closed", "filled": 1,
            "average": 100, "partial": False,
            "submission_audit": {"client_order_id": "client-1"},
        }

    def test_position_and_protection_credentials_propagate_as_unsettled_fill(self):
        for failure_at in ("position", "protection"):
            with self.subTest(failure_at=failure_at):
                self.exchange.position.reset_mock(side_effect=True)
                self.exchange.ensure_protection.reset_mock(side_effect=True)
                self.exchange.position.return_value = {
                    "id": "position-1", "symbol": "BTC/USDT:USDT",
                    "contracts": 1, "markPrice": 100,
                    "info": {"posId": "position-1",
                             "instId": "BTC-USDT-SWAP"},
                }
                self.exchange.ensure_protection.return_value = {
                    "stop_loss": True, "take_profit": True,
                }
                error = CredentialError("bad credentials")
                if failure_at == "position":
                    self.exchange.position.side_effect = error
                else:
                    self.exchange.ensure_protection.side_effect = error
                with patch("agent.exchange.time.sleep"):
                    with self.assertRaises(CredentialError) as raised:
                        self.exchange.settle_fill(
                            dict(self.fill), "BTC/USDT:USDT", "buy", 1,
                            98, 104)
                self.assertTrue(raised.exception._post_fill_unsettled)
                self.assertEqual(
                    raised.exception._order_audit["order_id"], "entry-1")

    def test_malformed_position_or_protection_after_fill_is_ambiguous(self):
        valid_position = {
            "id": "position-1", "symbol": "BTC/USDT:USDT",
            "contracts": 1, "markPrice": 100,
            "info": {"posId": "position-1", "instId": "BTC-USDT-SWAP"},
        }
        cases = (
            ("position", "not-a-position"),
            ("position", {**valid_position, "contracts": 2}),
            ("protection", "not-a-status"),
            ("protection", {"stop_loss": "true", "take_profit": True}),
        )
        for boundary, value in cases:
            with self.subTest(boundary=boundary, value=value):
                self.exchange.position.return_value = valid_position
                self.exchange.ensure_protection.return_value = {
                    "stop_loss": True, "take_profit": True}
                if boundary == "position":
                    self.exchange.position.return_value = value
                else:
                    self.exchange.ensure_protection.return_value = value
                with patch("agent.exchange.time.sleep"):
                    with self.assertRaises(
                            OrderSubmissionAmbiguousError) as raised:
                        self.exchange.settle_fill(
                            dict(self.fill), "BTC/USDT:USDT", "buy", 1,
                            98, 104)
                self.assertTrue(raised.exception._post_fill_unsettled)


class ExchangeTypedBoundaryTests(unittest.TestCase):
    @staticmethod
    def valid_close_result(**updates):
        result = {
            "order_id": "close-1", "client_order_id": "client-close-1",
            "status": "canceled", "filled": 1, "average": 100,
            "remaining_contracts": 1, "fully_closed": False,
            "fee_usd": 0.1, "slippage_usd": 0,
            "adverse_slippage_usd": 0,
        }
        result.update(updates)
        return result

    def test_protective_order_auth_failure_is_not_best_effort(self):
        exchange = Exchange.__new__(Exchange)
        exchange.x = Mock()
        exchange.retry = Mock(side_effect=CredentialError("bad credentials"))

        with self.assertRaises(CredentialError):
            exchange.protective_orders("BTC/USDT:USDT")

    def test_closed_position_summary_preserves_auth_failures(self):
        for positions_history in (True, False):
            with self.subTest(positions_history=positions_history):
                exchange = Exchange.__new__(Exchange)
                exchange.x = Mock()
                exchange.x.has = {
                    "fetchPositionsHistory": positions_history,
                }
                exchange.retry = Mock(
                    side_effect=CredentialError("bad credentials"))
                with self.assertRaises(CredentialError):
                    exchange.closed_position_summary(
                        "BTC/USDT:USDT", 0, "long", 100, 1)

    def test_post_close_position_read_uncertainty_is_ambiguous(self):
        exchange = Exchange.__new__(Exchange)
        exchange.x = Mock()
        exchange._create_order_once = Mock(return_value={
            "id": "close-1", "status": "closed", "filled": 1,
            "average": 100,
        })
        exchange.verify_fill = Mock(return_value={
            "order_id": "close-1", "status": "closed", "filled": 1,
            "average": 100, "submission_audit": {},
        })
        exchange.position = Mock(side_effect=ccxt.RequestTimeout("timeout"))

        with self.assertRaises(OrderSubmissionAmbiguousError) as raised:
            exchange.close_position({
                "symbol": "BTC/USDT:USDT", "side": "long",
                "contracts": 1, "markPrice": 100, "info": {},
            })

        self.assertEqual(
            raised.exception._order_audit["order_id"], "close-1")

    def test_close_result_rejects_malformed_or_inconsistent_quantities(self):
        cases = (
            self.valid_close_result(
                fully_closed=True, remaining_contracts=1),
            {key: value for key, value in self.valid_close_result().items()
             if key != "remaining_contracts"},
            self.valid_close_result(remaining_contracts="bad"),
            self.valid_close_result(remaining_contracts=3),
            self.valid_close_result(filled=0.5, remaining_contracts=1),
        )
        for result in cases:
            with self.subTest(result=result):
                with self.assertRaises(OrderSubmissionAmbiguousError):
                    Exchange._validated_close_execution(
                        result, requested=2, symbol="BTC/USDT:USDT")

    def test_close_result_accepts_exact_full_and_partial_shapes(self):
        full = self.valid_close_result(
            status="closed", filled=2, remaining_contracts=0,
            fully_closed=True)
        partial = self.valid_close_result()

        self.assertTrue(Exchange._validated_close_execution(
            full, 2, "BTC/USDT:USDT")["fully_closed"])
        self.assertFalse(Exchange._validated_close_execution(
            partial, 2, "BTC/USDT:USDT")["fully_closed"])

    def test_position_history_requires_explicit_positive_price_and_qty(self):
        bad_values = (None, 0, float("nan"))
        for field in ("price", "qty"):
            for bad in bad_values:
                with self.subTest(field=field, bad=bad):
                    exchange = Exchange.__new__(Exchange)
                    exchange.x = Mock()
                    exchange.x.has = {"fetchPositionsHistory": True}
                    info = {
                        "closeAvgPx": "105", "closeTotalPos": "1",
                        "realizedPnl": "5", "fee": "-0.1",
                        "fundingFee": "0",
                    }
                    if field == "price":
                        info["closeAvgPx"] = bad
                    else:
                        info["closeTotalPos"] = bad
                    exchange.x.fetch_positions_history.return_value = [{
                        "symbol": "BTC/USDT:USDT", "timestamp": 1,
                        "info": info,
                    }]
                    exchange.x.fetch_my_trades.return_value = []
                    exchange.retry = lambda fn, *args, **kwargs: fn(
                        *args, **kwargs)
                    with self.assertRaises(OrderSubmissionAmbiguousError):
                        exchange.closed_position_summary(
                            "BTC/USDT:USDT", 0, "long", 100, 1)

    def test_trade_fallback_requires_matching_symbol_price_and_qty(self):
        rows = (
            {"symbol": "ETH/USDT:USDT", "side": "sell",
             "amount": 1, "price": 105, "info": {}},
            {"symbol": "BTC/USDT:USDT", "side": "sell",
             "amount": 0, "price": 105, "info": {}},
            {"symbol": "BTC/USDT:USDT", "side": "sell",
             "amount": 1, "price": float("nan"), "info": {}},
        )
        exchange = Exchange.__new__(Exchange)
        exchange.x = Mock()
        exchange.x.has = {"fetchPositionsHistory": False}
        exchange.x.fetch_my_trades.return_value = list(rows)
        exchange.retry = lambda fn, *args, **kwargs: fn(*args, **kwargs)

        with self.assertRaises(OrderSubmissionAmbiguousError):
            exchange.closed_position_summary(
                "BTC/USDT:USDT", 0, "long", 100, 1)


class PositionAgeRecoveryTests(unittest.TestCase):
    @staticmethod
    def _exchange(client):
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client
        return exchange

    def test_okx_position_creation_time_is_preferred(self):
        opened_ms = int((time.time() - 3_600) * 1000)
        client = Mock()
        recovered = self._exchange(client).position_opened_at({
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2,
            "info": {"cTime": str(opened_ms)},
        })
        self.assertAlmostEqual(recovered, opened_ms / 1000, places=3)
        client.fetch_my_trades.assert_not_called()

    def test_fill_history_recovers_the_last_flat_to_open_transition(self):
        now_ms = int(time.time() * 1000)
        client = Mock()
        client.fetch_my_trades.return_value = [
            {"timestamp": now_ms - 20_000, "side": "buy", "amount": 2},
            {"timestamp": now_ms - 10_000, "side": "buy", "amount": 1},
        ]
        recovered = self._exchange(client).position_opened_at({
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 3,
            "info": {},
        })
        self.assertAlmostEqual(
            recovered, (now_ms - 20_000) / 1000, places=3)

    def test_unknown_position_age_is_never_replaced_with_now(self):
        client = Mock()
        client.fetch_my_trades.return_value = []
        recovered = self._exchange(client).position_opened_at({
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 3,
            "info": {},
        })
        self.assertIsNone(recovered)


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
        client.fetch_trading_fee.return_value = {"taker": "-0.0007"}
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client

        first = exchange.taker_fee_pct("BTC/USDT:USDT")
        second = exchange.taker_fee_pct("BTC/USDT:USDT")

        self.assertAlmostEqual(first, 0.07)
        self.assertEqual(second, first)
        client.fetch_trading_fee.assert_called_once_with("BTC/USDT:USDT")

    def test_missing_account_taker_fee_fails_closed(self):
        client = Mock()
        client.fetch_trading_fee.return_value = {"maker": "-0.0002"}
        exchange = Exchange.__new__(Exchange)
        exchange.cfg = valid_config()
        exchange.alerts = None
        exchange.x = client

        with self.assertRaisesRegex(RuntimeError, "no taker fee"):
            exchange.taker_fee_pct("BTC/USDT:USDT")


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
        exchange.verify_fill = Mock(return_value={
            "order_id": "close", "client_order_id": "client-close",
            "status": "closed", "filled": 2, "average": 100,
            "fee_usd": 0, "slippage_usd": 0,
            "adverse_slippage_usd": 0, "submission_audit": {},
        })
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
