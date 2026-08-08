"""Deterministic filled-order lifecycle regressions.

These tests deliberately drive reconciliation snapshots rather than calling
the strategy loop.  A paper broker can report an accepted order, a position,
and a terminal fill in separate snapshots; the durable journal must preserve
that lifecycle without treating the protection order as a new entry.
"""

from dataclasses import replace
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3
from pathlib import Path
import tempfile
import unittest

from agent import state
from agent.alpaca_domain import (Account, Order, OrderRequest, Position, Quote,
                                 parse_occ_symbol)
from agent.engine import Engine
from agent.market import MarketData


class LifecycleProvider:
    """Small mutable broker snapshot used by both stock and option tests."""

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
        self._next_id = 1
        self.quote = Quote(
            "SPY", datetime(2026, 8, 7, 14, tzinfo=timezone.utc),
            bid=Decimal("98"), ask=Decimal("99"),
        )

    def submit_order(self, request: OrderRequest) -> Order:
        order_id = f"order-{self._next_id}"
        self._next_id += 1
        order = Order(
            order_id, request.symbol, request.qty, request.side, "accepted",
            request.type, request.time_in_force,
            client_order_id=request.client_order_id,
        )
        self.orders_by_id[order_id] = order
        return order

    def close_position(self, symbol, qty=None, *, client_order_id=None,
                       order_type="market", time_in_force="day"):
        held = next(item for item in self.positions_live
                    if item.symbol == str(symbol).upper())
        intent = "sell_to_close" if parse_occ_symbol(held.symbol) else None
        request = OrderRequest(
            held.symbol, qty or abs(held.qty), "sell", type=order_type,
            time_in_force=time_in_force, client_order_id=client_order_id,
            position_intent=intent,
        )
        self.close_requests.append(request)
        return self.submit_order(request)

    def set_order(self, order_id, *, status, filled_qty=0, filled_avg_price=None):
        self.orders_by_id[order_id] = replace(
            self.orders_by_id[order_id], status=status,
            filled_qty=Decimal(str(filled_qty)),
            filled_avg_price=(None if filled_avg_price is None
                              else Decimal(str(filled_avg_price))),
        )

    def reconcile(self):
        return {"positions": list(self.positions_live),
                "orders": list(self.orders_by_id.values())}

    def positions(self):
        return list(self.positions_live)

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
        "strategy": {"id": "ibr", "version": "v1",
                      "execution_mode": profile},
        "risk": {},
        "execution": {"client_order_id_prefix": "lifecycle"},
        "llm": {"enabled": False},
        # Keep edge resolution on a throwaway ledger while allowing the
        # lifecycle fixture to run without requiring a pre-validated champion.
        "research": {"enabled": True, "require_validated_variant": False,
                      "champion_min_confidence": .95,
                      "db_path": str(edge_db)},
    }


class ExecutionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpaca-lifecycle-")
        root = Path(self.tmp.name)
        self.edge_db = root / "edge.sqlite3"
        self.provider = LifecycleProvider()
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

    def _bind_engine(self, profile="shares", market_data=None,
                     runtime_name="runtime"):
        # Bind before Engine construction so the fixture never touches the
        # repository's operator runtime.
        state.RUNTIME_BASE = Path(self.tmp.name) / runtime_name
        self.engine = Engine(_config(self.edge_db, profile=profile), light=True,
                             provider=self.provider, market_data=market_data)
        state.ensure_ready()

    def _journal_trades(self):
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            return db.execute(
                "SELECT symbol, action, qty, price, realized_pnl_usd "
                "FROM trades ORDER BY id"
            ).fetchall()

    def _run_lifecycle(self, *, option=False):
        symbol = "SPY260821C00600000" if option else "SPY"
        quantity = Decimal("1") if option else Decimal("246")
        entry_price = Decimal("2") if option else Decimal("101.5")
        exit_price = Decimal("1") if option else Decimal("99")
        stop_price = Decimal("99")
        multiplier = Decimal("100") if option else Decimal("1")
        request = OrderRequest(
            symbol, quantity, "buy", client_order_id="entry-1",
            position_intent="buy_to_open" if option else None,
        )
        accepted = self.provider.submit_order(request)
        plan = {
            "execution_profile": "options" if option else "shares",
            "direction": "long", "entry_price": 101.5,
            "stop_price": 99, "target_price": 105,
            "underlying_stop_price": 99, "underlying_target_price": 105,
            "underlying_symbol": "SPY", "contract_multiplier": multiplier,
            "setup_id": "lifecycle-setup", "setup_type": "ibr",
            "risk_usd": 615 if not option else 100,
            "notional": 24969 if not option else 200,
        }

        # Submission is accepted and has no position yet.
        self.engine._record_open_order(request, accepted, plan)
        self.assertEqual(state.load_state()["active_trades"], {})
        self.engine.reconcile()
        self.assertEqual(state.load_state()["active_trades"], {})

        # A position can settle before the broker's order endpoint reports a
        # terminal fill.  Attribute that position-backed fill once, without
        # creating a phantom order trade.
        self.provider.positions_live = [Position(
            symbol, quantity, "long", avg_entry_price=entry_price,
            current_price=Decimal("98.5") if not option else Decimal("1.5"),
        )]
        self.engine.reconcile()
        runtime = state.load_state()
        self.assertEqual(list(runtime["active_trades"]), [symbol])
        self.assertEqual(runtime["active_trades"][symbol]["status"], "open")

        # The next snapshot catches the order up to its terminal fill.  This
        # must not journal a second open row or regress the durable evidence.
        self.provider.set_order(accepted.id, status="filled",
                                filled_qty=quantity,
                                filled_avg_price=entry_price)
        self.engine.reconcile()
        self.assertEqual([row[1] for row in self._journal_trades()], ["open"])

        # Underlying stop protection submits one accepted close.  A second
        # monitor pass sees the pending close and must not submit another.
        monitored = self.engine._monitor_positions(
            datetime(2026, 8, 7, 14, tzinfo=timezone.utc),
            list(self.provider.positions_live),
        )
        self.assertEqual(monitored["closed"], [{"symbol": symbol, "reason": "stop"}])
        self.assertEqual(len(self.provider.close_requests), 1)
        self.assertEqual(self.provider.close_requests[0].position_intent,
                         "sell_to_close" if option else None)
        self.engine._monitor_positions(
            datetime(2026, 8, 7, 14, tzinfo=timezone.utc),
            list(self.provider.positions_live),
        )
        self.assertEqual(len(self.provider.close_requests), 1)

        close_id = next(item.id for item in self.provider.orders_by_id.values()
                        if item.id != accepted.id)
        self.provider.set_order(close_id, status="filled", filled_qty=quantity,
                                filled_avg_price=exit_price)
        self.provider.positions_live = []
        self.engine.reconcile()

        runtime = state.load_state()
        self.assertEqual(runtime["active_trades"], {})
        trades = self._journal_trades()
        self.assertEqual([row[1] for row in trades], ["open", "close"])
        self.assertEqual(sum(row[1] == "open" for row in trades), 1)
        self.assertEqual(sum(row[1] == "close" for row in trades), 1)
        self.assertEqual(trades[-1][0], symbol)
        expected_pnl = (exit_price - entry_price) * quantity * multiplier
        self.assertEqual(Decimal(str(trades[-1][4])), expected_pnl)

        # A stale broker order snapshot must not regress terminal evidence.
        self.provider.set_order(close_id, status="accepted")
        self.engine.reconcile()
        saved_close = state.load_state()["orders"][close_id]
        self.assertEqual(saved_close["status"], "filled")
        self.assertEqual(Decimal(str(saved_close["filled_avg_price"])), exit_price)
        self.assertEqual([row[1] for row in self._journal_trades()], ["open", "close"])

    def test_stock_entry_stop_exit_has_one_open_and_one_realized_close(self):
        self._bind_engine()
        self._run_lifecycle()

    def test_long_option_entry_uses_sell_to_close_and_contract_pnl(self):
        self._bind_engine(profile="options", market_data=MarketData(self.provider),
                          runtime_name="runtime-options")
        self._run_lifecycle(option=True)


if __name__ == "__main__":
    unittest.main()
