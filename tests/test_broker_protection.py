"""Broker-resident equity protection: brackets, child legs, and the poller.

Protective exits live at the broker for the shares profile.  These tests pin
the submit shape, the durable child-leg association, the close each filled leg
produces, and the poller's demotion to a backstop that must cancel before it
closes.
"""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent import state
from agent.alpaca_domain import Account, Order, OrderRequest, Position, Quote
from agent.engine import Engine
from agent.execution_lifecycle import _broker_protected, _leg_rows, _protective_legs
from agent.market import MarketData


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


class BrokerProtectionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
