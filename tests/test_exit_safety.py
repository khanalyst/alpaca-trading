"""Exit identity, flatten booking and protection re-arm (review B-01, C1-01, C1-02).

Every close and flatten client order id must be unique per exposure, a flatten
must be booked like any other close, and a close that fails after its bracket
legs were cancelled must not leave the position without a broker stop.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock
import unittest

from agent import state
from agent.alpaca_domain import Order, OrderRequest, Position
from agent.execution_lifecycle import _broker_protected, _leg_live, _leg_rows
from tests.test_broker_protection import ProtectionHarness, ProtectionProvider


class IdempotentProvider(ProtectionProvider):
    """Returns the earlier order for a reused client id, as Alpaca does."""

    def __init__(self):
        super().__init__()
        self.by_client_id = {}

    def close_position(self, symbol, qty=None, *, client_order_id=None,
                       order_type="market", time_in_force="day"):
        if client_order_id in self.by_client_id:
            return self.by_client_id[client_order_id]
        order = super().close_position(symbol, qty, client_order_id=client_order_id,
                                       order_type=order_type,
                                       time_in_force=time_in_force)
        self.by_client_id[client_order_id] = order
        return order


class ExitIdentityTests(ProtectionHarness):
    def test_exit_ids_are_stable_per_exposure_and_unique_across_exposures(self):
        self._bind_engine(runtime_name="rt-exit-ids")
        engine = self.engine
        first = engine._exit_client_id("close", "SPY", "max_hold",
                                       trade={"order_id": "entry-1"})
        self.assertEqual(first, engine._exit_client_id(
            "close", "SPY", "max_hold", trade={"order_id": "entry-1"}))
        self.assertNotEqual(first, engine._exit_client_id(
            "close", "SPY", "max_hold", trade={"order_id": "entry-2"}))
        retry = engine._exit_client_id("close", "SPY", "max_hold",
                                       trade={"order_id": "entry-1"}, attempt=1)
        self.assertNotEqual(first, retry)
        self.assertTrue(retry.endswith("-1"))
        self.assertLessEqual(len(retry), 48)

    def test_exposure_token_survives_prefix_and_reason_truncation(self):
        self._bind_engine(runtime_name="rt-exit-ids-long")
        self.engine.cfg["execution"]["client_order_id_prefix"] = "x" * 100
        reason = "protection_data_unavailable"
        one = self.engine._exit_client_id("close", "XLRE", reason,
                                          trade={"order_id": "entry-1"}, attempt=12)
        two = self.engine._exit_client_id("close", "XLRE", reason,
                                          trade={"order_id": "entry-2"}, attempt=12)
        self.assertLessEqual(len(one), 48)
        self.assertNotEqual(one, two)
        self.assertTrue(one.endswith("-12"))

    def test_broker_only_exposure_is_bound_to_its_session(self):
        self._bind_engine(runtime_name="rt-exit-ids-session")
        position = Position("SPY", Decimal("10"), "long",
                            avg_entry_price=Decimal("101.5"))
        monday = datetime(2026, 8, 3, 15, 55, tzinfo=timezone.utc)
        self.engine._wall_clock = lambda: monday
        first = self.engine._exit_client_id("flatten", "SPY", "before_close",
                                            position=position)
        self.assertEqual(first, self.engine._exit_client_id(
            "flatten", "SPY", "before_close", position=position))
        self.engine._wall_clock = lambda: monday + timedelta(days=1)
        self.assertNotEqual(first, self.engine._exit_client_id(
            "flatten", "SPY", "before_close", position=position))

    def test_second_exposure_on_the_same_exit_path_reaches_the_broker(self):
        self.provider = IdempotentProvider()
        self._bind_engine(runtime_name="rt-second-exposure")
        provider = self.provider

        def filling_close(symbol, qty=None, **kwargs):
            order = IdempotentProvider.close_position(provider, symbol, qty, **kwargs)
            provider.set_order(order.id, status="filled", filled_qty=order.qty,
                               filled_avg_price=100.0)
            provider.positions_live = []
            return provider.orders_by_id[order.id]

        provider.close_position = filling_close
        with mock.patch("agent.runtime_control.time.sleep"):
            self._open_bracketed_position()
            self.assertTrue(self.engine.flatten_all("before_close"))
            self._open_bracketed_position()
            self.assertTrue(self.engine.flatten_all("before_close"))
        self.assertEqual(len(provider.close_requests), 2)
        first, second = (request.client_order_id
                         for request in provider.close_requests)
        self.assertNotEqual(first, second)
        self.assertEqual(state.load_state()["active_trades"], {})


class FlattenBookingTests(ProtectionHarness):
    def _filling_close(self):
        provider = self.provider

        def close_position(symbol, qty=None, **kwargs):
            order = ProtectionProvider.close_position(provider, symbol, qty, **kwargs)
            provider.set_order(order.id, status="filled", filled_qty=order.qty,
                               filled_avg_price=100.0)
            provider.positions_live = []
            return provider.orders_by_id[order.id]

        provider.close_position = close_position

    def test_flatten_close_is_booked_and_releases_the_local_book(self):
        self._bind_engine(runtime_name="rt-flatten-booked")
        self._open_bracketed_position()
        self._filling_close()
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertTrue(self.engine.flatten_all("before_close"))
        self.engine.reconcile()
        runtime = state.load_state()
        self.assertEqual(runtime["active_trades"], {})
        closes = [row for row in self._journal_trades() if row[1] == "close"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][0], "SPY")
        self.assertEqual(closes[0][5], "before_close")
        flatten_rows = [row for row in runtime["orders"].values()
                        if row.get("action") == "flatten"]
        self.assertEqual(len(flatten_rows), 1)
        self.assertTrue(flatten_rows[0].get("entry_order_id"))
        # The local book is flat, so a paused runtime can be resumed again.
        self.engine._resume_pause_invariants(
            {**runtime, "state": state.PAUSED, "operator_pause": True},
            check_orders=True)

    def test_next_trade_on_the_symbol_keeps_its_own_legs(self):
        self._bind_engine(runtime_name="rt-flatten-next-trade")
        self._open_bracketed_position()
        self._filling_close()
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertTrue(self.engine.flatten_all("before_close"))
        self.engine.reconcile()
        second = self._open_bracketed_position()
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["order_id"], second.id)
        self.assertEqual(sorted(leg["order_id"] for leg in _leg_rows(trade)),
                         sorted([f"{second.id}-stop", f"{second.id}-target"]))
        self.assertTrue(_broker_protected(_leg_rows(trade)))
        self.assertNotIn("closing_order_id", trade)


class ProtectionRearmTests(ProtectionHarness):
    def _stop_requests(self):
        return [request for request in self.provider.submitted
                if request.type == "stop"]

    def test_failed_flatten_close_rearms_a_broker_stop(self):
        self._bind_engine(runtime_name="rt-rearm-flatten")
        order = self._open_bracketed_position()

        def refusing_close(symbol, qty=None, **kwargs):
            raise RuntimeError("broker refused the close")

        self.provider.close_position = refusing_close
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertFalse(self.engine.flatten_all("before_close"))
        self.assertIn(f"{order.id}-stop", self.provider.cancelled)
        stops = self._stop_requests()
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].side, "sell")
        self.assertEqual(stops[0].qty, Decimal("10"))
        self.assertEqual(stops[0].stop_price, Decimal("99.00"))
        trade = state.load_state()["active_trades"]["SPY"]
        rearmed = [leg for leg in _leg_rows(trade) if leg.get("rearmed")]
        self.assertEqual(len(rearmed), 1)
        self.assertTrue(_leg_live(rearmed[0]))
        self.assertEqual(trade["status"], "open")
        self.assertNotIn("closing_order_id", trade)

    def test_no_rearm_while_the_close_is_still_working(self):
        self._bind_engine(runtime_name="rt-rearm-working")
        self._open_bracketed_position()
        with mock.patch("agent.runtime_control.time.sleep"):
            # The default fake leaves the close accepted and the position open.
            self.assertFalse(self.engine.flatten_all("before_close"))
        self.assertEqual(self._stop_requests(), [])
        self.assertEqual(len(self.provider.close_requests), 1)

    def test_failed_monitor_close_rearms_a_broker_stop(self):
        self._bind_engine(runtime_name="rt-rearm-monitor")
        order = self._open_bracketed_position(plan_updates={
            "hold_deadline_ts": (self.NOW - timedelta(minutes=1)).timestamp()})

        def refusing_close(symbol, qty=None, **kwargs):
            raise RuntimeError("broker refused the close")

        self.provider.close_position = refusing_close
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live), market_rows={})
        self.assertEqual([item["reason"] for item in monitored["failed"]],
                         ["max_hold"])
        self.assertIn(f"{order.id}-stop", self.provider.cancelled)
        stops = self._stop_requests()
        self.assertEqual(len(stops), 1)
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertTrue(any(leg.get("rearmed") and _leg_live(leg)
                            for leg in _leg_rows(trade)))

    def test_rearm_refuses_when_the_position_changed(self):
        self._bind_engine(runtime_name="rt-rearm-changed")
        self._open_bracketed_position()
        trade = state.load_state()["active_trades"]["SPY"]
        stale = Position("SPY", Decimal("10"), "long")
        self.provider.positions_live = []
        self.assertFalse(self.engine._rearm_protective_stop(
            "SPY", trade, stale, reason="before_close"))
        self.assertEqual(self._stop_requests(), [])


class StopOrderRequestTests(unittest.TestCase):
    def test_standalone_stop_is_valid_only_as_a_simple_equity_stop(self):
        request = OrderRequest("SPY", Decimal("10"), "sell", type="stop",
                               stop_price=Decimal("99"))
        self.assertEqual(request.stop_price, Decimal("99"))
        with self.assertRaisesRegex(ValueError, "require a finite positive stop_price"):
            OrderRequest("SPY", Decimal("10"), "sell", type="stop")
        with self.assertRaisesRegex(ValueError, "cannot carry a limit_price"):
            OrderRequest("SPY", Decimal("10"), "sell", type="stop",
                         stop_price=Decimal("99"), limit_price=Decimal("98"))
        with self.assertRaisesRegex(ValueError, "not supported for option symbols"):
            OrderRequest("SPY260821C00600000", Decimal("1"), "sell", type="stop",
                         stop_price=Decimal("1"))
        with self.assertRaisesRegex(ValueError, "stop orders are not supported"):
            OrderRequest("SPY", Decimal("10"), "sell", stop_price=Decimal("99"))


if __name__ == "__main__":
    unittest.main()
