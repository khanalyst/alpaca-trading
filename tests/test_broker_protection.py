"""Broker-resident equity protection: brackets, child legs, and the poller.

Protective exits live at the broker for the shares profile.  These tests pin
the submit shape, the durable child-leg association, the close each filled leg
produces, and the poller's demotion to a backstop that must cancel before it
closes.
"""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from agent import state
from agent.config import ConfigError, validate_config
from agent.alpaca_domain import Account, Order, OrderRequest, Position, Quote
from agent.engine import Engine
from agent.execution_lifecycle import (_broker_protected, _leg_live, _leg_rows,
                                        _protective_legs)
from agent.market import MarketData
from deploy import watchdog


_LIVE_CONFIG = {
    "mode": "live",
    "broker": {"paper": False, "allow_live": True},
    "strategy": {"id": "ibr", "version": "v1", "variant_id": "ibr.baseline",
                 "selection_mode": "specific", "execution_mode": "shares"},
    "llm": {"enabled": False},
    "research": {"enabled": True, "require_validated_variant": True},
}


def _leg(order_id, role, *, status="held", price=None, qty="10"):
    return {"id": order_id, "symbol": "SPY",
            "side": "sell", "status": status,
            "type": "stop" if role == "stop" else "limit", "role": role,
            "qty": Decimal(qty), "filled_qty": Decimal("0"),
            "filled_avg_price": None,
            "limit_price": None if role == "stop" else Decimal(str(price or 105)),
            "stop_price": Decimal(str(price or 99)) if role == "stop" else None}


class ProtectionProvider:
    """Broker fake that returns bracket legs and tracks per-order cancels."""

    paper = True
    data_feed = "iex"
    options_feed = "indicative"

    class Session:
        api_key = "paper-key"
        secret_key = "paper-secret"

    session = Session()
    endpoint = "https://paper-api.alpaca.markets"

    def __init__(self):
        self.orders_by_id = {}
        self.positions_live = []
        self.submitted = []
        self.close_requests = []
        self.cancelled = []
        self.cancel_all_calls = 0
        self.cancel_error = None
        self._next_id = 1
        self.quote = Quote("SPY", datetime(2026, 8, 7, 14, tzinfo=timezone.utc),
                           bid=Decimal("98"), ask=Decimal("99"))

    def submit_order(self, request: OrderRequest) -> Order:
        order_id = f"order-{self._next_id}"
        self._next_id += 1
        self.submitted.append(request)
        legs = ()
        if request.order_class == "bracket":
            legs = (_leg(f"{order_id}-stop", "stop", price=request.stop_loss,
                         qty=str(request.qty)),
                    _leg(f"{order_id}-target", "target", price=request.take_profit,
                         qty=str(request.qty)))
        order = Order(order_id, request.symbol, request.qty, request.side,
                      "accepted", request.type, request.time_in_force,
                      client_order_id=request.client_order_id, legs=legs)
        self.orders_by_id[order_id] = order
        for leg in legs:
            self.orders_by_id[leg["id"]] = Order(
                leg["id"], "SPY", Decimal(str(leg["qty"])), leg["side"],
                leg["status"], leg["type"], "day")
        return order

    def close_position(self, symbol, qty=None, *, client_order_id=None,
                       order_type="market", time_in_force="day"):
        held = next(item for item in self.positions_live
                    if item.symbol == str(symbol).upper())
        request = OrderRequest(held.symbol, qty or abs(held.qty), "sell",
                               type=order_type, time_in_force=time_in_force,
                               client_order_id=client_order_id)
        self.close_requests.append(request)
        return self.submit_order(request)

    def cancel_order(self, order_id):
        if self.cancel_error is not None:
            raise RuntimeError(self.cancel_error)
        self.cancelled.append(str(order_id))
        self.set_order(str(order_id), status="canceled")

    def cancel_all_orders(self):
        self.cancel_all_calls += 1

    def set_order(self, order_id, *, status, filled_qty=0, filled_avg_price=None):
        self.orders_by_id[order_id] = replace(
            self.orders_by_id[order_id], status=status,
            filled_qty=Decimal(str(filled_qty)),
            filled_avg_price=(None if filled_avg_price is None
                              else Decimal(str(filled_avg_price))))

    def reconcile(self):
        return {"positions": list(self.positions_live),
                "orders": list(self.orders_by_id.values())}

    def positions(self):
        return list(self.positions_live)

    def orders(self, **_):
        return list(self.orders_by_id.values())

    def quotes(self, symbols, **_):
        return {str(symbol).upper(): [self.quote] for symbol in symbols}

    def account(self):
        return Account("paper-account", "active", Decimal("100000"),
                       Decimal("100000"), Decimal("100000"))


def _config(edge_db: Path, profile="shares"):
    return {
        "mode": "paper",
        "broker": {"paper": True},
        "universe": {"symbols": ["SPY"]},
        "session": {"timezone": "America/New_York",
                     "entries_regular_session_only": True,
                     "allow_exits_outside_session": True,
                     "force_flat_minutes_before_close": 10,
                     "reject_new_entries_minutes_before_close": 5},
        "strategy": {"id": "ibr", "version": "v1", "execution_mode": profile},
        "risk": {},
        "execution": {"client_order_id_prefix": "protection"},
        "llm": {"enabled": False},
        "research": {"enabled": True, "require_validated_variant": False,
                      "champion_min_confidence": .95, "db_path": str(edge_db)},
    }


class ProtectionHarness(unittest.TestCase):
    """Shared engine/runtime fixture; the test cases live in the subclasses."""

    NOW = datetime(2026, 8, 7, 14, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpaca-protection-")
        self.edge_db = Path(self.tmp.name) / "edge.sqlite3"
        self.provider = ProtectionProvider()
        self.engine = None
        self.original_runtime_base = state.RUNTIME_BASE
        self.addCleanup(self._cleanup_runtime)

    def _cleanup_runtime(self):
        try:
            if self.engine is not None:
                self.engine.close()
        finally:
            state.RUNTIME_BASE = self.original_runtime_base
            state.configure_runtime("paper")
            self.tmp.cleanup()

    def _bind_engine(self, profile="shares", runtime_name="runtime"):
        state.RUNTIME_BASE = Path(self.tmp.name) / runtime_name
        self.engine = Engine(_config(self.edge_db, profile=profile), light=True,
                             provider=self.provider,
                             market_data=MarketData(self.provider))
        state.ensure_ready()

    def _journal_trades(self):
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            return db.execute(
                "SELECT symbol, action, qty, price, realized_pnl_usd, reason "
                "FROM trades ORDER BY id").fetchall()

    def _signal(self, direction="long"):
        return {"symbol": "SPY", "direction": direction, "setup_id": "protect-1",
                "setup_type": "ibr", "entry_price": 101.5, "stop_price": 99,
                "target_price": 105, "confidence": 1.0}

    def _row(self):
        return {"symbol": "SPY", "quote": {"timestamp": self.NOW,
                                            "bid": 101.4, "ask": 101.5}}

    def _entry_request(self, profile="shares"):
        return self.engine._risk_order(
            "SPY", self._signal(), self._row(), self.provider.account(), [],
            self.NOW)

    def _open_bracketed_position(self, quantity=Decimal("10")):
        request = OrderRequest("SPY", quantity, "buy",
                               client_order_id="entry-protected",
                               order_class="bracket",
                               take_profit=Decimal("105"),
                               stop_loss=Decimal("99"))
        order = self.provider.submit_order(request)
        plan = {"execution_profile": "shares", "direction": "long",
                "entry_price": 101.5, "stop_price": 99, "target_price": 105,
                "underlying_stop_price": 99, "underlying_target_price": 105,
                "underlying_symbol": "SPY", "contract_multiplier": Decimal("1"),
                "setup_id": "entry-protected", "setup_type": "ibr",
                "risk_usd": 25, "notional": 1015}
        self.engine._record_open_order(request, order, plan)
        self.provider.set_order(order.id, status="filled", filled_qty=quantity,
                                filled_avg_price=101.5)
        self.provider.positions_live = [Position(
            "SPY", quantity, "long", avg_entry_price=Decimal("101.5"),
            current_price=Decimal("98.5"))]
        self.engine.reconcile()
        return order

    OPTION = "SPY260821C00600000"

    def _option_plan(self):
        return {"execution_profile": "options", "direction": "long",
                "entry_price": 101.5, "stop_price": 99, "target_price": 105,
                "underlying_stop_price": 99, "underlying_target_price": 105,
                "underlying_symbol": "SPY", "contract_multiplier": 100,
                "setup_id": "option-protect", "setup_type": "ibr",
                "risk_usd": 200, "notional": 400,
                "option": {"symbol": self.OPTION, "debit": 2.0, "multiplier": 100}}

    def _open_option_position(self, quantity=Decimal("2"), debit=Decimal("2")):
        request = OrderRequest(self.OPTION, quantity, "buy",
                               client_order_id="entry-option",
                               position_intent="buy_to_open")
        order = self.provider.submit_order(request)
        self.engine._record_open_order(request, order, self._option_plan())
        self._fill_option(order, quantity, debit)
        return order

    def _fill_option(self, order, quantity, debit=Decimal("2")):
        self.provider.set_order(order.id, status="partially_filled",
                                filled_qty=quantity, filled_avg_price=debit)
        self.provider.positions_live = [Position(
            self.OPTION, quantity, "long", avg_entry_price=Decimal(str(debit)),
            current_price=Decimal("2.2"))]
        self.engine.reconcile()

    def _resting_legs(self):
        return _leg_rows(state.load_state()["active_trades"][self.OPTION])


class BrokerProtectionTests(ProtectionHarness):
    # 1. submit shape

    def test_equity_entry_attaches_a_bracket_with_plan_stop_and_target(self):
        self._bind_engine(runtime_name="runtime-bracket")
        request, plan = self._entry_request()
        self.assertEqual(request.order_class, "bracket")
        self.assertEqual(request.stop_loss, Decimal(str(plan["stop_price"])))
        self.assertEqual(request.take_profit, Decimal(str(plan["target_price"])))
        self.assertEqual(request.symbol, "SPY")
        self.assertEqual(request.side, "buy")

    def test_bracket_rejected_by_validation_is_reported_not_raised(self):
        self._bind_engine(runtime_name="runtime-bracket-reject")
        self.engine.cfg["execution"].update({"order_type": "limit",
                                              "max_slippage_bps": 1000})
        events = []
        self.engine._event = lambda kind, payload: events.append((kind, payload))
        signal = self._signal()
        signal["target_price"] = 102
        # The quoted limit sits above the take-profit leg, so the broker would
        # reject the bracket.  The entry is dropped, not downgraded.
        row = {"symbol": "SPY", "quote": {"timestamp": self.NOW,
                                           "bid": 102.9, "ask": 103}}
        result = self.engine._risk_order("SPY", signal, row,
                                          self.provider.account(), [], self.NOW)
        self.assertIsNone(result)
        self.assertEqual(events[-1][0], "execution_reject")
        self.assertIn("straddle", events[-1][1]["reason"])

    def test_option_entry_is_never_bracketed(self):
        self._bind_engine(profile="options", runtime_name="runtime-options")
        signal = self._signal()
        row = dict(self._row())
        signal["execution_profile"] = "options"
        row["option_chain"] = [{
            "symbol": "SPY260821C00600000", "underlying_symbol": "SPY",
            "type": "call", "bid": 1.9, "ask": 2.0,
            "timestamp": self.NOW,
            "volume": 10, "open_interest": 100, "multiplier": 100,
        }]
        request, _ = self.engine._risk_order(
            "SPY", signal, row, self.provider.account(), [], self.NOW)
        self.assertIsNone(request.order_class)
        self.assertIsNone(request.take_profit)
        self.assertIsNone(request.stop_loss)

    # 2. child legs

    def test_child_leg_ids_are_persisted_on_trade_and_protection(self):
        self._bind_engine(runtime_name="runtime-legs")
        order = self._open_bracketed_position()
        runtime = state.load_state()
        trade_legs = runtime["active_trades"]["SPY"]["protective_legs"]
        self.assertEqual([leg["order_id"] for leg in trade_legs],
                         [f"{order.id}-stop", f"{order.id}-target"])
        self.assertEqual([leg["role"] for leg in trade_legs], ["stop", "target"])
        self.assertEqual([leg["order_id"] for leg in
                          runtime["protection"]["SPY"]["protective_legs"]],
                         [f"{order.id}-stop", f"{order.id}-target"])
        self.assertTrue(_broker_protected(trade_legs))

    def test_normalized_legs_drop_sdk_shape_and_keep_role_and_price(self):
        rows = _protective_legs([_leg("leg-1", "stop", price=99),
                                 _leg("leg-2", "target", price=105)])
        self.assertEqual(rows, [
            {"order_id": "leg-1", "role": "stop", "status": "held", "price": 99.0},
            {"order_id": "leg-2", "role": "target", "status": "held", "price": 105.0},
        ])

    def _filled_leg_close(self, role):
        self._bind_engine(runtime_name=f"runtime-leg-{role}")
        order = self._open_bracketed_position()
        leg_id = f"{order.id}-{role}"
        exit_price = 99 if role == "stop" else 105
        self.provider.set_order(leg_id, status="filled", filled_qty=10,
                                filled_avg_price=exit_price)
        self.provider.positions_live = []
        self.engine.reconcile()
        self.engine.reconcile()
        return exit_price

    def test_filled_stop_leg_books_exactly_one_stop_close(self):
        exit_price = self._filled_leg_close("stop")
        trades = self._journal_trades()
        self.assertEqual([row[1] for row in trades], ["open", "close"])
        self.assertEqual(trades[-1][5], "stop")
        self.assertEqual(Decimal(str(trades[-1][4])),
                         (Decimal(str(exit_price)) - Decimal("101.5")) * 10)
        self.assertEqual(state.load_state()["active_trades"], {})

    def test_filled_target_leg_books_exactly_one_target_close(self):
        exit_price = self._filled_leg_close("target")
        trades = self._journal_trades()
        self.assertEqual([row[1] for row in trades], ["open", "close"])
        self.assertEqual(trades[-1][5], "target")
        self.assertEqual(Decimal(str(trades[-1][4])),
                         (Decimal(str(exit_price)) - Decimal("101.5")) * 10)

    def test_filled_leg_records_one_paper_edge_outcome(self):
        self._bind_engine(runtime_name="runtime-leg-outcome")
        order = self._open_bracketed_position()
        recorded = []
        self.engine._record_edge_outcome = lambda *args: recorded.append(args)
        self.provider.set_order(f"{order.id}-stop", status="filled",
                                filled_qty=10, filled_avg_price=99)
        self.provider.positions_live = []
        self.engine.reconcile()
        self.engine.reconcile()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][0].get("closing_reason"), "stop")

    # 3. poller as backstop

    def test_poller_does_not_close_while_broker_legs_are_live(self):
        self._bind_engine(runtime_name="runtime-poller-quiet")
        self._open_bracketed_position()
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live))
        self.assertEqual(monitored["closed"], [])
        self.assertEqual(self.provider.close_requests, [])
        self.assertEqual(self.provider.cancelled, [])

    def test_poller_does_not_close_when_a_filled_leg_lags_the_position(self):
        # The order and position endpoints settle in either order.  A filled
        # stop leg already closed the position, so a poller that still sees
        # the stale position must not send a second, opposite-side close.
        self._bind_engine(runtime_name="runtime-poller-leg-lag")
        order = self._open_bracketed_position()
        self.provider.set_order(f"{order.id}-stop", status="filled",
                                filled_qty=10, filled_avg_price=99)
        self.engine.reconcile()
        self.assertEqual(self.provider.close_requests, [])
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live))
        self.assertEqual(monitored["closed"], [])
        self.assertEqual(self.provider.close_requests, [])
        self.assertEqual(self.provider.cancelled, [])
        self.assertEqual([row[1] for row in self._journal_trades()], ["open"])

    def test_poller_cancels_legs_before_a_force_flat_close(self):
        self._bind_engine(runtime_name="runtime-poller-force-flat")
        order = self._open_bracketed_position()
        self.engine.market.should_force_flat = lambda now: True
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live))
        self.assertEqual(monitored["closed"],
                         [{"symbol": "SPY", "reason": "before_close"}])
        self.assertEqual(self.provider.cancelled,
                         [f"{order.id}-stop", f"{order.id}-target"])
        self.assertEqual(len(self.provider.close_requests), 1)

    def test_poller_cancels_legs_before_a_max_hold_close(self):
        self._bind_engine(runtime_name="runtime-poller-max-hold")
        order = self._open_bracketed_position()
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": {**current["active_trades"]["SPY"],
                                       "hold_deadline_ts": self.NOW.timestamp() - 1}},
        })
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live))
        self.assertEqual(monitored["closed"],
                         [{"symbol": "SPY", "reason": "max_hold"}])
        self.assertEqual(self.provider.cancelled,
                         [f"{order.id}-stop", f"{order.id}-target"])

    def test_poller_fails_closed_when_a_leg_cancel_fails(self):
        self._bind_engine(runtime_name="runtime-poller-cancel-fail")
        self._open_bracketed_position()
        self.engine.market.should_force_flat = lambda now: True
        self.provider.cancel_error = "cancel unavailable"
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live))
        self.assertEqual(monitored["closed"], [])
        self.assertEqual(monitored["failed"][0]["symbol"], "SPY")
        self.assertEqual(self.provider.close_requests, [])

    # 4. missing protection

    def test_terminal_legs_on_an_open_position_close_it_fail_closed(self):
        self._bind_engine(runtime_name="runtime-legs-lost")
        order = self._open_bracketed_position()
        self.provider.set_order(f"{order.id}-stop", status="canceled")
        self.provider.set_order(f"{order.id}-target", status="canceled")
        self.engine.reconcile()
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live))
        self.assertEqual(monitored["closed"],
                         [{"symbol": "SPY", "reason": "protection_missing"}])
        self.assertEqual(len(self.provider.close_requests), 1)
        self.assertFalse(_broker_protected(
            _leg_rows(state.load_state()["active_trades"]["SPY"])))

    # 5. cleanup ordering

    def test_flatten_cancels_legs_per_symbol_and_sweeps_only_once_flat(self):
        self._bind_engine(runtime_name="runtime-flatten-order")
        order = self._open_bracketed_position()
        events = []

        def close_position(symbol, qty=None, **kwargs):
            events.append(("close", symbol))
            result = ProtectionProvider.close_position(
                self.provider, symbol, qty, **kwargs)
            self.provider.positions_live = []
            return result

        def cancel_order(order_id):
            events.append(("cancel", str(order_id)))
            ProtectionProvider.cancel_order(self.provider, order_id)

        def cancel_all_orders():
            events.append(("cancel_all", None))
            self.provider.cancel_all_calls += 1
            for saved in list(self.provider.orders_by_id.values()):
                if saved.status not in {"filled", "canceled"}:
                    self.provider.set_order(saved.id, status="canceled")

        self.provider.close_position = close_position
        self.provider.cancel_order = cancel_order
        self.provider.cancel_all_orders = cancel_all_orders
        self.provider.set_order(order.id, status="filled", filled_qty=10,
                                filled_avg_price=101.5)

        self.assertTrue(self.engine._flatten_all_impl("cleanup"))
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds.index("cancel"), 0)
        self.assertLess(kinds.index("close"), kinds.index("cancel_all"))
        self.assertEqual(
            [name for kind, name in events if kind == "cancel"],
            [f"{order.id}-stop", f"{order.id}-target"])

    def test_flatten_fails_closed_when_a_protective_leg_cancel_fails(self):
        self._bind_engine(runtime_name="runtime-flatten-cancel-fail")
        self._open_bracketed_position()
        self.provider.cancel_error = "cancel unavailable"
        self.assertFalse(self.engine._flatten_all_impl("cleanup"))
        self.assertEqual(self.provider.close_requests, [])
        self.assertEqual(self.provider.cancel_all_calls, 0)


class OptionTakeProfitTests(ProtectionHarness):
    """The only broker-resident option protection Alpaca permits.

    Options accept market and limit day orders only, so there is no bracket
    and no stop of any kind.  A resting ``sell_to_close`` limit is therefore
    the whole of what survives this process; the stop stays with the poller.
    """

    # 1. the resting take-profit

    def test_option_fill_rests_a_sell_to_close_limit_at_the_target(self):
        self._bind_engine(profile="options", runtime_name="runtime-option-tp")
        self._open_option_position()
        legs = self._resting_legs()
        self.assertEqual([leg["role"] for leg in legs], ["target"])
        # 2.0 debit at the plan's 3.5/2.5 reward-to-risk on the underlying.
        self.assertEqual(legs[0]["price"], 4.8)
        self.assertEqual(legs[0]["qty"], 2.0)
        resting = self.provider.submitted[-1]
        self.assertEqual(resting.symbol, self.OPTION)
        self.assertEqual(resting.side, "sell")
        self.assertEqual(resting.type, "limit")
        self.assertEqual(resting.time_in_force, "day")
        self.assertEqual(resting.position_intent, "sell_to_close")
        self.assertEqual(resting.qty, Decimal("2"))
        self.assertEqual(resting.limit_price, Decimal("4.8"))
        self.assertIsNone(resting.order_class)
        self.assertIsNone(resting.stop_price)

    def test_option_take_profit_is_not_resubmitted_while_it_is_live(self):
        self._bind_engine(profile="options", runtime_name="runtime-option-once")
        self._open_option_position()
        before = len(self.provider.submitted)
        self.engine.reconcile()
        self.engine.reconcile()
        self.assertEqual(len(self.provider.submitted), before)
        self.assertEqual(len(self._resting_legs()), 1)

    def test_further_partial_fill_amends_the_resting_quantity(self):
        self._bind_engine(profile="options", runtime_name="runtime-option-partial")
        order = self._open_option_position(quantity=Decimal("1"))
        first = self._resting_legs()[0]
        self.assertEqual(first["qty"], 1.0)
        self._fill_option(order, Decimal("3"))
        self.assertEqual(self.provider.cancelled, [first["order_id"]])
        legs = [leg for leg in self._resting_legs() if _leg_live(leg)]
        self.assertEqual([leg["qty"] for leg in legs], [3.0])
        self.assertEqual(self.provider.submitted[-1].qty, Decimal("3"))

    def test_filled_take_profit_books_one_close_and_one_edge_outcome(self):
        self._bind_engine(profile="options", runtime_name="runtime-option-fill")
        self._open_option_position()
        recorded = []
        self.engine._record_edge_outcome = lambda *args: recorded.append(args)
        leg_id = self._resting_legs()[0]["order_id"]
        self.provider.set_order(leg_id, status="filled", filled_qty=2,
                                filled_avg_price=Decimal("4.8"))
        self.provider.positions_live = []
        self.engine.reconcile()
        self.engine.reconcile()
        trades = self._journal_trades()
        self.assertEqual([row[1] for row in trades], ["open", "close"])
        self.assertEqual(trades[-1][5], "target")
        self.assertEqual(Decimal(str(trades[-1][4])),
                         (Decimal("4.8") - Decimal("2")) * 2 * 100)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(state.load_state()["active_trades"], {})

    # 2. the stop stays with the poller

    def test_poller_owns_the_option_stop_and_cancels_the_resting_leg(self):
        self._bind_engine(profile="options", runtime_name="runtime-option-stop")
        self._open_option_position()
        leg_id = self._resting_legs()[0]["order_id"]
        # The underlying mid is below the plan stop; only the poller can act,
        # and it must clear the resting sell before closing.
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live))
        self.assertEqual(monitored["closed"],
                         [{"symbol": self.OPTION, "reason": "stop"}])
        self.assertEqual(self.provider.cancelled, [leg_id])
        self.assertEqual(len(self.provider.close_requests), 1)

    def test_a_lone_resting_leg_is_not_treated_as_lost_protection(self):
        self._bind_engine(profile="options", runtime_name="runtime-option-lone")
        self._open_option_position()
        self.engine.market.should_force_flat = lambda now: False
        self.engine._protection_price = lambda *args, **kwargs: 102.0
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live))
        self.assertEqual(monitored["closed"], [])
        self.assertEqual(self.provider.close_requests, [])

    def test_option_take_profit_failure_leaves_the_poller_in_charge(self):
        self._bind_engine(profile="options", runtime_name="runtime-option-fail")
        events = []
        self.engine._event = lambda kind, payload: events.append((kind, payload))
        original = self.provider.submit_order

        def refuse(request):
            if request.side == "sell":
                raise RuntimeError("options take-profit rejected")
            return original(request)

        self.provider.submit_order = refuse
        self._open_option_position()
        self.assertEqual(self._resting_legs(), [])
        self.assertIn("option_take_profit_failed", [kind for kind, _ in events])
        self.provider.submit_order = original
        monitored = self.engine._monitor_positions(
            self.NOW, list(self.provider.positions_live))
        self.assertEqual(monitored["closed"],
                         [{"symbol": self.OPTION, "reason": "stop"}])


class WatchdogTests(ProtectionHarness):
    """The independent flatten-only process that bounds the software stop."""

    def _heartbeat(self, age_seconds: float, status="running"):
        state.HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        state.HEARTBEAT_FILE.write_text(json.dumps({
            "schema": 1, "status": status, "runtime_mode": "paper",
            "updated_ts": time.time() - age_seconds}), encoding="utf-8")

    def _closing_provider(self):
        original = self.provider.close_position

        def close_position(symbol, qty=None, **kwargs):
            result = original(symbol, qty, **kwargs)
            self.provider.positions_live = []
            return result

        def cancel_all_orders():
            self.provider.cancel_all_calls += 1
            for saved in list(self.provider.orders_by_id.values()):
                if saved.status not in {"filled", "canceled"}:
                    self.provider.set_order(saved.id, status="canceled")

        self.provider.close_position = close_position
        self.provider.cancel_all_orders = cancel_all_orders

    def _run_watchdog(self, runtime_name):
        return watchdog.run_once(_config(self.edge_db, profile="options"),
                                 self.provider, max_age=300)

    def test_watchdog_flattens_a_stale_trader_with_open_positions(self):
        self._bind_engine(profile="options", runtime_name="runtime-dog-stale")
        self._open_option_position()
        self._closing_provider()
        self._heartbeat(3600)
        leg_id = _leg_rows(state.load_state()["active_trades"][
            OptionTakeProfitTests.OPTION])[0]["order_id"]
        verdict = self._run_watchdog("runtime-dog-stale")
        self.assertTrue(verdict["act"])
        self.assertEqual(verdict["reason"], watchdog.ACT_STALE)
        self.assertTrue(verdict["flattened"])
        # The resting leg reserves the contracts; it is cancelled first.
        self.assertEqual(self.provider.cancelled, [leg_id])
        self.assertEqual(len(self.provider.close_requests), 1)
        self.assertEqual(self.provider.positions_live, [])

    def test_watchdog_is_inert_while_the_heartbeat_is_fresh(self):
        self._bind_engine(profile="options", runtime_name="runtime-dog-fresh")
        self._open_option_position()
        self._heartbeat(1)
        verdict = self._run_watchdog("runtime-dog-fresh")
        self.assertFalse(verdict["act"])
        self.assertEqual(verdict["reason"], watchdog.INERT_FRESH)
        self.assertEqual(self.provider.close_requests, [])

    def test_watchdog_never_races_a_trader_that_holds_the_run_lock(self):
        self._bind_engine(profile="options", runtime_name="runtime-dog-lock")
        self._open_option_position()
        self._heartbeat(3600)
        handle = state.acquire_run_lock()
        self.assertIsNotNone(handle)
        try:
            verdict = self._run_watchdog("runtime-dog-lock")
        finally:
            state.release_run_lock(handle)
        self.assertFalse(verdict["act"])
        self.assertEqual(verdict["reason"], watchdog.INERT_TRADER)
        self.assertEqual(self.provider.close_requests, [])
        self.assertEqual(self.provider.cancelled, [])

    def test_watchdog_tolerates_engine_releasing_transferred_lock(self):
        self._bind_engine(profile="options", runtime_name="runtime-dog-release")
        self._open_option_position()
        self._heartbeat(3600)

        class ReleasingEngine:
            _lock_handle = None

            def flatten_all(self, _reason):
                # Simulate an injected/current implementation that takes
                # ownership and releases the transferred descriptor itself,
                # but leaves the attribute pointing at the now-closed handle.
                state.release_run_lock(self._lock_handle)
                return True

            def close(self):
                pass

        with mock.patch.object(watchdog, "_engine",
                               return_value=ReleasingEngine()):
            verdict = self._run_watchdog("runtime-dog-release")
        self.assertTrue(verdict["flattened"])
        # A closed descriptor must not be released a second time, and the
        # watchdog must leave the mode lock available for the next owner.
        handle = state.acquire_run_lock()
        self.assertIsNotNone(handle)
        state.release_run_lock(handle)

    def test_watchdog_is_inert_when_the_broker_reports_no_exposure(self):
        self._bind_engine(profile="options", runtime_name="runtime-dog-flat")
        self._heartbeat(3600)
        verdict = self._run_watchdog("runtime-dog-flat")
        self.assertFalse(verdict["act"])
        self.assertEqual(verdict["reason"], watchdog.INERT_FLAT)

    def test_watchdog_never_opens_a_position(self):
        self._bind_engine(profile="options", runtime_name="runtime-dog-scope")
        self._open_option_position()
        self._closing_provider()
        self._heartbeat(3600)
        before = len(self.provider.submitted)
        self._run_watchdog("runtime-dog-scope")
        opened = [request for request in self.provider.submitted[before:]
                  if request.side == "buy" or
                  request.position_intent in {"buy_to_open"}]
        self.assertEqual(opened, [])
        self.assertFalse(hasattr(watchdog, "submit_order"))

    def test_watchdog_decision_treats_a_missing_heartbeat_as_stale(self):
        held = [Position("SPY", Decimal("1"), "long")]
        self.assertTrue(watchdog.decide({}, held, trader_alive=False,
                                        max_age=300)["act"])
        self.assertFalse(watchdog.decide({}, held, trader_alive=True,
                                         max_age=300)["act"])


class LiveOptionsGateTests(unittest.TestCase):
    """Live mode is equities-only until the option stop is more than software."""

    def _live(self, execution_mode="shares", **overrides):
        cfg = deepcopy(_LIVE_CONFIG)
        cfg["strategy"]["execution_mode"] = execution_mode
        cfg.update(overrides)
        return cfg

    def test_live_rejects_the_options_execution_mode(self):
        with mock.patch.dict(os.environ, {"ALPACA_LIVE_ENABLE": "true"},
                             clear=False):
            os.environ.pop("ALPACA_PAPER", None)
            with self.assertRaises(ConfigError) as caught:
                validate_config(self._live("options"))
        self.assertIn("execution_mode=options", str(caught.exception))
        self.assertIn("no bracket", str(caught.exception))

    def test_live_accepts_the_shares_execution_mode(self):
        with mock.patch.dict(os.environ, {"ALPACA_LIVE_ENABLE": "true"},
                             clear=False):
            os.environ.pop("ALPACA_PAPER", None)
            result = validate_config(self._live("shares"))
        self.assertEqual(result["strategy"]["execution_mode"], "shares")

    def test_paper_still_accepts_the_options_execution_mode(self):
        cfg = deepcopy(_LIVE_CONFIG)
        cfg.update({"mode": "paper",
                    "broker": {"paper": True, "allow_live": False}})
        cfg["strategy"]["execution_mode"] = "options"
        result = validate_config(cfg)
        self.assertEqual(result["strategy"]["execution_mode"], "options")


class CostsConfigTests(unittest.TestCase):
    """The shared research cost model must be settable from config.yaml."""

    def _cfg(self, costs, execution=None):
        cfg = {"mode": "paper", "broker": {"paper": True, "allow_live": False},
               "costs": costs}
        if execution is not None:
            cfg["execution"] = execution
        return cfg

    def test_costs_block_is_accepted_and_normalized(self):
        result = validate_config(self._cfg(
            {"spread_bps": 3, "slippage_bps": 4, "fee_bps": 0.25}))
        self.assertEqual(result["costs"], {"spread_bps": 3.0,
                                           "slippage_bps": 4.0, "fee_bps": .25})

    def test_costs_block_rejects_unknown_fields(self):
        with self.assertRaises(ConfigError):
            validate_config(self._cfg({"spread_bps": 3, "commission_bps": 1}))

    def test_expected_cost_beyond_the_runtime_slippage_cap_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config(self._cfg({"slippage_bps": 60}))
        self.assertIn("slippage cap", str(caught.exception))

    def test_expected_spread_beyond_the_runtime_spread_cap_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config(self._cfg(
                {"spread_bps": 40, "slippage_bps": 1},
                execution={"max_spread_bps": 20, "max_slippage_bps": 50}))
        self.assertIn("rejection cap", str(caught.exception))

    def test_default_config_still_omits_the_costs_block(self):
        self.assertNotIn("costs", validate_config({"mode": "paper"}))


if __name__ == "__main__":
    unittest.main()
