"""Cycle resilience: flat by the close and no liquidation on noise (review C2-01, B-03, B-04).

A paused or not-ready trader must still flatten in the force-flat window.  A
single failed broker read, a small clock skew or one refused entry must block
entries without closing the broker-protected book; persistent failures still
fail closed.
"""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent import state
from agent.alpaca_domain import Order, Position
from agent.alpaca_provider import (SUBMIT_AMBIGUOUS, SUBMIT_REJECTED, AlpacaError,
                                   IdempotencyConflict, submit_failure_kind)
from agent.engine import Engine
from agent.engine_cycle import TRANSIENT_FAILURE_LIMIT
from tests.test_broker_protection import _leg
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

    def test_calendar_outage_does_not_disable_the_force_flat(self):
        class FailingCalendar(FakeProvider):
            calendar_down = False

            def calendar(self, **kwargs):
                if self.calendar_down:
                    raise RuntimeError("calendar unavailable")
                return super().calendar(**kwargs)

        provider = FailingCalendar()
        provider.now = provider.day.close - timedelta(minutes=5)
        self._open_book(provider)
        engine = self._engine(provider)
        engine.market.refresh_calendar()
        provider.calendar_down = True
        state.update_state(lambda current: {**current, "operator_pause": True})
        result = engine.run_once({})
        self.assertEqual(result["action"], "force_flat")
        self.assertEqual([(order.symbol, order.side) for order in provider.orders_sent],
                         [("SPY", "sell")])

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

    def test_calendar_outage_inside_the_force_flat_window_fails_closed_at_once(self):
        class FailingCalendar(FakeProvider):
            calendar_down = False

            def calendar(self, **kwargs):
                if self.calendar_down:
                    raise RuntimeError("calendar unavailable")
                return super().calendar(**kwargs)

        provider = FailingCalendar()
        provider.now = provider.day.close - timedelta(minutes=5)
        engine = self._engine(provider)
        engine._ensure_order_ready = lambda: True
        engine.market.refresh_calendar()
        provider.calendar_down = True
        result = engine.run_once({})
        self.assertNotIn("transient", result)
        self.assertGreaterEqual(provider.cancel_calls, 1)

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


class PersistentReconcileFailureTests(_RuntimeCase):
    def test_reconciliation_failing_every_cycle_still_fails_closed(self):
        class BrokenOrders(FakeProvider):
            broken = False

            def orders(self, **kwargs):
                if self.broken:
                    raise RuntimeError("orders endpoint unavailable")
                return super().orders(**kwargs)

        provider = BrokenOrders()
        engine = self._engine(provider)
        self.assertNotIn(engine.run_once({})["action"], {"fail_closed"})
        provider.broken = True
        results = [engine.run_once({}) for _ in range(TRANSIENT_FAILURE_LIMIT)]
        for result in results[:-1]:
            self.assertEqual(result["action"], "hold")
            self.assertTrue(result["transient"])
        self.assertEqual(results[-1]["reason"], "cycle_reconciliation_failed")
        self.assertNotIn("transient", results[-1])
        self.assertTrue(state.load_state().get("operator_pause"))


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

        class _Unprocessable(Exception):
            status_code = 422

        try:
            raise AlpacaError("submit order failed") from _Unprocessable(
                '{"code": 40010001, "message": "client_order_id must be unique"}')
        except AlpacaError as exc:
            # The duplicate proves the order exists: never a plain rejection.
            self.assertEqual(submit_failure_kind(exc), SUBMIT_AMBIGUOUS)
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

    def test_refusal_of_a_retried_post_that_created_the_order_is_recovered(self):
        class _Duplicate(Exception):
            status_code = 422

        class RetriedProvider(FakeProvider):
            created = None

            def submit_order(self, request):
                # The first POST created the order and timed out at the
                # gateway; the SDK's automatic retry was refused as a
                # duplicate client order id.
                self.orders_sent.append(request)
                self.created = Order("order-first-post", request.symbol, request.qty,
                                     request.side, "accepted", request.type,
                                     request.time_in_force,
                                     client_order_id=request.client_order_id)
                raise AlpacaError("submit order failed: client_order_id must be "
                                  "unique") from _Duplicate("duplicate")

            def orders(self, *, client_order_id=None, **_):
                if (client_order_id and self.created is not None and
                        self.created.client_order_id == client_order_id):
                    return [self.created]
                return []

        provider = RetriedProvider()
        engine, snapshot = self._entry_cycle(provider)
        result = engine.run_once(snapshot)
        self.assertEqual([order.id for order in result["orders"]], ["order-first-post"])
        self.assertTrue(engine._reconciled)

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

    def test_unresolved_entry_is_adopted_with_its_legs_once_the_broker_shows_it(self):
        class LaggingProvider(FakeProvider):
            created = None
            visible = False

            def submit_order(self, request):
                self.orders_sent.append(request)
                self.created = Order(
                    "order-late", request.symbol, request.qty, request.side,
                    "accepted", request.type, request.time_in_force,
                    client_order_id=request.client_order_id,
                    legs=(_leg("order-late-stop", "stop"),
                          _leg("order-late-target", "target")))
                raise TimeoutError("read timed out")

            def orders(self, *, client_order_id=None, **_):
                if not self.visible or self.created is None:
                    return []
                if client_order_id is not None:
                    return ([self.created]
                            if client_order_id == self.created.client_order_id else [])
                # The listing does not nest bracket legs.
                return [replace(self.created, legs=())]

        provider = LaggingProvider()
        engine, snapshot = self._entry_cycle(provider)
        engine.run_once(snapshot)
        client_order_id = provider.orders_sent[0].client_order_id
        rows = [row for row in state.load_state()["orders"].values()
                if row.get("client_order_id") == client_order_id]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["submit_unresolved"])
        self.assertTrue(engine._client_order_pending(client_order_id))

        filled = provider.created.qty
        provider.created = replace(provider.created, status="filled",
                                   filled_qty=filled,
                                   filled_avg_price=Decimal("101.5"))
        provider.visible = True
        provider.positions_live = [Position("SPY", filled, "long",
                                            avg_entry_price=Decimal("101.5"),
                                            market_value=filled * Decimal("101.5"))]
        engine.reconcile()
        runtime = state.load_state()
        self.assertIn("order-late", runtime["orders"])
        self.assertNotIn("submit_unresolved", runtime["orders"]["order-late"])
        trade = runtime["active_trades"]["SPY"]
        self.assertEqual(trade["order_id"], "order-late")
        self.assertEqual(sorted(leg["order_id"] for leg in trade["protective_legs"]),
                         ["order-late-stop", "order-late-target"])
        self.assertNotEqual(trade.get("execution_profile"), "unknown")


class _LateBracketProvider(FakeProvider):
    """An entry created at the broker although its submit timed out."""

    created = None
    visible = False
    exact_down = False

    def submit_order(self, request):
        self.orders_sent.append(request)
        self.created = Order(
            "order-late", request.symbol, request.qty, request.side,
            "accepted", request.type, request.time_in_force,
            client_order_id=request.client_order_id,
            legs=(_leg("order-late-stop", "stop", qty=str(request.qty)),
                  _leg("order-late-target", "target", qty=str(request.qty))))
        raise TimeoutError("read timed out")

    def orders(self, *, client_order_id=None, **_):
        if not self.visible or self.created is None:
            return []
        if client_order_id is not None:
            if self.exact_down:
                raise RuntimeError("502 Bad Gateway")
            return [self.created] if client_order_id == self.created.client_order_id else []
        # The listing is not nested: the parent comes without legs, and each
        # leg is its own row.
        legs = [Order(leg["id"], "SPY", Decimal(str(leg["qty"])), leg["side"],
                      leg["status"], leg["type"], "day",
                      filled_qty=Decimal(str(leg.get("filled_qty") or 0)),
                      filled_avg_price=leg.get("filled_avg_price"))
                for leg in self.created.legs]
        return [replace(self.created, legs=()), *legs]

    def fill(self):
        filled = self.created.qty
        self.created = replace(self.created, status="filled", filled_qty=filled,
                               filled_avg_price=Decimal("101.5"))
        self.visible = True
        self.positions_live = [Position("SPY", filled, "long",
                                        avg_entry_price=Decimal("101.5"),
                                        market_value=filled * Decimal("101.5"))]


def _journal_actions():
    with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
        return [row[0] for row in db.execute("SELECT action FROM trades ORDER BY id")]


class EntryAdoptionTests(_RuntimeCase):
    _entry_cycle = EntryFailureTests._entry_cycle

    def test_an_adopted_entry_is_rekeyed_and_journalled_once(self):
        provider = _LateBracketProvider()
        engine, snapshot = self._entry_cycle(provider)
        engine.run_once(snapshot)
        client_order_id = provider.orders_sent[0].client_order_id
        provider.fill()
        for _ in range(3):
            engine.reconcile()
        orders = state.load_state()["orders"]
        self.assertNotIn(client_order_id, orders)
        self.assertEqual([key for key, row in orders.items()
                          if row.get("client_order_id") == client_order_id], ["order-late"])
        self.assertEqual(_journal_actions(), ["open"])
        self.assertFalse(engine._client_order_pending(client_order_id))

    def test_legs_unreadable_at_adoption_are_attached_by_a_later_reconcile(self):
        provider = _LateBracketProvider()
        engine, snapshot = self._entry_cycle(provider)
        engine.run_once(snapshot)
        provider.fill()
        provider.exact_down = True
        engine.reconcile()
        runtime = state.load_state()
        self.assertTrue(runtime["orders"]["order-late"]["legs_pending"])
        self.assertFalse(runtime["active_trades"]["SPY"].get("protective_legs"))
        provider.exact_down = False
        engine.reconcile()
        runtime = state.load_state()
        trade = runtime["active_trades"]["SPY"]
        self.assertEqual(sorted(leg["order_id"] for leg in trade["protective_legs"]),
                         ["order-late-stop", "order-late-target"])
        self.assertNotIn("legs_pending", runtime["orders"]["order-late"])
        # The bracket's own stop then closes the position (one cancels the
        # other, so the target is cancelled) and the exit is booked.
        stop, target = (next(leg for leg in provider.created.legs if leg["role"] == role)
                        for role in ("stop", "target"))
        provider.created = replace(provider.created, legs=(
            {**stop, "status": "filled", "filled_qty": provider.created.qty,
             "filled_avg_price": Decimal("99")},
            {**target, "status": "canceled"}))
        provider.positions_live = []
        for _ in range(3):
            engine.reconcile()
        self.assertEqual(state.load_state()["active_trades"], {})
        self.assertEqual(_journal_actions(), ["open", "close"])


if __name__ == "__main__":
    unittest.main()
