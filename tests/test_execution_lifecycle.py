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
from unittest import mock

from agent import engine as engine_module
from agent import state
from agent.alpaca_domain import (Account, Order, OrderRequest, Position, Quote,
                                 parse_occ_symbol)
from agent.engine import Engine
from agent.execution_lifecycle import (ExecutionLifecycleMixin,
                                       _FILLED_ORDER_STATUSES,
                                       _TERMINAL_ORDER_STATUSES, _plain, _value)
from agent.market import MarketData
from research.edge_ledger import EdgeLedger


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

    def cancel_order(self, order_id):
        self.set_order(str(order_id), status="canceled")

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


class ExecutionLifecycleFacadeTests(unittest.TestCase):
    def test_engine_reexports_lifecycle_facade_by_identity(self):
        self.assertIs(engine_module.ExecutionLifecycleMixin, ExecutionLifecycleMixin)
        self.assertIs(engine_module._FILLED_ORDER_STATUSES, _FILLED_ORDER_STATUSES)
        self.assertIs(engine_module._TERMINAL_ORDER_STATUSES, _TERMINAL_ORDER_STATUSES)
        self.assertIs(engine_module._plain, _plain)
        self.assertIs(engine_module._value, _value)
        self.assertTrue(issubclass(Engine, ExecutionLifecycleMixin))
        for name in (
                "_client_order_pending", "_record_open_order",
                "_activate_filled_trade", "_close_position", "_protection_price",
                "_monitor_positions", "reconcile", "_record_edge_outcome"):
            self.assertIs(getattr(Engine, name), getattr(ExecutionLifecycleMixin, name))


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

    def _submit_stock_entry(self, quantity=Decimal("10"),
                            client_order_id="entry-lifecycle"):
        request = OrderRequest(
            "SPY", quantity, "buy", client_order_id=client_order_id)
        order = self.provider.submit_order(request)
        plan = {
            "execution_profile": "shares", "direction": "long",
            "entry_price": 101.5, "stop_price": 99, "target_price": 105,
            "underlying_stop_price": 99, "underlying_target_price": 105,
            "underlying_symbol": "SPY", "contract_multiplier": Decimal("1"),
            "setup_id": client_order_id, "setup_type": "ibr",
            "risk_usd": 100, "notional": 1000,
        }
        self.engine._record_open_order(request, order, plan)
        return request, order

    def test_broker_fill_net_pnl_does_not_double_count_slippage(self):
        self._bind_engine(runtime_name="runtime-pnl-attribution")
        attribution = self.engine._close_pnl_attribution(
            {"execution_profile": "shares", "fees": 1.0, "slippage": 2.0},
            10.0, 100.0, 101.0, 10.0, 1.0)
        self.assertEqual(attribution["gross_pnl"], 10.0)
        self.assertEqual(attribution["fees"], 1.0)
        self.assertEqual(attribution["slippage"], 2.0)
        self.assertEqual(attribution["net_pnl"], 9.0)
        self.assertEqual(attribution["pnl_semantics"],
                         "broker_fill_pnl_minus_fees")

    def test_partial_fill_progression_is_monotonic_and_incremental(self):
        self._bind_engine(runtime_name="runtime-partial")
        _, entry = self._submit_stock_entry()

        for status, filled_qty in (
                ("partially_filled", 3), ("partially_filled", 7),
                ("partially_filled", 3), ("filled", 10)):
            self.provider.set_order(entry.id, status=status,
                                    filled_qty=filled_qty, filled_avg_price=101.5)
            self.engine.reconcile()
        # A repeated terminal fill snapshot with no position must not turn the
        # just-observed fill into a phantom disappearance/close.
        self.engine.reconcile()

        runtime = state.load_state()
        saved = runtime["orders"][entry.id]
        self.assertEqual(saved["status"], "filled")
        self.assertEqual(saved["filled_qty"], 10)
        self.assertEqual(saved["logged_filled_qty"], 10)
        self.assertEqual(runtime["active_trades"]["SPY"]["qty"], "10.0")
        trades = self._journal_trades()
        self.assertEqual([row[1] for row in trades], ["open"] * 3)
        self.assertEqual([row[2] for row in trades], [3.0, 4.0, 3.0])
        self.assertEqual(sum(row[2] for row in trades), 10.0)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            evidence = db.execute(
                "SELECT qty, notional, execution_profile, vehicle, "
                "reference_price, entry_reference, requested_qty, planned_qty, "
                "cumulative_filled_qty, fill_fraction FROM trades "
                "WHERE action='open' ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in evidence], [3.0, 4.0, 3.0])
        self.assertEqual([row[2:4] for row in evidence],
                         [("shares", "equity")] * 3)
        self.assertEqual([row[4:8] for row in evidence],
                         [(101.5, 101.5, 10.0, 10.0)] * 3)
        self.assertEqual([row[8:] for row in evidence],
                         [(3.0, .3), (7.0, .7), (10.0, 1.0)])

    def test_terminal_rejection_keeps_max_partial_fill_and_is_not_pending(self):
        self._bind_engine(runtime_name="runtime-terminal-partial")
        _, entry = self._submit_stock_entry(client_order_id="entry-terminal")
        self.provider.set_order(entry.id, status="partially_filled",
                                filled_qty=7, filled_avg_price=101.5)
        self.engine.reconcile()
        self.provider.set_order(entry.id, status="rejected", filled_qty=3,
                                filled_avg_price=101.5)
        self.engine.reconcile()
        self.engine.reconcile()

        saved = state.load_state()["orders"][entry.id]
        self.assertEqual(saved["status"], "rejected")
        self.assertEqual(saved["filled_qty"], 7)
        self.assertFalse(self.engine._client_order_pending("entry-terminal"))
        self.assertEqual([row[2] for row in self._journal_trades()], [7.0])

    def test_terminal_rejection_with_positive_fill_keeps_status_when_position_first(self):
        for status, runtime_name in (("partially_filled", "runtime-position-partial"),
                                     ("rejected", "runtime-position-rejected"),
                                     ("canceled", "runtime-position-canceled")):
            self._bind_engine(runtime_name=runtime_name)
            _, entry = self._submit_stock_entry(
                client_order_id=f"entry-position-{status}")
            self.provider.set_order(entry.id, status=status, filled_qty=3,
                                    filled_avg_price=101.5)
            self.provider.positions_live = [Position(
                "SPY", Decimal("3"), "long", avg_entry_price=Decimal("101.5"),
                current_price=Decimal("100"),
            )]
            self.engine.reconcile()

            runtime = state.load_state()
            self.assertEqual(runtime["orders"][entry.id]["status"], status)
            self.assertEqual(runtime["active_trades"]["SPY"]["status"], "open")
            self.assertEqual(runtime["active_trades"]["SPY"]["qty"], "3")
            self.assertEqual([row[2] for row in self._journal_trades()], [3.0])

    def test_terminal_status_absorbs_later_filled_snapshot_without_duplicate_open(self):
        for terminal, runtime_name in (("rejected", "runtime-terminal-absorbing-rejected"),
                                        ("canceled", "runtime-terminal-absorbing-canceled")):
            self._bind_engine(runtime_name=runtime_name)
            _, entry = self._submit_stock_entry(
                client_order_id=f"entry-absorbing-{terminal}")
            self.provider.set_order(entry.id, status=terminal, filled_qty=3,
                                    filled_avg_price=101.5)
            self.provider.positions_live = []
            self.engine.reconcile()
            self.provider.set_order(entry.id, status="filled", filled_qty=10,
                                    filled_avg_price=101.5)
            self.engine.reconcile()

            runtime = state.load_state()
            saved = runtime["orders"][entry.id]
            self.assertEqual(saved["status"], terminal)
            self.assertEqual(saved["filled_qty"], 10)
            self.assertEqual(saved["logged_filled_qty"], 10)
            self.assertEqual([row[2] for row in self._journal_trades()], [3.0, 7.0])

    def test_durable_fill_growth_activates_before_status_catches_up(self):
        self._bind_engine(runtime_name="runtime-accepted-filled")
        _, entry = self._submit_stock_entry(client_order_id="entry-accepted-filled")
        self.provider.set_order(entry.id, status="accepted", filled_qty=3,
                                filled_avg_price=101.5)
        self.provider.positions_live = []
        self.engine.reconcile()

        runtime = state.load_state()
        self.assertEqual(runtime["orders"][entry.id]["filled_qty"], 3)
        self.assertEqual(runtime["orders"][entry.id]["logged_filled_qty"], 3)
        self.assertEqual(runtime["active_trades"]["SPY"]["qty"], "3.0")
        self.assertEqual([row[2] for row in self._journal_trades()], [3.0])

    def test_partial_fill_without_position_is_retained_until_position_appears(self):
        self._bind_engine(runtime_name="runtime-partial-no-position")
        _, entry = self._submit_stock_entry(client_order_id="entry-no-position")
        self.provider.set_order(entry.id, status="partially_filled",
                                filled_qty=3, filled_avg_price=101.5)
        self.provider.positions_live = []
        self.engine.reconcile()

        runtime = state.load_state()
        self.assertEqual(runtime["active_trades"]["SPY"]["qty"], "3.0")
        self.assertEqual([row[1] for row in self._journal_trades()], ["open"])

        self.provider.positions_live = [Position(
            "SPY", Decimal("3"), "long", avg_entry_price=Decimal("101.5"),
            current_price=Decimal("100"),
        )]
        self.engine.reconcile()
        runtime = state.load_state()
        self.assertEqual(runtime["active_trades"]["SPY"]["qty"], "3")
        self.assertEqual([row[1] for row in self._journal_trades()], ["open"])

    def test_partial_cancellation_books_only_actual_exposure(self):
        self._bind_engine(runtime_name="runtime-partial-accounting")
        request = OrderRequest("SPY", Decimal("10"), "buy",
                               client_order_id="entry-partial-accounting")
        entry = self.provider.submit_order(request)
        plan = {
            "execution_profile": "shares", "direction": "long",
            "entry_price": 100, "stop_price": 99, "target_price": 102,
            "underlying_stop_price": 99, "underlying_target_price": 102,
            "underlying_symbol": "SPY", "contract_multiplier": 1,
            "setup_id": "entry-partial-accounting", "setup_type": "ibr",
            "risk_usd": 10, "notional": 1000,
        }
        self.engine._record_open_order(request, entry, plan)
        self.provider.set_order(entry.id, status="partially_filled", filled_qty=3,
                                filled_avg_price=100)
        self.provider.positions_live = [Position(
            "SPY", Decimal("3"), "long", avg_entry_price=Decimal("100"),
            current_price=Decimal("100"))]
        self.engine.reconcile()

        self.provider.set_order(entry.id, status="canceled", filled_qty=3,
                                filled_avg_price=100)
        # A transient empty position snapshot must not turn cancellation of
        # the unfilled remainder into a close of the three filled shares.
        self.provider.positions_live = []
        self.engine.reconcile()
        runtime = state.load_state()
        trade = runtime["active_trades"]["SPY"]
        self.assertEqual(runtime["orders"][entry.id]["status"], "canceled")
        self.assertEqual(trade["qty"], "3")
        self.assertEqual(trade["risk_usd"], 3.0)
        self.assertEqual(trade["notional"], 300.0)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            row = db.execute(
                "SELECT qty, price, notional, risk_usd FROM trades "
                "WHERE action='open'").fetchone()
        self.assertEqual(row, (3.0, 100.0, 300.0, 3.0))

    def test_cap_binding_fill_keeps_configured_budget_in_order_and_trade_telemetry(self):
        self._bind_engine(runtime_name="runtime-cap-telemetry")
        request = OrderRequest("SPY", Decimal("25"), "buy",
                               client_order_id="entry-cap-telemetry")
        entry = self.provider.submit_order(request)
        plan = {
            "execution_profile": "shares", "direction": "long",
            "entry_price": 100, "stop_price": 99.5, "target_price": 101,
            "underlying_stop_price": 99.5, "underlying_target_price": 101,
            "underlying_symbol": "SPY", "contract_multiplier": 1,
            "setup_id": "entry-cap-telemetry", "setup_type": "ibr",
            # A 25% notional cap reduced a $200 configured budget to $12.50
            # for the immutable 25-share plan at a 50-bps stop.
            "configured_risk_budget_usd": 200.0,
            "risk_budget_usd": 200.0,
            "planned_risk_usd": 12.5,
            "risk_usd": 12.5, "notional": 2500.0,
        }
        self.engine._record_open_order(request, entry, plan)
        self.provider.set_order(entry.id, status="filled", filled_qty=25,
                                filled_avg_price=100)
        self.provider.positions_live = [Position(
            "SPY", Decimal("25"), "long", avg_entry_price=Decimal("100"),
            current_price=Decimal("100"))]
        self.engine.reconcile()

        runtime = state.load_state()
        order = runtime["orders"][entry.id]
        trade = runtime["active_trades"]["SPY"]
        for row in (order, trade):
            self.assertEqual(row["configured_risk_budget_usd"], 200.0)
            self.assertEqual(row["risk_budget_usd"], 200.0)
            self.assertEqual(row["planned_risk_usd"], 12.5)
            self.assertEqual(row["delivered_risk_usd"], 12.5)
            self.assertEqual(row["planned_to_configured_risk_ratio"], .0625)
            self.assertEqual(row["delivered_to_configured_risk_ratio"], .0625)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            order_row = db.execute(
                "SELECT configured_risk_budget_usd, planned_risk_usd, "
                "delivered_risk_usd, planned_to_configured_risk_ratio, "
                "delivered_to_configured_risk_ratio FROM orders "
                "WHERE action='reconcile' ORDER BY id DESC LIMIT 1").fetchone()
            trade_row = db.execute(
                "SELECT configured_risk_budget_usd, planned_risk_usd, "
                "delivered_risk_usd, planned_to_configured_risk_ratio, "
                "delivered_to_configured_risk_ratio FROM trades "
                "WHERE action='open' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(order_row, (200.0, 12.5, 12.5, .0625, .0625))
        self.assertEqual(trade_row, (200.0, 12.5, 12.5, .0625, .0625))

    def test_incremental_fill_journal_uses_implied_weighted_price(self):
        self._bind_engine(runtime_name="runtime-weighted-accounting")
        request = OrderRequest("SPY", Decimal("5"), "buy",
                               client_order_id="entry-weighted-accounting")
        entry = self.provider.submit_order(request)
        plan = {
            "execution_profile": "shares", "direction": "long",
            "entry_price": 100, "stop_price": 99, "target_price": 104,
            "underlying_stop_price": 99, "underlying_target_price": 104,
            "underlying_symbol": "SPY", "contract_multiplier": 1,
            "setup_id": "entry-weighted-accounting", "setup_type": "ibr",
            "risk_usd": 5, "notional": 500,
        }
        self.engine._record_open_order(request, entry, plan)
        self.provider.set_order(entry.id, status="partially_filled", filled_qty=2,
                                filled_avg_price=100)
        self.provider.positions_live = [Position(
            "SPY", Decimal("2"), "long", avg_entry_price=Decimal("100"),
            current_price=Decimal("100"))]
        self.engine.reconcile()
        self.provider.set_order(entry.id, status="filled", filled_qty=5,
                                filled_avg_price=101.2)
        self.provider.positions_live = [Position(
            "SPY", Decimal("5"), "long", avg_entry_price=Decimal("101.2"),
            current_price=Decimal("101"))]
        self.engine.reconcile()

        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["qty"], "5")
        self.assertEqual(trade["entry_price"], 101.2)
        self.assertEqual(trade["notional"], 506.0)
        self.assertEqual(trade["risk_usd"], 11.0)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            rows = db.execute(
                "SELECT qty, price, notional, risk_usd FROM trades "
                "WHERE action='open' ORDER BY id").fetchall()
        self.assertEqual(rows, [(2.0, 100.0, 200.0, 2.0),
                                (3.0, 102.0, 306.0, 9.0)])

    def test_singular_option_profile_uses_premium_risk_and_option_edge_vehicle(self):
        """Legacy ``option`` aliases must follow options semantics end-to-end."""
        self._bind_engine(profile="options", runtime_name="runtime-option-alias")
        symbol = "SPY260821C00600000"
        request = OrderRequest(symbol, Decimal("1"), "buy",
                               client_order_id="entry-option-alias",
                               position_intent="buy_to_open")
        entry = self.provider.submit_order(request)
        plan = {
            "execution_profile": "option", "direction": "long",
            "entry_price": 101.5, "stop_price": 99,
            "underlying_stop_price": 99, "underlying_target_price": 105,
            "underlying_symbol": "SPY", "contract_multiplier": 100,
            "option": {"symbol": symbol, "debit": 1.5, "multiplier": 100},
            # Deliberately contradictory legacy risk: treating this as an
            # equity stop-distance would produce 9,750 instead of premium
            # risk 150.
            "risk_usd": 9750, "notional": 150,
            "setup_id": "entry-option-alias", "setup_type": "ibr",
            "variant_id": "option-alias-variant", "candidate_id": "option-candidate",
        }
        self.engine._record_open_order(request, entry, plan)
        self.provider.set_order(entry.id, status="filled", filled_qty=1,
                                filled_avg_price=1.5)
        self.provider.positions_live = [Position(
            symbol, Decimal("1"), "long", avg_entry_price=Decimal("1.5"),
            current_price=Decimal("1.6"),
        )]
        self.engine.reconcile()

        trade = state.load_state()["active_trades"][symbol]
        self.assertEqual(trade["vehicle"], "option")
        self.assertEqual(trade["entry_reference"], 1.5)
        self.assertEqual(trade["risk_usd"], 150.0)
        self.assertEqual(trade["delivered_risk_usd"], 150.0)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            row = db.execute(
                "SELECT risk_usd, intended_risk_usd, delivered_risk_usd "
                "FROM trades WHERE action='open'").fetchone()
        self.assertEqual(row, (150.0, 9750.0, 150.0))

        self.engine._record_edge_outcome(trade, 15.0, 10.0, 1.65)
        outcome = self.engine._pending_edge_outcomes[-1]["outcome"]
        self.assertEqual(outcome["vehicle"], "option")
        self.assertEqual(outcome["risk_usd"], 150.0)

    def test_unprotected_position_stays_unprotected_and_fail_closed_close_dedupes(self):
        self._bind_engine(runtime_name="runtime-unprotected")
        self.provider.positions_live = [Position(
            "SPY", Decimal("2"), "long", avg_entry_price=Decimal("101"),
            current_price=Decimal("100"),
        )]
        self.engine.reconcile()
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["status"], "unprotected")
        self.assertEqual(trade["execution_profile"], "unknown")
        self.assertEqual(trade["risk_usd"], 0.0)

        monitored = self.engine._monitor_positions(
            datetime(2026, 8, 7, 14, tzinfo=timezone.utc),
            list(self.provider.positions_live),
        )
        self.assertEqual(monitored["closed"],
                         [{"symbol": "SPY", "reason": "protection_missing"}])
        self.assertEqual(len(self.provider.close_requests), 1)
        self.assertEqual(
            self.engine._monitor_positions(
                datetime(2026, 8, 7, 14, tzinfo=timezone.utc),
                list(self.provider.positions_live),
            )["closed"], [])
        self.assertEqual(len(self.provider.close_requests), 1)

    def test_rejected_zero_fill_entry_cannot_attribute_same_symbol_position(self):
        self._bind_engine(runtime_name="runtime-rejected-zero-fill")
        _, entry = self._submit_stock_entry(client_order_id="entry-rejected-zero")
        self.provider.set_order(entry.id, status="rejected", filled_qty=0)
        position = Position(
            "SPY", Decimal("2"), "long", avg_entry_price=Decimal("101"),
            current_price=Decimal("100"),
        )
        self.provider.positions_live = [position]
        self.engine.reconcile()

        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["status"], "unprotected")
        self.assertEqual(trade["execution_profile"], "unknown")
        self.assertEqual([row[1] for row in self._journal_trades()], [])
        monitored = self.engine._monitor_positions(
            datetime(2026, 8, 7, 14, tzinfo=timezone.utc), [position])
        self.assertEqual(monitored["closed"],
                         [{"symbol": "SPY", "reason": "protection_missing"}])
        self.assertEqual(len(self.provider.close_requests), 1)

    def test_rejected_close_retries_once_with_distinct_attempt_id(self):
        self._bind_engine(runtime_name="runtime-close-retry")
        self.provider.positions_live = [Position(
            "SPY", Decimal("2"), "long", avg_entry_price=Decimal("101"),
            current_price=Decimal("100"),
        )]
        self.engine.reconcile()
        now = datetime(2026, 8, 7, 14, tzinfo=timezone.utc)
        self.engine._monitor_positions(now, list(self.provider.positions_live))
        first_id = next(iter(self.provider.close_requests)).client_order_id
        first_order = next(item for item in self.provider.orders_by_id.values()
                           if item.client_order_id == first_id)
        self.provider.set_order(first_order.id, status="rejected")
        self.engine.reconcile()

        self.engine._monitor_positions(now, list(self.provider.positions_live))
        self.assertEqual(len(self.provider.close_requests), 2)
        second_id = self.provider.close_requests[-1].client_order_id
        self.assertNotEqual(first_id, second_id)
        self.engine._monitor_positions(now, list(self.provider.positions_live))
        self.assertEqual(len(self.provider.close_requests), 2)

    def test_reappearing_closed_position_is_unprotected_residual_not_reactivated(self):
        self._bind_engine(runtime_name="runtime-reappearing")
        _, entry = self._submit_stock_entry(client_order_id="entry-reappearing")
        self.provider.set_order(entry.id, status="filled", filled_qty=10,
                                filled_avg_price=101.5)
        position = Position("SPY", Decimal("10"), "long",
                            avg_entry_price=Decimal("101.5"),
                            current_price=Decimal("98.5"))
        self.provider.positions_live = [position]
        self.engine.reconcile()
        now = datetime(2026, 8, 7, 14, tzinfo=timezone.utc)
        self.engine._monitor_positions(now, [position])
        first_close = self.provider.close_requests[-1].client_order_id
        close_order = next(item for item in self.provider.orders_by_id.values()
                           if item.client_order_id == first_close)
        self.provider.set_order(close_order.id, status="filled", filled_qty=10,
                                filled_avg_price=99)
        self.provider.positions_live = []
        self.engine.reconcile()
        self.assertTrue(state.load_state()["orders"][entry.id]["position_closed"])
        self.assertEqual([row[1] for row in self._journal_trades()], ["open", "close"])

        # A later broker position is real exposure, but it is not evidence that
        # the completed entry should be reactivated.
        residual = replace(position, current_price=Decimal("100"))
        self.provider.positions_live = [residual]
        self.engine.reconcile()
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["status"], "unprotected")
        self.assertEqual(trade["execution_profile"], "unknown")
        self.assertEqual([row[1] for row in self._journal_trades()], ["open", "close"])
        self.engine._monitor_positions(now, [residual])
        self.assertEqual(len(self.provider.close_requests), 2)
        self.assertNotEqual(first_close,
                            self.provider.close_requests[-1].client_order_id)
        self.assertEqual(sum(row[4] or 0 for row in self._journal_trades()),
                         Decimal("-25"))

    def test_protective_cancel_requires_broker_terminal_confirmation(self):
        self._bind_engine(runtime_name="runtime-protection-confirmation")

        class StaleCancelProvider(LifecycleProvider):
            def orders(self, **_):
                return [Order("protect-leg", "SPY", Decimal("10"), "sell",
                               "accepted", "limit", "day")]

        provider = StaleCancelProvider()
        provider.orders_by_id["protect-leg"] = Order(
            "protect-leg", "SPY", Decimal("10"), "sell", "accepted",
            "limit", "day")
        self.engine.provider = provider
        leg = {"order_id": "protect-leg", "role": "target", "status": "accepted"}
        self.assertFalse(self.engine._cancel_protective_legs("SPY", [leg]))
        self.assertEqual(leg["status"], "accepted")

    def test_close_journal_replay_is_idempotent_after_state_removal_crash(self):
        self._bind_engine(runtime_name="runtime-close-replay")
        _, entry = self._submit_stock_entry(client_order_id="entry-close-replay")
        self.provider.set_order(entry.id, status="filled", filled_qty=10,
                                filled_avg_price=101.5)
        position = Position("SPY", Decimal("10"), "long",
                            avg_entry_price=Decimal("101.5"),
                            current_price=Decimal("98"))
        self.provider.positions_live = [position]
        self.engine.reconcile()
        self.engine._monitor_positions(
            datetime(2026, 8, 7, 14, tzinfo=timezone.utc), [position])
        close = self.provider.close_requests[-1]
        close_order = next(item for item in self.provider.orders_by_id.values()
                           if item.client_order_id == close.client_order_id)
        self.provider.set_order(close_order.id, status="filled", filled_qty=10,
                                filled_avg_price=99)
        self.provider.positions_live = []
        with mock.patch.object(state, "update_state",
                               side_effect=OSError("crash after journal commit")):
            with self.assertRaises(OSError):
                self.engine.reconcile()
        self.assertEqual([row[1] for row in self._journal_trades()],
                         ["open", "close"])

        self.engine.reconcile()
        self.assertEqual([row[1] for row in self._journal_trades()],
                         ["open", "close"])
        self.assertEqual(state.load_state()["active_trades"], {})

    def test_edge_proof_identity_survives_fill_and_close_outbox(self):
        self._bind_engine(runtime_name="runtime-proof-epoch")
        request = OrderRequest("SPY", Decimal("10"), "buy",
                               client_order_id="entry-proof-epoch")
        order = self.provider.submit_order(request)
        plan = {
            "execution_profile": "shares", "direction": "long",
            "entry_price": 101.5, "stop_price": 99, "target_price": 105,
            "underlying_stop_price": 99, "underlying_target_price": 105,
            "underlying_symbol": "SPY", "contract_multiplier": Decimal("1"),
            "setup_id": "proof-epoch-setup", "setup_type": "ibr",
            "variant_id": "ibr.target.1_5r", "candidate_id": "candidate-old",
            "proof_run_id": "shadow-run-old", "risk_usd": 100, "notional": 1000,
        }
        self.engine._record_open_order(request, order, plan)
        self.provider.set_order(order.id, status="filled", filled_qty=10,
                                filled_avg_price=101.5)
        self.provider.positions_live = [Position(
            "SPY", Decimal("10"), "long", avg_entry_price=Decimal("101.5"),
            current_price=Decimal("101"))]
        self.engine.reconcile()
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["candidate_id"], "candidate-old")
        self.assertEqual(trade["proof_run_id"], "shadow-run-old")

        self.engine._record_edge_outcome(trade, -25.0, -2.5, 99.0)
        queued = self.engine._pending_edge_outcomes[-1]
        self.assertEqual(queued["outcome"]["candidate_id"], "candidate-old")
        self.assertEqual(queued["outcome"]["proof_run_id"], "shadow-run-old")
        self.assertTrue(queued["entry_id"].endswith("\0shadow-run-old"))

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

        # The option profile also rests a take-profit order, so identify the
        # close by the client id the monitor actually submitted it under.
        close_id = next(
            item.id for item in self.provider.orders_by_id.values()
            if item.client_order_id == self.provider.close_requests[0].client_order_id)
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


class EdgeOutboxTests(unittest.TestCase):
    """A booked close must never lose its learning event."""

    VARIANT = "ibr.target.1_5r"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpaca-outbox-")
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

    def _bind_engine(self, runtime_name="runtime"):
        state.RUNTIME_BASE = Path(self.tmp.name) / runtime_name
        self.engine = Engine(_config(self.edge_db), light=True,
                             provider=self.provider)
        state.ensure_ready()

    def _register_candidate(self):
        EdgeLedger(self.edge_db).register_candidate(
            self.VARIANT, vehicle="equity", hypothesis="durable outbox",
            config={"strategy": {"target_r": 1.5}})

    def _close_one_trade(self, client_order_id="entry-outbox"):
        request = OrderRequest("SPY", Decimal("10"), "buy",
                               client_order_id=client_order_id)
        order = self.provider.submit_order(request)
        self.engine._record_open_order(request, order, {
            "execution_profile": "shares", "direction": "long",
            "entry_price": 101.5, "stop_price": 99, "target_price": 105,
            "underlying_stop_price": 99, "underlying_target_price": 105,
            "underlying_symbol": "SPY", "contract_multiplier": Decimal("1"),
            "setup_id": client_order_id, "setup_type": "ibr",
            "variant_id": self.VARIANT, "risk_usd": 100, "notional": 1000,
        })
        self.provider.set_order(order.id, status="filled", filled_qty=10,
                                filled_avg_price=101.5)
        self.provider.positions_live = [
            Position("SPY", Decimal("10"), "long",
                     avg_entry_price=Decimal("101.5"),
                     current_price=Decimal("101.5"))]
        self.engine.reconcile()
        self.provider.positions_live = []
        state.update_state(lambda latest: {
            **latest, "active_trades": {
                symbol: {**trade, "closing_price": 99, "closing_reason": "stop"}
                for symbol, trade in latest["active_trades"].items()}})
        self.engine._runtime_state = state.load_state()
        self.engine.reconcile()

    def _outcomes(self):
        with closing(sqlite3.connect(self.edge_db)) as db:
            return db.execute(
                "SELECT opportunity_id FROM paper_outcomes ORDER BY created_at"
            ).fetchall()

    def test_outcome_survives_a_ledger_failure_and_drains_on_a_later_cycle(self):
        self._bind_engine(runtime_name="runtime-outbox-retry")
        self._register_candidate()
        with mock.patch("agent.edge.record_paper_outcome",
                        side_effect=sqlite3.OperationalError("database is locked")):
            self._close_one_trade()
        queued = state.load_state()["edge_outbox"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["attempts"], 1)
        self.assertEqual(queued[0]["outcome"]["opportunity_id"], "entry-outbox")
        self.assertEqual(self._outcomes(), [])

        self.engine.reconcile()
        self.assertEqual(state.load_state()["edge_outbox"], [])
        self.assertEqual([row[0] for row in self._outcomes()], ["entry-outbox"])

    def test_draining_is_idempotent_across_retries_and_a_process_restart(self):
        self._bind_engine(runtime_name="runtime-outbox-idempotent")
        self._register_candidate()
        self._close_one_trade()
        self.assertEqual([row[0] for row in self._outcomes()], ["entry-outbox"])
        durable = state.load_state()
        self.assertEqual(durable["edge_outbox"], [])

        # A crash between the ledger write and the outbox pruning leaves the
        # entry queued; a restarted process must not book it a second time.
        replayed = {"entry_id": f"{self.VARIANT}\0equity\0entry-outbox",
                    "queued_ts": 1.0, "attempts": 1,
                    "outcome": {"variant_id": self.VARIANT, "vehicle": "equity",
                                "opportunity_id": "entry-outbox",
                                "session_date": "2026-01-05", "net_pnl": -25.0,
                                "risk_usd": 100.0, "paper": True}}
        state.update_state(lambda latest: {**latest, "edge_outbox": [replayed]})
        restarted = Engine(_config(self.edge_db), light=True, provider=self.provider)
        self.addCleanup(restarted.close)
        restarted._runtime_state = state.load_state()
        for _ in range(3):
            restarted._drain_edge_outbox()
        self.assertEqual([row[0] for row in self._outcomes()], ["entry-outbox"])
        self.assertEqual(state.load_state()["edge_outbox"], [])

    def test_unusable_outcome_is_journaled_rather_than_retried_forever(self):
        self._bind_engine(runtime_name="runtime-outbox-rejected")
        state.update_state(lambda latest: {**latest, "edge_outbox": [
            {"entry_id": "missing", "queued_ts": 1.0, "attempts": 0,
             "outcome": {"variant_id": "ibr.unknown", "vehicle": "equity",
                         "opportunity_id": "gone", "net_pnl": 1.0,
                         "risk_usd": 100.0}}]})
        self.engine._runtime_state = state.load_state()
        self.assertEqual(self.engine._drain_edge_outbox(), 0)
        self.assertEqual(state.load_state()["edge_outbox"], [])
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            kinds = [row[0] for row in db.execute("SELECT kind FROM events")]
        self.assertIn("edge_outcome_rejected", kinds)


if __name__ == "__main__":
    unittest.main()
