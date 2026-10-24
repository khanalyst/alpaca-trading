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

    def test_flatten_of_a_broker_only_position_is_booked(self):
        self._bind_engine(runtime_name="rt-flatten-broker-only")
        self.provider.positions_live = [Position(
            "SPY", Decimal("10"), "long", avg_entry_price=Decimal("101.5"),
            current_price=Decimal("101"))]
        self.engine.reconcile()
        self.assertEqual(state.load_state()["active_trades"]["SPY"]["status"],
                         "unprotected")
        self._filling_close()
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertTrue(self.engine.flatten_all("before_close"))
        self.engine.reconcile()
        self.assertEqual(state.load_state()["active_trades"], {})
        closes = [row for row in self._journal_trades() if row[1] == "close"]
        self.assertEqual([(row[0], row[5]) for row in closes],
                         [("SPY", "before_close")])

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


class RealCancelProvider(ProtectionProvider):
    """Keeps a cancelled order's partial fill, as Alpaca does."""

    def cancel_order(self, order_id):
        if self.cancel_error is not None:
            raise RuntimeError(self.cancel_error)
        self.cancelled.append(str(order_id))
        order = self.orders_by_id[str(order_id)]
        self.set_order(str(order_id), status="canceled",
                       filled_qty=order.filled_qty or 0,
                       filled_avg_price=order.filled_avg_price)


class PartlyFilledLegFlattenTests(ProtectionHarness):
    def setUp(self):
        super().setUp()
        self.provider = RealCancelProvider()

    def test_partly_filled_target_does_not_block_the_flatten(self):
        self._bind_engine(runtime_name="rt-partial-target")
        order = self._open_bracketed_position(quantity=Decimal("300"))
        target = f"{order.id}-target"
        self.provider.set_order(target, status="partially_filled",
                                filled_qty=100, filled_avg_price=105)
        self.provider.positions_live = [Position(
            "SPY", Decimal("200"), "long", avg_entry_price=Decimal("101.5"))]
        self.engine.reconcile()
        self.assertEqual(state.load_state()["active_trades"]["SPY"]["closing_order_id"],
                         target)
        with mock.patch("agent.runtime_control.time.sleep"):
            self.engine.flatten_all("before_close")
        self.assertIn(target, self.provider.cancelled)
        self.assertEqual([request.qty for request in self.provider.close_requests],
                         [Decimal("200")])

    def test_partly_filled_target_and_flattened_residual_are_both_booked(self):
        self._bind_engine(runtime_name="rt-partial-target-booked")
        order = self._open_bracketed_position(quantity=Decimal("300"))
        self.provider.set_order(f"{order.id}-target", status="partially_filled",
                                filled_qty=100, filled_avg_price=105)
        self.provider.positions_live = [Position(
            "SPY", Decimal("200"), "long", avg_entry_price=Decimal("101.5"))]
        self.engine.reconcile()
        provider = self.provider

        def filling_close(symbol, qty=None, **kwargs):
            close = ProtectionProvider.close_position(provider, symbol, qty, **kwargs)
            provider.set_order(close.id, status="filled", filled_qty=close.qty,
                               filled_avg_price=100.0)
            provider.positions_live = []
            return provider.orders_by_id[close.id]

        provider.close_position = filling_close
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertTrue(self.engine.flatten_all("before_close"))
        self.engine.reconcile()
        self.assertEqual(state.load_state()["active_trades"], {})
        closes = [(row[2], row[3], row[5]) for row in self._journal_trades()
                  if row[1] == "close"]
        self.assertEqual(closes, [(100.0, 105.0, "target"), (200.0, 100.0, "before_close")])

    def test_partly_filled_option_take_profit_does_not_block_the_flatten(self):
        self._bind_engine(profile="options", runtime_name="rt-partial-option")
        self._open_option_position(quantity=Decimal("6"))
        self.engine._sync_option_take_profit()
        take_profit = self._resting_legs()[0]["order_id"]
        self.provider.set_order(take_profit, status="partially_filled",
                                filled_qty=2, filled_avg_price=2.5)
        self.provider.positions_live = [Position(
            self.OPTION, Decimal("4"), "long", avg_entry_price=Decimal("2"))]
        self.engine.reconcile()
        with mock.patch("agent.runtime_control.time.sleep"):
            self.engine.flatten_all("before_close")
        self.assertIn(take_profit, self.provider.cancelled)
        self.assertEqual([request.qty for request in self.provider.close_requests],
                         [Decimal("4")])


class RearmDurabilityTests(ProtectionHarness):
    def _stops(self):
        return [request for request in self.provider.submitted
                if request.type == "stop"]

    def test_rearmed_stop_survives_the_monitor_end_of_pass_write(self):
        self._bind_engine(runtime_name="rt-rearm-durable")
        self._open_bracketed_position(plan_updates={
            "hold_deadline_ts": (self.NOW - timedelta(minutes=1)).timestamp()})
        # A second position whose close succeeds makes the pass write its book.
        self.provider.positions_live.append(Position(
            "QQQ", Decimal("5"), "long", avg_entry_price=Decimal("400"),
            current_price=Decimal("401")))
        self.engine.reconcile()
        provider = self.provider

        def close(symbol, qty=None, **kwargs):
            if symbol == "SPY":
                raise RuntimeError("503 Service Unavailable")
            return ProtectionProvider.close_position(provider, symbol, qty, **kwargs)

        provider.close_position = close
        monitored = self.engine._monitor_positions(
            self.NOW, list(provider.positions_live), market_rows={})
        self.assertEqual([item["symbol"] for item in monitored["closed"]], ["QQQ"])
        self.assertEqual(len(self._stops()), 1)
        runtime = state.load_state()
        for bucket in ("active_trades", "protection"):
            rearmed = [leg for leg in _leg_rows(runtime[bucket]["SPY"])
                       if leg.get("rearmed") and _leg_live(leg)]
            self.assertEqual(len(rearmed), 1, bucket)
        self.assertEqual(runtime["active_trades"]["SPY"]["protection_rearm_count"], 1)
        self.assertEqual(runtime["active_trades"]["QQQ"]["status"], "closing")

    def _close_that_fills_but_times_out(self):
        provider = self.provider

        def close(symbol, qty=None, **kwargs):
            order = ProtectionProvider.close_position(provider, symbol, qty, **kwargs)
            provider.set_order(order.id, status="filled", filled_qty=order.qty,
                               filled_avg_price=100.0)
            # The position endpoint has not caught up with the fill yet.
            raise TimeoutError("read timed out")

        provider.close_position = close

    def test_monitor_never_rearms_over_a_close_that_reached_the_broker(self):
        self._bind_engine(runtime_name="rt-rearm-ambiguous-monitor")
        self._open_bracketed_position(plan_updates={
            "hold_deadline_ts": (self.NOW - timedelta(minutes=1)).timestamp()})
        self._close_that_fills_but_times_out()
        self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live), market_rows={})
        self.assertEqual(len(self.provider.close_requests), 1)
        self.assertEqual(self._stops(), [])

    def test_flatten_never_rearms_over_a_close_that_reached_the_broker(self):
        self._bind_engine(runtime_name="rt-rearm-ambiguous-flatten")
        self._open_bracketed_position()
        self._close_that_fills_but_times_out()
        with mock.patch("agent.runtime_control.time.sleep"):
            self.engine.flatten_all("before_close")
        self.assertEqual(self._stops(), [])

    def _stripped_bracket(self):
        order = self._open_bracketed_position()
        # An earlier call cancelled the bracket and could not restore a stop.
        for leg in ("stop", "target"):
            self.provider.set_order(f"{order.id}-{leg}", status="canceled")
        self.engine.reconcile()

        def refusing_close(symbol, qty=None, **kwargs):
            raise RuntimeError("503 Service Unavailable")

        self.provider.close_position = refusing_close

    def test_monitor_rearms_a_bracket_stripped_by_an_earlier_call(self):
        self._bind_engine(runtime_name="rt-rearm-stripped-monitor")
        self._stripped_bracket()
        self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live), market_rows={})
        self.assertEqual(len(self._stops()), 1)

    def test_flatten_rearms_a_bracket_stripped_by_an_earlier_call(self):
        self._bind_engine(runtime_name="rt-rearm-stripped-flatten")
        self._stripped_bracket()
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertFalse(self.engine.flatten_all("before_close"))
        self.assertEqual(len(self._stops()), 1)

    def test_a_read_failure_after_a_failed_close_still_rearms(self):
        self._bind_engine(runtime_name="rt-rearm-after-read-error")
        self._open_bracketed_position()
        provider = self.provider

        def refusing_close(symbol, qty=None, **kwargs):
            raise RuntimeError("503 Service Unavailable")

        calls = {"positions": 0}

        def flaky_positions():
            calls["positions"] += 1
            if calls["positions"] == 2:
                raise RuntimeError("502 Bad Gateway")
            return ProtectionProvider.positions(provider)

        provider.close_position = refusing_close
        provider.positions = flaky_positions
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertFalse(self.engine.flatten_all("before_close"))
        self.assertEqual(len(self._stops()), 1)
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertTrue(any(leg.get("rearmed") and _leg_live(leg)
                            for leg in _leg_rows(trade)))


class LaggingPositionsProvider(ProtectionProvider):
    """The positions endpoint lags the order endpoint by ``lag`` reads."""

    def __init__(self):
        super().__init__()
        self.lag = None
        self.flat_after_lag = False

    def _settle(self):
        if self.lag is not None:
            if self.lag > 0:
                self.lag -= 1
            elif self.flat_after_lag:
                self.positions_live = []

    def positions(self):
        self._settle()
        return list(self.positions_live)

    def reconcile(self):
        self._settle()
        return {"positions": list(self.positions_live),
                "orders": list(self.orders_by_id.values())}


class ExitFillAccountingTests(ProtectionHarness):
    """Exits are sized from the broker's order fills, never from a lagging position."""

    def _stops(self):
        return [request for request in self.provider.submitted if request.type == "stop"]

    def _working_stops(self):
        return [order.id for order in self.provider.orders_by_id.values()
                if order.type == "stop" and order.status not in
                {"filled", "canceled", "cancelled", "expired", "rejected", "replaced"}]

    def test_a_stop_filled_in_the_cancel_race_is_never_sold_again(self):
        provider = LaggingPositionsProvider()
        self.provider = provider
        self._bind_engine(runtime_name="rt-race-full-fill")
        order = self._open_bracketed_position(quantity=Decimal("300"))
        stop = f"{order.id}-stop"

        def cancel(order_id):
            provider.cancelled.append(str(order_id))
            if str(order_id) == stop:
                # The stop triggers and fills in full as its cancel arrives.
                provider.set_order(stop, status="filled", filled_qty=300,
                                   filled_avg_price=98.9)
                provider.lag, provider.flat_after_lag = 50, True
                return
            provider.set_order(str(order_id), status="canceled")

        provider.cancel_order = cancel
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertFalse(self.engine.flatten_all("before_close"))
        self.assertEqual(provider.close_requests, [])
        self.assertEqual(self._stops(), [])
        provider.lag = 0
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertTrue(self.engine.flatten_all("before_close"))
        self.assertEqual(provider.close_requests, [])
        self.assertEqual(state.load_state()["active_trades"], {})

    def test_no_close_while_a_partly_filled_leg_is_still_cancelling(self):
        self._bind_engine(runtime_name="rt-pending-cancel")
        provider = self.provider
        order = self._open_bracketed_position(quantity=Decimal("300"))
        stop = f"{order.id}-stop"
        provider.set_order(stop, status="partially_filled", filled_qty=100,
                           filled_avg_price=99)
        provider.positions_live = [Position(
            "SPY", Decimal("200"), "long", avg_entry_price=Decimal("101.5"))]
        self.engine.reconcile()
        overlaps = []

        def cancel(order_id):
            provider.cancelled.append(str(order_id))
            if provider.orders_by_id[str(order_id)].status == "canceled":
                raise RuntimeError("422 order is not cancelable")
            if str(order_id) == stop:
                # The venue has not confirmed the cancel of a triggered stop.
                provider.set_order(stop, status="pending_cancel", filled_qty=100,
                                   filled_avg_price=99)
                return
            provider.set_order(str(order_id), status="canceled")

        def close(symbol, qty=None, **kwargs):
            overlaps.append((str(qty), provider.orders_by_id[stop].status))
            closed = ProtectionProvider.close_position(provider, symbol, qty, **kwargs)
            provider.set_order(closed.id, status="filled", filled_qty=closed.qty,
                               filled_avg_price=99.5)
            provider.positions_live = []
            return provider.orders_by_id[closed.id]

        provider.cancel_order = cancel
        provider.close_position = close
        with mock.patch("agent.runtime_control.time.sleep"), \
                mock.patch("agent.execution_lifecycle.time.sleep"):
            self.engine.flatten_all("before_close")
        self.assertEqual(overlaps, [])
        # Once the venue confirms the cancel, the residual is closed.
        provider.set_order(stop, status="canceled", filled_qty=100, filled_avg_price=99)
        with mock.patch("agent.runtime_control.time.sleep"), \
                mock.patch("agent.execution_lifecycle.time.sleep"):
            self.assertTrue(self.engine.flatten_all("before_close"))
        self.assertEqual(overlaps, [("200", "canceled")])

    def test_no_stop_is_rearmed_over_a_flatten_close_that_filled(self):
        provider = LaggingPositionsProvider()
        self.provider = provider
        self._bind_engine(runtime_name="rt-filled-close-no-rearm")
        self._open_bracketed_position(quantity=Decimal("300"))
        calls = {"reconcile": 0}
        real_reconcile = provider.reconcile

        def close(symbol, qty=None, **kwargs):
            closed = ProtectionProvider.close_position(provider, symbol, qty, **kwargs)
            provider.set_order(closed.id, status="filled", filled_qty=closed.qty,
                               filled_avg_price=98.5)
            provider.lag, provider.flat_after_lag = 2, True
            calls["reconcile"] = 1
            return provider.orders_by_id[closed.id]

        def reconcile():
            if calls["reconcile"] == 1:
                calls["reconcile"] = 2
                raise RuntimeError("502 Bad Gateway")
            return real_reconcile()

        provider.close_position = close
        provider.reconcile = reconcile
        with mock.patch("agent.runtime_control.time.sleep"):
            self.assertFalse(self.engine.flatten_all("before_close"))
        self.assertEqual(self._stops(), [])
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["status"], "closing")
        for _ in range(3):  # until the position endpoint catches up
            self.engine.reconcile()
        self.assertEqual(state.load_state()["active_trades"], {})
        self.assertEqual(self._working_stops(), [])
        closes = [row for row in self._journal_trades() if row[1] == "close"]
        self.assertEqual([(row[2], row[5]) for row in closes], [(300.0, "before_close")])

    def test_a_partly_filled_bracket_is_rearmed_after_a_failed_rearm(self):
        self.provider = RealCancelProvider()
        provider = self.provider
        self._bind_engine(runtime_name="rt-partial-rearm-retry")
        order = self._open_bracketed_position(quantity=Decimal("300"))
        provider.set_order(f"{order.id}-target", status="partially_filled",
                           filled_qty=100, filled_avg_price=105)
        provider.positions_live = [Position(
            "SPY", Decimal("200"), "long", avg_entry_price=Decimal("101.5"),
            current_price=Decimal("101"))]
        self.engine.reconcile()
        stop_errors = {"left": 1}
        real_submit = provider.submit_order

        def submit(request):
            if request.type == "stop" and stop_errors["left"] > 0:
                stop_errors["left"] -= 1
                raise RuntimeError("503 Service Unavailable")
            return real_submit(request)

        def refusing_close(symbol, qty=None, **kwargs):
            raise RuntimeError("503 Service Unavailable")

        provider.submit_order = submit
        provider.close_position = refusing_close
        with mock.patch("agent.runtime_control.time.sleep"):
            self.engine.flatten_all("before_close")
            self.assertEqual(self._stops(), [])
            self.engine.flatten_all("before_close")
        self.assertEqual([(request.side, request.qty) for request in self._stops()],
                         [("sell", Decimal("200"))])
        self.engine._monitor_positions(self.NOW, list(provider.positions_live),
                                       market_rows={})
        # A later failed close cancels and re-arms; never two stops at once.
        self.assertLessEqual(len(self._working_stops()), 1)
        self.assertTrue(all(request.qty == Decimal("200") for request in self._stops()))


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
