"""Regression coverage for close and flatten lifecycle identity binding."""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent import state
from agent.alpaca_domain import Order, Position
from agent.engine import Engine
from tests.test_runtime_safety import FakeProvider, _cfg


class FlattenProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.close_requests = []
        self.close_orders = []
        self._close_number = 0

    def close_position(self, symbol, qty=None, *, client_order_id=None,
                       order_type="market", time_in_force="day"):
        quantity = qty or abs(self.positions_live[0].qty)
        order = Order(
            f"flatten-close-{self._close_number}", symbol, quantity, "sell",
            "filled", order_type, time_in_force,
            filled_qty=quantity, filled_avg_price=Decimal("100"),
            client_order_id=client_order_id)
        self._close_number += 1
        self.close_requests.append(order)
        self.close_orders.append(order)
        self.positions_live = []
        return order

    def orders(self, **_):
        return list(self.close_orders)


class RetryFlattenProvider(FlattenProvider):
    def __init__(self):
        super().__init__()
        self.failures_remaining = 1
        self.submission_ids = []

    def close_position(self, symbol, qty=None, *, client_order_id=None,
                       order_type="market", time_in_force="day"):
        self.submission_ids.append(client_order_id)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary close submission failure")
        return super().close_position(
            symbol, qty=qty, client_order_id=client_order_id,
            order_type=order_type, time_in_force=time_in_force)


def _trade(entry_order_id: str, *, lifecycle_id: str | None = None) -> dict:
    return {
        "symbol": "SPY", "underlying_symbol": "SPY",
        "execution_profile": "shares", "vehicle": "equity",
        "direction": "long", "position_side": "long", "qty": "2",
        "entry_price": 100.0, "order_id": entry_order_id,
        "lifecycle_id": lifecycle_id or f"entry:{entry_order_id}",
        "opened_at": 1_700_000_000.0, "status": "open",
        "stop_price": 99.0, "target_price": 101.0,
        "position_confirmed": True,
    }


class CloseFlattenSafetyTests(unittest.TestCase):
    def setUp(self):
        self.runtime_tmp = tempfile.TemporaryDirectory(prefix="alpaca-close-flatten-")
        self.original_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.runtime_tmp.name)
        state.configure_runtime("paper")
        state.ensure_ready()
        self.addCleanup(self._cleanup_runtime)

    def _cleanup_runtime(self):
        state.RUNTIME_BASE = self.original_runtime_base
        state.configure_runtime("paper")
        self.runtime_tmp.cleanup()

    def _engine(self, provider):
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        return engine

    def _assert_unconfirmed_flatten_keeps_intent(self, response):
        provider = FlattenProvider()
        provider.positions_live = [Position("SPY", Decimal("2"), "long",
                                            avg_entry_price=Decimal("100"))]
        engine = self._engine(provider)
        state.update_state(lambda current: {
            **current, "active_trades": {"SPY": _trade("entry-unconfirmed")},
        })
        with patch.object(provider, "close_position", return_value=response) as close, \
                patch("agent.runtime_control.time.sleep"):
            self.assertFalse(engine.flatten_all())
        current = state.load_state()
        intents = [row for row in current["orders"].values()
                   if row.get("action") == "flatten"]
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["status"], "intent")
        self.assertTrue(current["operator_pause"])
        self.assertEqual({call.kwargs["client_order_id"] for call in close.call_args_list},
                         {intents[0]["client_order_id"]})
        self.assertTrue(engine.flatten_all())
        self.assertEqual(provider.close_requests[0].client_order_id,
                         intents[0]["client_order_id"])

    def test_none_flatten_response_keeps_retryable_intent(self):
        self._assert_unconfirmed_flatten_keeps_intent(None)

    def test_idless_flatten_response_keeps_retryable_intent(self):
        self._assert_unconfirmed_flatten_keeps_intent(
            SimpleNamespace(id="", status="accepted"))

    def test_unknown_flatten_status_keeps_retryable_intent(self):
        self._assert_unconfirmed_flatten_keeps_intent(
            SimpleNamespace(id="unconfirmed", status="mystery"))

    def test_close_retry_survives_restart_and_keeps_trade_identity(self):
        provider = FakeProvider()
        engine = self._engine(provider)
        position = Position("SPY", Decimal("2"), "long",
                            avg_entry_price=Decimal("100"),
                            current_price=Decimal("98"))
        state.update_state(lambda current: {
            **current, "active_trades": {"SPY": _trade("entry-1")},
        })
        with patch.object(engine, "_cancel_protective_legs", return_value=True):
            engine._monitor_positions(
                datetime(2026, 8, 7, 14, tzinfo=timezone.utc), [position])
        first = provider.orders_sent[-1].client_order_id
        runtime = state.load_state()
        close_id = runtime["active_trades"]["SPY"]["closing_order_id"]
        runtime["orders"][close_id]["status"] = "rejected"
        state.save_state(runtime)

        restarted = self._engine(provider)
        with patch.object(restarted, "_cancel_protective_legs", return_value=True):
            restarted._monitor_positions(
                datetime(2026, 8, 7, 14, 1, tzinfo=timezone.utc), [position])
        second = provider.orders_sent[-1].client_order_id
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith("-1"))
        self.assertIn("close-spy-", first)
        self.assertIn("close-spy-", second)

    def test_flatten_fill_binds_trade_and_journals_once(self):
        provider = FlattenProvider()
        engine = self._engine(provider)
        state.update_state(lambda current: {
            **current, "active_trades": {"SPY": _trade("entry-1")},
        })
        position = Position("SPY", Decimal("2"), "long",
                            avg_entry_price=Decimal("100"),
                            current_price=Decimal("100"))
        provider.positions_live = [position]
        with patch("agent.runtime_control.time.sleep"):
            self.assertTrue(engine.flatten_all("operator"))
            self.assertEqual(len(provider.close_requests), 1)
            # A repeated flatten sees terminal broker evidence and does not
            # send a second close for the already-flat account.
            self.assertTrue(engine.flatten_all("operator"))
        runtime = state.load_state()
        self.assertEqual(runtime["active_trades"], {})
        self.assertEqual(runtime["protection"], {})
        flatten_rows = [row for row in runtime["orders"].values()
                        if row.get("action") == "flatten"]
        self.assertEqual(len(flatten_rows), 1)
        self.assertEqual(flatten_rows[0]["entry_order_id"], "entry-1")
        self.assertEqual(flatten_rows[0]["status"], "filled")
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM trades WHERE action='close'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_day_reopen_and_orphan_flatten_get_new_client_ids(self):
        provider = FlattenProvider()
        engine = self._engine(provider)
        position = Position("SPY", Decimal("2"), "long",
                            avg_entry_price=Decimal("100"),
                            current_price=Decimal("100"))
        state.update_state(lambda current: {
            **current, "active_trades": {"SPY": _trade("entry-1")},
        })
        provider.positions_live = [position]
        with patch("agent.runtime_control.time.sleep"):
            self.assertTrue(engine.flatten_all("operator"))
        first = provider.close_requests[-1].client_order_id

        # A new entry on the same day has a new immutable lifecycle id.
        state.update_state(lambda current: {
            **current, "active_trades": {"SPY": _trade("entry-2")},
        })
        provider.positions_live = [position]
        with patch("agent.runtime_control.time.sleep"):
            self.assertTrue(engine.flatten_all("operator"))
        second = provider.close_requests[-1].client_order_id
        self.assertNotEqual(first, second)

        # A broker-only position is assigned a durable orphan intent, and a
        # later broker-only position gets a fresh generation after flatness.
        state.update_state(lambda current: {**current, "active_trades": {}})
        provider.positions_live = [position]
        with patch("agent.runtime_control.time.sleep"):
            self.assertTrue(engine.flatten_all("operator"))
        orphan_first = provider.close_requests[-1].client_order_id
        provider.positions_live = [position]
        with patch("agent.runtime_control.time.sleep"):
            self.assertTrue(engine.flatten_all("operator"))
        orphan_second = provider.close_requests[-1].client_order_id
        self.assertNotEqual(orphan_first, orphan_second)

    def test_new_orphan_does_not_inherit_fully_filled_old_flatten(self):
        provider = FlattenProvider()
        engine = self._engine(provider)
        old_lifecycle = "orphan:SPY:old-generation"
        old = Order("old-flatten", "SPY", Decimal("2"), "sell", "filled",
                    "market", "day", filled_qty=Decimal("2"),
                    filled_avg_price=Decimal("100"),
                    client_order_id="old-flatten-client")
        provider.close_orders.append(old)
        state.update_state(lambda current: {
            **current,
            "orders": {"old-flatten": {
                "order_id": "old-flatten", "symbol": "SPY",
                "status": "filled", "action": "flatten",
                "client_order_id": "old-flatten-client", "qty": "2",
                "filled_qty": "2", "lifecycle_id": old_lifecycle,
                "attempt": 0,
            }},
        })
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        engine.reconcile()
        recovered = state.load_state()["active_trades"]["SPY"]
        self.assertNotEqual(recovered["lifecycle_id"], old_lifecycle)
        self.assertNotIn("closing_order_id", recovered)

    def test_flatten_does_not_race_a_live_normal_close(self):
        provider = FlattenProvider()
        engine = self._engine(provider)
        live = Order("normal-close", "SPY", Decimal("2"), "sell", "accepted",
                     "market", "day", client_order_id="normal-close-client")
        provider.close_orders.append(live)
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": {
                **_trade("entry-1"), "status": "closing",
                "closing_order_id": "normal-close",
                "closing_reason": "stop", "closing_attempt": 0,
            }},
            "orders": {"normal-close": {
                "order_id": "normal-close", "symbol": "SPY",
                "action": "close", "status": "accepted",
                "client_order_id": "normal-close-client",
                "entry_order_id": "entry-1", "attempt": 0,
            }},
        })
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        with patch("agent.runtime_control.time.sleep"):
            self.assertFalse(engine.flatten_all("operator"))
        self.assertEqual(provider.close_requests, [])

    def test_restart_promotes_live_flatten_intent_by_client_id(self):
        provider = FlattenProvider()
        engine = self._engine(provider)
        lifecycle = "orphan:SPY:restart-generation"
        client_id = engine._client_id(
            "flatten", {"symbol": "SPY", "setup_id": lifecycle}, 0)
        intent_id = f"flatten-intent:SPY:{lifecycle}:0"
        live = Order("broker-flatten", "SPY", Decimal("2"), "sell", "accepted",
                     "market", "day", client_order_id=client_id)
        provider.close_orders.append(live)
        state.update_state(lambda current: {
            **current,
            "orders": {intent_id: {
                "order_id": intent_id, "symbol": "SPY", "status": "intent",
                "client_order_id": client_id, "qty": "2", "action": "flatten",
                "reason": "operator", "lifecycle_id": lifecycle, "attempt": 0,
            }},
        })
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        restarted = self._engine(provider)
        with patch("agent.runtime_control.time.sleep"):
            self.assertFalse(restarted.flatten_all("operator"))
        self.assertEqual(provider.close_requests, [])
        runtime = state.load_state()
        self.assertIn("broker-flatten", runtime["orders"])
        self.assertNotIn(intent_id, runtime["orders"])
        with patch("agent.runtime_control.time.sleep"):
            self.assertFalse(restarted.flatten_all("operator"))
        self.assertEqual(provider.close_requests, [])

    def test_filled_intent_is_promoted_before_empty_position_shortcut(self):
        provider = FlattenProvider()
        engine = self._engine(provider)
        trade = _trade("entry-1")
        lifecycle = trade["lifecycle_id"]
        client_id = engine._client_id(
            "flatten", {"symbol": "SPY", "setup_id": lifecycle}, 0)
        intent_id = f"flatten-intent:SPY:{lifecycle}:0"
        filled = Order("filled-after-crash", "SPY", Decimal("2"), "sell",
                       "filled", "market", "day", filled_qty=Decimal("2"),
                       filled_avg_price=Decimal("98"),
                       client_order_id=client_id)
        provider.close_orders.append(filled)
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": trade},
            "orders": {intent_id: {
                "order_id": intent_id, "symbol": "SPY", "status": "intent",
                "client_order_id": client_id, "qty": "2", "action": "flatten",
                "reason": "operator", "entry_order_id": "entry-1",
                "lifecycle_id": lifecycle, "attempt": 0,
            }},
        })
        restarted = self._engine(provider)
        with patch("agent.runtime_control.time.sleep"):
            self.assertTrue(restarted.flatten_all("operator"))
            self.assertTrue(restarted.flatten_all("operator"))
        runtime = state.load_state()
        self.assertEqual(runtime["active_trades"], {})
        self.assertIn("filled-after-crash", runtime["orders"])
        self.assertNotIn(intent_id, runtime["orders"])
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM trades WHERE action='close'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_terminal_filled_close_does_not_retry_stale_position_snapshot(self):
        provider = FakeProvider()
        engine = self._engine(provider)
        trade = {**_trade("entry-1"), "status": "closing",
                 "closing_order_id": "close-1", "closing_reason": "stop",
                 "closing_attempt": 0}
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": trade},
            "orders": {"close-1": {
                "order_id": "close-1", "symbol": "SPY", "status": "filled",
                "action": "close", "client_order_id": "close-client",
                "entry_order_id": "entry-1", "attempt": 0,
                "filled_qty": "2", "filled_avg_price": "98",
            }},
        })
        position = Position("SPY", Decimal("2"), "long",
                            avg_entry_price=Decimal("100"),
                            current_price=Decimal("98"))
        with patch.object(engine, "_cancel_protective_legs", return_value=True):
            engine._monitor_positions(
                datetime(2026, 8, 7, 14, tzinfo=timezone.utc), [position])
        self.assertEqual(provider.orders_sent, [])

    def test_failed_flatten_submission_retries_same_client_id(self):
        provider = RetryFlattenProvider()
        engine = self._engine(provider)
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        with patch("agent.runtime_control.time.sleep"):
            self.assertTrue(engine.flatten_all("operator"))
        self.assertGreaterEqual(len(provider.submission_ids), 2)
        self.assertEqual(provider.submission_ids[0], provider.submission_ids[1])

    def test_tracked_trade_retries_its_durable_intent_after_timeout(self):
        provider = RetryFlattenProvider()
        engine = self._engine(provider)
        state.update_state(lambda current: {
            **current, "active_trades": {"SPY": _trade("entry-timeout")},
        })
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        with patch("agent.runtime_control.time.sleep"):
            self.assertTrue(engine.flatten_all("operator"))
        self.assertEqual(len(provider.submission_ids), 2)
        self.assertEqual(provider.submission_ids[0], provider.submission_ids[1])
        self.assertEqual(state.load_state()["active_trades"], {})

    def test_flatten_intent_blocks_normal_monitor_close(self):
        provider = RetryFlattenProvider()
        provider.failures_remaining = 100
        engine = self._engine(provider)
        state.update_state(lambda current: {
            **current, "active_trades": {"SPY": _trade("entry-1")},
        })
        position = Position("SPY", Decimal("2"), "long")
        provider.positions_live = [position]
        with patch("agent.runtime_control.time.sleep"):
            self.assertFalse(engine.flatten_all("operator"))
        attempted = len(provider.submission_ids)
        with patch.object(engine, "_cancel_protective_legs", return_value=True):
            engine._monitor_positions(
                datetime(2026, 8, 7, 14, tzinfo=timezone.utc), [position])
        self.assertEqual(len(provider.submission_ids), attempted)
        runtime = state.load_state()
        self.assertTrue(runtime["active_trades"]["SPY"]["closing_order_id"].startswith(
            "flatten-intent:"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
