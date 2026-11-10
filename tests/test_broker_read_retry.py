"""A transient broker read must recover before portfolio cleanup is invoked."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from agent import state
from agent.alpaca_domain import OrderRequest, Position
from agent.alpaca_provider import (AlpacaError, AlpacaProvider, AlpacaSession,
                                   _transient_read_error)
from agent.engine import Engine
from tests.test_runtime_safety import FakeProvider, _cfg


class HttpFailure(Exception):
    def __init__(self, status):
        super().__init__("broker request failed")
        self.status_code = status


class BrokerReadRetryTests(unittest.TestCase):
    def provider(self, trading):
        return AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=trading))

    def test_transport_and_temporary_http_errors_retry_exactly_once(self):
        clock = {"timestamp": "2026-08-07T14:00:00+00:00", "is_open": True}
        for error in (TimeoutError("timeout"), ConnectionError("connection"),
                      HttpFailure(429), HttpFailure(503)):
            with self.subTest(error=type(error).__name__, status=getattr(error, "status_code", None)):
                trading = SimpleNamespace(get_clock=Mock(side_effect=[error, clock]))
                with patch("agent.alpaca_provider.time.sleep") as sleep:
                    self.assertTrue(self.provider(trading).clock().is_open)
                self.assertEqual(trading.get_clock.call_count, 2)
                sleep.assert_called_once_with(0.25)

    def test_persistent_failure_propagates_after_one_retry(self):
        trading = SimpleNamespace(get_all_positions=Mock(side_effect=TimeoutError("timeout")))
        with patch("agent.alpaca_provider.time.sleep"), self.assertRaises(AlpacaError):
            self.provider(trading).positions()
        self.assertEqual(trading.get_all_positions.call_count, 2)

    def test_authentication_and_invalid_data_are_not_retried(self):
        for response in (HttpFailure(401), HttpFailure(403),
                         {"timestamp": None, "is_open": True},
                         {"timestamp": "2026-08-07T14:00:00+00:00", "is_open": "true"}):
            with self.subTest(response=response):
                getter = (Mock(side_effect=response) if isinstance(response, Exception)
                          else Mock(return_value=response))
                trading = SimpleNamespace(get_clock=getter)
                with patch("agent.alpaca_provider.time.sleep") as sleep, self.assertRaises(AlpacaError):
                    self.provider(trading).clock()
                self.assertEqual(getter.call_count, 1)
                sleep.assert_not_called()

    def test_wrapped_requests_timeout_is_classified_without_parsing_messages(self):
        class ReadTimeout(Exception):
            pass

        class RequestsConnectionError(Exception):
            pass

        exceptions = types.ModuleType("requests.exceptions")
        exceptions.Timeout = ReadTimeout
        exceptions.ConnectionError = RequestsConnectionError

        with patch.dict(sys.modules, {"requests.exceptions": exceptions}):
            try:
                raise ReadTimeout("contains no useful error keywords")
            except ReadTimeout as cause:
                try:
                    raise AlpacaError("account request failed") from cause
                except AlpacaError as error:
                    self.assertTrue(_transient_read_error(error))
        self.assertFalse(_transient_read_error(RuntimeError("timeout 503")))

    def test_mutating_endpoints_are_never_blindly_retried(self):
        trading = SimpleNamespace(
            get_order_by_client_id=Mock(return_value=None),
            submit_order=Mock(side_effect=TimeoutError("unknown submit result")),
            cancel_orders=Mock(side_effect=TimeoutError("unknown cancel result")),
            cancel_order_by_id=Mock(side_effect=TimeoutError("unknown cancel result")))
        provider = self.provider(trading)
        with patch("agent.alpaca_provider.time.sleep") as sleep:
            with self.assertRaises(AlpacaError):
                provider.submit_order(OrderRequest(
                    "SPY", Decimal("1"), "buy", client_order_id="retry-test"))
            with self.assertRaises(AlpacaError):
                provider.cancel_all_orders()
            with self.assertRaises(AlpacaError):
                provider.cancel_order("stop-1")
        self.assertEqual(trading.submit_order.call_count, 1)
        self.assertEqual(trading.cancel_orders.call_count, 1)
        self.assertEqual(trading.cancel_order_by_id.call_count, 1)
        sleep.assert_not_called()

    def test_one_account_timeout_does_not_flatten_an_existing_position(self):
        with tempfile.TemporaryDirectory(prefix="alpaca-read-retry-") as directory:
            old_base = state.RUNTIME_BASE
            engine = None
            try:
                state.RUNTIME_BASE = Path(directory)
                provider = FakeProvider()
                position = Position("SPY", Decimal("10"), "long",
                                    market_value=Decimal("1000"))
                provider.positions_live = [position]
                account = {"id": "a", "status": "active", "equity": "100000",
                           "cash": "100000", "buying_power": "100000"}
                provider.session = SimpleNamespace(
                    api_key="key", secret_key="secret",
                    trading=SimpleNamespace(get_account=Mock(
                        side_effect=[TimeoutError("one failed read"), account])))
                provider.account = AlpacaProvider.account.__get__(provider)
                engine = Engine(_cfg(), light=True, provider=provider)
                engine._ensure_order_ready = lambda: True
                engine._wall_clock = lambda: provider.now
                engine.reconcile = Mock(return_value={"positions": [position]})
                engine._monitor_positions = Mock(return_value={"closed": [], "failed": []})
                engine._refresh_edge = Mock(return_value=False)
                with patch.object(engine, "flatten_all") as flatten, \
                     patch("agent.alpaca_provider.time.sleep"):
                    result = engine.run_once({})
                self.assertEqual(result["action"], "hold")
                self.assertEqual(provider.session.trading.get_account.call_count, 2)
                flatten.assert_not_called()
                self.assertEqual(provider.positions_live, [position])
                self.assertEqual(provider.orders_sent, [])
            finally:
                if engine is not None:
                    engine.close()
                state.RUNTIME_BASE = old_base
                state.configure_runtime("paper")
