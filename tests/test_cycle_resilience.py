"""Cycle resilience: flat by the close and no liquidation on noise (review C2-01, B-03, B-04).

A paused or not-ready trader must still flatten in the force-flat window.  A
single failed broker read, a small clock skew or one refused entry must block
entries without closing the broker-protected book; persistent failures still
fail closed.
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from agent import state
from agent.alpaca_domain import Order, Position
from agent.alpaca_provider import (SUBMIT_AMBIGUOUS, SUBMIT_REJECTED, AlpacaError,
                                   IdempotencyConflict, submit_failure_kind)
from agent.engine import Engine
from tests.test_runtime_safety import BrainFake, FakeProvider, _cfg


class _RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.runtime_tmp = tempfile.TemporaryDirectory(prefix="alpaca-resilience-")
        self.original_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.runtime_tmp.name)
        state.configure_runtime("paper")
        state.ensure_ready()
        self.addCleanup(self._cleanup_runtime)

    def _cleanup_runtime(self):
        state.RUNTIME_BASE = self.original_runtime_base
        state.configure_runtime("paper")
        self.runtime_tmp.cleanup()

    def _engine(self, provider, **kwargs):
        engine = Engine(_cfg(), light=True, provider=provider, **kwargs)
        self.addCleanup(engine.close)
        engine._wall_clock = lambda: provider.now
        return engine


class NotReadyForceFlatTests(_RuntimeCase):
    def _open_book(self, provider):
        provider.positions_live = [Position("SPY", Decimal("10"), "long",
                                            market_value=Decimal("1000"))]

    def test_paused_trader_still_flattens_in_the_force_flat_window(self):
        provider = FakeProvider()
        provider.now = provider.day.close - timedelta(minutes=5)
        self._open_book(provider)
        engine = self._engine(provider)
        state.update_state(lambda current: {**current, "operator_pause": True})
        result = engine.run_once({})
        self.assertEqual(result["action"], "force_flat")
        self.assertTrue(result["not_ready"])
        self.assertTrue(result["closed"])
        self.assertEqual([(order.symbol, order.side) for order in provider.orders_sent],
                         [("SPY", "sell")])
        self.assertEqual(provider.positions_live, [])

    def test_paused_trader_holds_outside_the_force_flat_window(self):
        provider = FakeProvider()
        self._open_book(provider)
        engine = self._engine(provider)
        state.update_state(lambda current: {**current, "operator_pause": True})
        result = engine.run_once({})
        self.assertEqual(result["action"], "hold")
        self.assertEqual(provider.orders_sent, [])

    def test_killed_runtime_is_left_to_the_operator(self):
        provider = FakeProvider()
        provider.now = provider.day.close - timedelta(minutes=5)
        self._open_book(provider)
        engine = self._engine(provider)
        state.update_state(lambda current: {**current, "state": state.KILLED,
                                             "kill_reason": "operator kill"})
        result = engine.run_once({})
        self.assertEqual(result["action"], "hold")
        self.assertEqual(provider.orders_sent, [])


class TransientReadTests(_RuntimeCase):
    def test_small_clock_skew_is_tolerated_but_a_future_clock_is_not(self):
        provider = FakeProvider()
        engine = self._engine(provider)
        engine._wall_clock = lambda: provider.now - timedelta(seconds=1)
        self.assertEqual(engine._validated_clock_timestamp(provider.clock()),
                         provider.now)
        engine._wall_clock = lambda: provider.now - timedelta(seconds=5)
        with self.assertRaisesRegex(AlpacaError, "in the future"):
            engine._validated_clock_timestamp(provider.clock())

    def test_failed_clock_read_mid_session_holds_without_touching_the_book(self):
        class BrokenClock(FakeProvider):
            def clock(self):
                raise RuntimeError("clock unavailable")

        provider = BrokenClock()
        engine = self._engine(provider)
        engine._ensure_order_ready = lambda: True
        result = engine.run_once({})
        self.assertEqual(result["action"], "hold")
        self.assertTrue(result["transient"])
        self.assertEqual(provider.cancel_calls, 0)
        self.assertEqual(provider.orders_sent, [])
        self.assertFalse(state.load_state().get("operator_pause"))

    def test_failed_clock_read_inside_the_force_flat_window_fails_closed_at_once(self):
        class BrokenClock(FakeProvider):
            def clock(self):
                raise RuntimeError("clock unavailable")

        provider = BrokenClock()
        provider.now = provider.day.close - timedelta(minutes=5)
        engine = self._engine(provider)
        engine._ensure_order_ready = lambda: True
        result = engine.run_once({})
        self.assertNotIn("transient", result)
        self.assertGreaterEqual(provider.cancel_calls, 1)


class _Forbidden(Exception):
    status_code = 403


class SubmitFailureClassificationTests(unittest.TestCase):
    def test_definite_and_ambiguous_failures_are_separated(self):
        try:
            raise AlpacaError("submit order failed: forbidden") from _Forbidden("x")
        except AlpacaError as exc:
            self.assertEqual(submit_failure_kind(exc), SUBMIT_REJECTED)

        class _Unavailable(Exception):
            status_code = 503

        try:
            raise AlpacaError("submit order failed") from _Unavailable("x")
        except AlpacaError as exc:
            self.assertEqual(submit_failure_kind(exc), SUBMIT_AMBIGUOUS)
        try:
            raise AlpacaError("submit order failed") from TimeoutError("read timed out")
        except AlpacaError as exc:
            self.assertEqual(submit_failure_kind(exc), SUBMIT_AMBIGUOUS)
        self.assertEqual(submit_failure_kind(AlpacaError("order time_in_force must be day")),
                         SUBMIT_REJECTED)
        self.assertEqual(submit_failure_kind(IdempotencyConflict("reused id")),
                         SUBMIT_REJECTED)


class EntryFailureTests(_RuntimeCase):
    def _entry_cycle(self, provider):
        now = provider.now
        bars = []
        for index in range(16):
            timestamp = now - timedelta(minutes=30 - index)
            bars.append({"timestamp": timestamp, "open": 100,
                         "high": 100.5 if index < 15 else 102,
                         "low": 99.5 if index < 15 else 100,
                         "close": 100 if index < 15 else 101.5,
                         "volume": 10 if index < 15 else 20, "atr": 1})
        snapshot = {"SPY": {"bars": bars, "quote": {
            "timestamp": now, "bid": 101.4, "ask": 101.5}}}
        cfg = _cfg()
        cfg["strategy"]["stale_minutes"] = 60
        engine = Engine(cfg, provider=provider, brain=BrainFake())
        self.addCleanup(engine.close)
        engine._wall_clock = lambda: provider.now
        return engine, snapshot

    def test_rejected_entry_is_refused_without_flattening_or_stopping(self):
        class RejectingProvider(FakeProvider):
            def submit_order(self, request):
                self.orders_sent.append(request)
                raise AlpacaError("submit order failed: insufficient buying power") \
                    from _Forbidden("forbidden")

        provider = RejectingProvider()
        engine, snapshot = self._entry_cycle(provider)
        result = engine.run_once(snapshot)
        self.assertEqual(result["action"], "decide")
        self.assertEqual(result["orders"], [])
        self.assertEqual(len(provider.orders_sent), 1)
        self.assertTrue(engine._reconciled)
        self.assertFalse(state.load_state().get("operator_pause"))

    def test_ambiguous_entry_is_recovered_by_its_client_order_id(self):
        class TimeoutProvider(FakeProvider):
            accepted = None

            def submit_order(self, request):
                self.orders_sent.append(request)
                self.accepted = Order("order-recovered", request.symbol, request.qty,
                                      request.side, "accepted", request.type,
                                      request.time_in_force,
                                      client_order_id=request.client_order_id)
                raise TimeoutError("read timed out")

            def orders(self, *, client_order_id=None, **_):
                if (client_order_id and self.accepted is not None and
                        self.accepted.client_order_id == client_order_id):
                    return [self.accepted]
                return []

        provider = TimeoutProvider()
        engine, snapshot = self._entry_cycle(provider)
        result = engine.run_once(snapshot)
        self.assertEqual(result["action"], "decide")
        self.assertEqual([order.id for order in result["orders"]], ["order-recovered"])
        self.assertFalse(state.load_state().get("operator_pause"))

    def test_unresolved_ambiguous_entry_halts_entries_and_requires_reconciliation(self):
        class LostProvider(FakeProvider):
            def submit_order(self, request):
                self.orders_sent.append(request)
                raise TimeoutError("read timed out")

        provider = LostProvider()
        engine, snapshot = self._entry_cycle(provider)
        result = engine.run_once(snapshot)
        self.assertEqual(result["action"], "decide")
        self.assertEqual(result["orders"], [])
        self.assertFalse(engine._reconciled)
        self.assertFalse(state.load_state().get("operator_pause"))


if __name__ == "__main__":
    unittest.main()
