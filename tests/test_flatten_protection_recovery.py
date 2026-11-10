"""Focused checks for failed-flatten equity stop recovery."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import state
from agent.alpaca_domain import Order, OrderRequest, Position
from agent.alpaca_provider import AlpacaProvider, IdempotencyConflict
from agent.alpaca_session import AlpacaSession
from agent.flatten_protection import restore_flatten_protection
from agent.engine import Engine
from deploy import health
from tests.test_runtime_safety import FakeProvider, _cfg


class RecoveryProvider:
    paper = True

    def __init__(self, positions, orders=()):
        self.positions_live = list(positions)
        self.orders_live = list(orders)
        self.restore_calls = []
        self.submitted = {}

    def positions(self):
        return list(self.positions_live)

    def orders(self, **_kwargs):
        return list(self.orders_live)

    def restore_equity_stop(self, symbol, *, qty, stop_price, client_order_id):
        self.restore_calls.append((symbol, qty, stop_price, client_order_id))
        if client_order_id in self.submitted:
            return self.submitted[client_order_id]
        position = next(item for item in self.positions_live
                        if item.symbol == symbol)
        side = "sell" if position.side == "long" else "buy"
        order = Order(
            f"stop-{len(self.submitted) + 1}", symbol, qty, side, "accepted",
            "stop", "day", client_order_id=client_order_id,
            raw={"stop_price": stop_price},
        )
        self.submitted[client_order_id] = order
        return order


class FilledRecoveryProvider(RecoveryProvider):
    def restore_equity_stop(self, symbol, *, qty, stop_price, client_order_id):
        self.restore_calls.append((symbol, qty, stop_price, client_order_id))
        return Order(
            "stop-filled", symbol, qty,
            "sell" if self.positions_live[0].side == "long" else "buy",
            "filled", "stop", "day", client_order_id=client_order_id,
            filled_qty=qty, filled_avg_price=stop_price,
            raw={"stop_price": stop_price},
        )


class PartialRecoveryProvider(RecoveryProvider):
    def restore_equity_stop(self, symbol, *, qty, stop_price, client_order_id):
        self.restore_calls.append((symbol, qty, stop_price, client_order_id))
        filled_qty = Decimal("0.5")
        return Order(
            "stop-partial", symbol, qty,
            "sell" if self.positions_live[0].side == "long" else "buy",
            "partially_filled", "stop", "day", client_order_id=client_order_id,
            filled_qty=filled_qty, filled_avg_price=Decimal("99"),
            raw={"stop_price": stop_price},
        )


class StatusRecoveryProvider(RecoveryProvider):
    def __init__(self, positions, orders=(), status="accepted"):
        super().__init__(positions, orders)
        self.restore_status = status

    def restore_equity_stop(self, symbol, *, qty, stop_price, client_order_id):
        self.restore_calls.append((symbol, qty, stop_price, client_order_id))
        return Order(
            "fresh-stop", symbol, qty,
            "sell" if self.positions_live[0].side == "long" else "buy",
            self.restore_status, "stop", "day", client_order_id=client_order_id,
            raw={"stop_price": stop_price},
        )


class TradingStopFake:
    def __init__(self, positions=(), orders=()):
        self.positions_rows = list(positions)
        self.orders_rows = list(orders)
        self.submitted_requests = []

    def get_all_positions(self):
        return list(self.positions_rows)

    def get_orders(self, **_kwargs):
        return list(self.orders_rows)

    def submit_order(self, request):
        self.submitted_requests.append(request)
        row = {
            "id": f"stop-{len(self.submitted_requests)}", "symbol": request.symbol,
            "asset_class": "us_equity", "qty": str(request.qty),
            "side": request.side, "status": "accepted", "type": "stop",
            "time_in_force": "day", "stop_price": str(request.stop_price),
            "client_order_id": request.client_order_id,
        }
        self.orders_rows.append(row)
        return row


class RecoveryEngine:
    def __init__(self, provider):
        self.provider = provider
        self.events = []

    def _client_id(self, action, signal, attempt=0):
        return f"{action}-{signal['symbol'].lower()}-{signal['setup_id']}-{attempt}"

    def _event(self, kind, payload):
        self.events.append((kind, payload))


def _close(symbol="SPY", side="sell", status="rejected"):
    return Order(f"close-{status}", symbol, Decimal("2"), side, status,
                 "market", "day", client_order_id=f"close-{status}")


class FlattenProtectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="flatten-protection-")
        self.original_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.tmp.name)
        state.configure_runtime("paper")
        state.ensure_ready()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        state.RUNTIME_BASE = self.original_base
        state.configure_runtime("paper")
        self.tmp.cleanup()

    @staticmethod
    def trade(**updates):
        row = {
            "symbol": "SPY", "execution_profile": "shares", "vehicle": "equity",
            "direction": "long", "position_side": "long", "qty": "2",
            "entry_price": 100, "stop_price": 99, "active_stop_price": 99,
            "order_id": "entry-1", "lifecycle_id": "entry:entry-1",
            "opened_at": 1_700_000_000.0, "status": "open",
            "position_confirmed": True,
        }
        row.update(updates)
        return row

    def seed(self, trade, *, flatten_status="rejected", flatten_attempt=0):
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": trade},
            "protection": {"SPY": {
                "stop_price": trade.get("stop_price"),
                "active_stop_price": trade.get("active_stop_price"),
                "protective_legs": [],
            }},
            "orders": {"close-row": {
                "symbol": "SPY", "action": "flatten", "status": flatten_status,
                "attempt": flatten_attempt, "lifecycle_id": "entry:entry-1",
            }},
        })

    def test_rejected_close_restores_and_attaches_both_durable_legs(self):
        provider = RecoveryProvider([
            Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        ], [_close()])
        engine = RecoveryEngine(provider)
        self.seed(self.trade())

        result = restore_flatten_protection(
            engine, provider.positions(), provider.orders())

        self.assertEqual(len(result["restored"]), 1)
        self.assertFalse(result["errors"])
        self.assertEqual(len(provider.submitted), 1)
        runtime = state.load_state()
        self.assertEqual(len(runtime["active_trades"]["SPY"]["protective_legs"]), 1)
        self.assertEqual(len(runtime["protection"]["SPY"]["protective_legs"]), 1)
        self.assertEqual(runtime["active_trades"]["SPY"]["protective_legs"][0]["side"], "sell")

    def test_engine_failed_flatten_restores_stop_and_reports_residual_risk(self):
        class RejectedCloseProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.history = {
                    "original-stop": Order("original-stop", "SPY", Decimal("2"),
                                           "sell", "accepted", "stop", "day",
                                           raw={"stop_price": "99"}),
                    "original-target": Order("original-target", "SPY", Decimal("2"),
                                             "sell", "accepted", "limit", "day"),
                }
                self.restored = []

            def orders(self, **_):
                return list(self.history.values())

            def cancel_order(self, order_id):
                self.history[order_id] = replace(self.history[order_id], status="canceled")

            def submit_order(self, request):
                self.orders_sent.append(request)
                order_id = f"rejected-{len(self.orders_sent)}"
                order = Order(order_id, request.symbol, request.qty, request.side,
                              "rejected", request.type, "day",
                              client_order_id=request.client_order_id)
                self.history[order_id] = order
                return order

            def restore_equity_stop(self, symbol, *, qty, stop_price, client_order_id):
                order = Order("restored-stop", symbol, qty, "sell", "accepted",
                              "stop", "day", client_order_id=client_order_id,
                              raw={"stop_price": stop_price})
                self.restored.append(order)
                self.history[order.id] = order
                return order

        provider = RejectedCloseProvider()
        provider.positions_live = [Position(
            "SPY", Decimal("2"), "long", current_price=Decimal("100"),
            avg_entry_price=Decimal("100"), market_value=Decimal("200"))]
        trade = self.trade(target_price=105, protective_legs=[
            {"order_id": "original-stop", "role": "stop", "status": "accepted"},
            {"order_id": "original-target", "role": "target", "status": "accepted"},
        ])
        self.seed(trade)
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        with patch("agent.runtime_control.time.sleep"):
            self.assertFalse(engine.flatten_all("operator"))
        self.assertEqual(len(provider.restored), 1)
        runtime = state.load_state()
        self.assertTrue(runtime["operator_pause"])
        live_stops = [leg for leg in runtime["active_trades"]["SPY"]["protective_legs"]
                      if leg["role"] == "stop" and leg["status"] == "accepted"]
        self.assertEqual([leg["order_id"] for leg in live_stops], ["restored-stop"])
        self.assertEqual(json.loads(state.HEARTBEAT_FILE.read_text())["status"], "degraded")
        self.assertFalse(health.trader(state.HEARTBEAT_FILE, 300)["ok"])

    def test_accepted_or_partial_close_never_rearms(self):
        for status in ("accepted", "partially_filled"):
            with self.subTest(status=status):
                provider = RecoveryProvider([
                    Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
                ], [_close(status=status)])
                engine = RecoveryEngine(provider)
                self.seed(self.trade(), flatten_status=status)
                result = restore_flatten_protection(
                    engine, provider.positions(), provider.orders())
                self.assertFalse(result["restored"])
                self.assertEqual(provider.restore_calls, [])
                self.assertEqual(result["skipped"][0]["reason"], "working_or_uncertain_close")

    def test_durable_flatten_intent_without_broker_order_blocks_recovery(self):
        provider = RecoveryProvider([
            Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        ], [])
        engine = RecoveryEngine(provider)
        self.seed(self.trade())
        state.update_state(lambda current: {
            **current,
            "active_trades": {
                "SPY": {**current["active_trades"]["SPY"]}
            },
            "orders": {"flatten-intent": {
                "order_id": "flatten-intent", "symbol": "SPY",
                "action": "flatten", "status": "intent",
                "lifecycle_id": "entry:entry-1", "attempt": 0,
            }},
        })
        result = restore_flatten_protection(
            engine, provider.positions(), provider.orders())
        self.assertFalse(result["restored"])
        self.assertEqual(result["skipped"][0]["reason"], "working_or_uncertain_close")
        self.assertEqual(provider.restore_calls, [])

    def test_unknown_durable_close_status_blocks_recovery(self):
        provider = RecoveryProvider([
            Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        ], [])
        engine = RecoveryEngine(provider)
        self.seed(self.trade())
        state.update_state(lambda current: {
            **current,
            "orders": {"close-unknown": {
                "order_id": "close-unknown", "symbol": "SPY",
                "action": "close", "status": "broker_settling",
                "lifecycle_id": "entry:entry-1", "attempt": 0,
            }},
        })
        result = restore_flatten_protection(
            engine, provider.positions(), provider.orders())
        self.assertFalse(result["restored"])
        self.assertEqual(result["skipped"][0]["reason"], "working_or_uncertain_close")

    def test_short_position_uses_opposite_buy_stop(self):
        trade = self.trade(
            direction="short", position_side="short", entry_price=100,
            stop_price=101, active_stop_price=101,
        )
        provider = RecoveryProvider([
            Position("SPY", Decimal("2"), "short", current_price=Decimal("99"))
        ], [_close(side="buy")])
        engine = RecoveryEngine(provider)
        self.seed(trade)
        result = restore_flatten_protection(
            engine, provider.positions(), provider.orders())
        self.assertEqual(len(result["restored"]), 1)
        self.assertEqual(provider.restore_calls[0][2], Decimal("101"))
        self.assertEqual(provider.submitted[next(iter(provider.submitted))].side, "buy")

    def test_existing_snapshot_stop_is_fresh_reconciled_by_exact_cid(self):
        position = Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        stale_stop = Order(
            "stale-stop", "SPY", Decimal("2"), "sell", "accepted", "stop", "day",
            client_order_id="original-stop-cid", raw={"stop_price": "99"})
        # The helper receives a stale accepted snapshot, while the provider's
        # own broker view is empty. A direct reassociation would claim safety;
        # the exact-CID provider call must perform the fresh reconciliation.
        provider = StatusRecoveryProvider([position], [], status="new")
        engine = RecoveryEngine(provider)
        self.seed(self.trade())
        result = restore_flatten_protection(
            engine, [position], [_close(), stale_stop])
        self.assertEqual(len(result["restored"]), 1)
        self.assertEqual(provider.restore_calls[0][3], "original-stop-cid")
        self.assertTrue(result["restored"][0]["reassociated"])
        self.assertEqual(result["restored"][0]["status"], "new")

    def test_pending_stop_status_is_conflict_and_never_reassociated(self):
        position = Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        provider = RecoveryProvider([position], [])
        engine = RecoveryEngine(provider)
        self.seed(self.trade())
        for pending in ("pending_cancel", "pending_replace"):
            with self.subTest(status=pending):
                pending_stop = Order(
                    f"{pending}-stop", "SPY", Decimal("2"), "sell", pending,
                    "stop", "day", client_order_id=f"{pending}-cid",
                    raw={"stop_price": "99"})
                result = restore_flatten_protection(
                    engine, [position], [_close(), pending_stop])
                self.assertFalse(result["restored"])
                self.assertEqual(result["skipped"][0]["reason"],
                                 "working_or_conflicting_exit")
        self.assertFalse(provider.restore_calls)

    def test_bracket_child_stop_is_inspected_even_when_parent_is_entry_side(self):
        position = Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        bracket = Order(
            "entry-bracket", "SPY", Decimal("2"), "buy", "accepted", "market", "day",
            legs=({"id": "child-stop", "symbol": "SPY", "qty": Decimal("2"),
                   "side": "sell", "status": "accepted", "type": "stop",
                   "role": "stop", "stop_price": Decimal("99")},))
        provider = RecoveryProvider([position], [])
        engine = RecoveryEngine(provider)
        self.seed(self.trade())
        result = restore_flatten_protection(
            engine, [position], [_close(), bracket])
        self.assertFalse(result["restored"])
        self.assertEqual(result["skipped"][0]["reason"],
                         "working_or_conflicting_exit")
        self.assertFalse(provider.restore_calls)

    def test_options_and_bad_geometry_are_explicitly_skipped(self):
        option = self.trade(execution_profile="options")
        provider = RecoveryProvider([
            Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        ], [_close()])
        engine = RecoveryEngine(provider)
        self.seed(option)
        result = restore_flatten_protection(engine, provider.positions(), provider.orders())
        self.assertEqual(result["skipped"][0]["reason"], "options_use_software_stop")

        bad = self.trade(stop_price=101, active_stop_price=101)
        self.seed(bad)
        result = restore_flatten_protection(engine, provider.positions(), provider.orders())
        self.assertEqual(result["skipped"][0]["reason"], "stop_geometry_unavailable_or_invalid")
        self.assertEqual(provider.restore_calls, [])

    def test_restart_reuses_cid_and_durable_state_without_second_submission(self):
        provider = RecoveryProvider([
            Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        ], [_close()])
        engine = RecoveryEngine(provider)
        self.seed(self.trade())
        with patch("agent.flatten_protection.state.load_state", wraps=state.load_state):
            first = restore_flatten_protection(engine, provider.positions(), provider.orders())
            second = restore_flatten_protection(engine, provider.positions(), provider.orders())
        self.assertEqual(len(first["restored"]), 1)
        self.assertEqual(len(second["restored"]), 1)
        self.assertEqual(provider.restore_calls[0][3], provider.restore_calls[1][3])
        self.assertEqual(len(provider.submitted), 1)
        runtime = state.load_state()
        self.assertEqual(runtime["active_trades"]["SPY"]["protective_legs"][0]["order_id"],
                         runtime["protection"]["SPY"]["protective_legs"][0]["order_id"])

    def test_stop_filled_during_recovery_is_durable_but_remains_residual_risk(self):
        provider = FilledRecoveryProvider([
            Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        ], [_close()])
        engine = RecoveryEngine(provider)
        self.seed(self.trade())
        result = restore_flatten_protection(
            engine, provider.positions(), provider.orders())
        self.assertFalse(result["restored"])
        self.assertEqual(result["errors"][0]["reason"],
                         "restore_stop_filled_during_submit")
        runtime = state.load_state()
        self.assertEqual(runtime["active_trades"]["SPY"]["protective_legs"][0]["status"],
                         "filled")

    def test_stop_partial_fill_is_durable_with_actual_filled_qty(self):
        provider = PartialRecoveryProvider([
            Position("SPY", Decimal("2"), "long", current_price=Decimal("100"))
        ], [_close()])
        engine = RecoveryEngine(provider)
        self.seed(self.trade())
        result = restore_flatten_protection(
            engine, provider.positions(), provider.orders())
        self.assertFalse(result["restored"])
        self.assertEqual(result["errors"][0]["reason"],
                         "restore_stop_partially_filled")
        runtime = state.load_state()
        for bucket in (runtime["active_trades"]["SPY"], runtime["protection"]["SPY"]):
            leg = bucket["protective_legs"][0]
            self.assertEqual(leg["status"], "partially_filled")
            self.assertEqual(leg["filled_qty"], 0.5)

    def test_provider_uses_one_official_stop_request_and_normalizes_it(self):
        trading = TradingStopFake(positions=[{
            "symbol": "SPY", "asset_class": "us_equity", "qty": "2",
            "side": "long",
        }])
        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=trading),
        )
        result = provider.restore_equity_stop(
            "SPY", Decimal("2"), Decimal("99"), "recover-1")
        self.assertEqual(result.type, "stop")
        self.assertEqual(result.client_order_id, "recover-1")
        self.assertEqual(len(trading.submitted_requests), 1)
        request = trading.submitted_requests[0]
        self.assertEqual((request.side, request.time_in_force), ("sell", "day"))
        with self.assertRaises(ValueError):
            OrderRequest("SPY", Decimal("2"), "sell", stop_price=Decimal("99"))

    def test_provider_rejects_terminal_cid_and_conflicting_exit(self):
        terminal = {
            "id": "old", "symbol": "SPY", "asset_class": "us_equity",
            "qty": "2", "side": "sell", "status": "rejected", "type": "market",
            "time_in_force": "day", "client_order_id": "recover-1",
        }
        trading = TradingStopFake(positions=[{
            "symbol": "SPY", "asset_class": "us_equity", "qty": "2",
            "side": "long",
        }], orders=[terminal])
        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=trading),
        )
        with self.assertRaises(IdempotencyConflict):
            provider.restore_equity_stop("SPY", 2, 99, "recover-1")

        trading.orders_rows = [{
            "id": "working", "symbol": "SPY", "asset_class": "us_equity",
            "qty": "2", "side": "sell", "status": "accepted", "type": "market",
            "time_in_force": "day", "client_order_id": "close-1",
        }]
        with self.assertRaisesRegex(Exception, "conflicting live"):
            provider.restore_equity_stop("SPY", 2, 99, "recover-2")
        self.assertEqual(trading.submitted_requests, [])

    def test_provider_rejects_pending_cid_and_pending_conflicting_exit(self):
        pending = {
            "id": "pending", "symbol": "SPY", "asset_class": "us_equity",
            "qty": "2", "side": "sell", "status": "pending_cancel", "type": "stop",
            "time_in_force": "day", "stop_price": "99",
            "client_order_id": "recover-1",
        }
        trading = TradingStopFake(positions=[{
            "symbol": "SPY", "asset_class": "us_equity", "qty": "2",
            "side": "long",
        }], orders=[pending])
        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=trading),
        )
        with self.assertRaises(IdempotencyConflict):
            provider.restore_equity_stop("SPY", 2, 99, "recover-1")

        trading.orders_rows = [{
            "id": "pending-close", "symbol": "SPY", "asset_class": "us_equity",
            "qty": "2", "side": "sell", "status": "pending_replace", "type": "market",
            "time_in_force": "day", "client_order_id": "close-1",
        }]
        with self.assertRaisesRegex(Exception, "conflicting live"):
            provider.restore_equity_stop("SPY", 2, 99, "recover-2")
        self.assertEqual(trading.submitted_requests, [])

    def test_provider_reconciles_same_cid_filled_evidence_without_resubmit(self):
        filled = {
            "id": "stop-old", "symbol": "SPY", "asset_class": "us_equity",
            "qty": "2", "side": "sell", "status": "filled", "type": "stop",
            "time_in_force": "day", "stop_price": "99",
            "filled_qty": "2", "filled_avg_price": "99",
            "client_order_id": "recover-1",
        }
        trading = TradingStopFake(positions=[{
            "symbol": "SPY", "asset_class": "us_equity", "qty": "2",
            "side": "long",
        }], orders=[filled])
        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=trading),
        )
        result = provider.restore_equity_stop("SPY", 2, 99, "recover-1")
        self.assertEqual(result.status, "filled")
        self.assertEqual(trading.submitted_requests, [])

    def test_provider_reconciles_same_cid_partial_evidence_without_resubmit(self):
        partial = {
            "id": "stop-partial", "symbol": "SPY", "asset_class": "us_equity",
            "qty": "2", "side": "sell", "status": "partially_filled", "type": "stop",
            "time_in_force": "day", "stop_price": "99",
            "filled_qty": "0.5", "filled_avg_price": "99",
            "client_order_id": "recover-1",
        }
        trading = TradingStopFake(positions=[{
            "symbol": "SPY", "asset_class": "us_equity", "qty": "2",
            "side": "long",
        }], orders=[partial])
        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=trading),
        )
        result = provider.restore_equity_stop("SPY", 2, 99, "recover-1")
        self.assertEqual(result.status, "partially_filled")
        self.assertEqual(result.filled_qty, Decimal("0.5"))
        self.assertEqual(trading.submitted_requests, [])


if __name__ == "__main__":
    unittest.main()
