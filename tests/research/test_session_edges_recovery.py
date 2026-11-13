"""Regression coverage for ambiguous broker outcomes and study authority."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studies.session_edges import live
from studies.session_edges.data import NEW_YORK


class RecoveryBroker:
    mode = "paper"
    paper = True

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 10, 30, 5, tzinfo=NEW_YORK)
        self.held = 10
        self.sent = []
        self.records = {}
        self.visible = True
        self.submit_error = None
        self.bars_calls = 0

    def clock(self):
        return SimpleNamespace(timestamp=self.now, is_open=True,
                               next_close=self.now.replace(hour=16, minute=0, second=0))

    def account(self):
        return SimpleNamespace(equity=Decimal("100000"), status="active")

    def positions(self):
        return [] if not self.held else [SimpleNamespace(
            symbol="QQQ", qty=Decimal(abs(self.held)), side="long" if self.held > 0 else "short")]

    def submit_order(self, request):
        self.sent.append(request)
        if self.submit_error is not None and not isinstance(self.submit_error, TimeoutError):
            raise self.submit_error
        order = SimpleNamespace(id=f"order-{len(self.sent)}", symbol=request.symbol,
                                client_order_id=request.client_order_id, status="accepted",
                                filled_qty=Decimal(0), filled_avg_price=None)
        self.records[request.client_order_id] = order
        if self.submit_error is not None:
            error, self.submit_error = self.submit_error, None
            raise error
        return order

    def orders(self, *, status=None, client_order_id=None):
        if not self.visible:
            return []
        if client_order_id is not None:
            row = self.records.get(client_order_id)
            return [] if row is None else [row]
        return [row for row in self.records.values() if status != "open" or row.status == "accepted"]

    def cancel_order(self, order_id):
        for order in self.records.values():
            if order.id == order_id:
                order.status = "canceled"

    def bars(self, *args, **kwargs):
        self.bars_calls += 1
        raise RuntimeError("market data unavailable")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.broker = RecoveryBroker()

    def seed_state(self, *, shares=10, pending=None):
        payload = {"schema": live.SCHEMA, "day": None, "legs": {}, "carry": {},
                   "order_seq": 0, "orders": {f"{live.NOISE}:QQQ": {
                       "shares": shares, "desired": shares, "pending": pending,
                       "attempts": 0, "disabled": None, "quote": None}}}
        (self.directory / "state.json").write_text(json.dumps(payload), encoding="utf-8")

    def runner(self):
        return live.ShadowRunner(self.broker, directory=self.directory, orders=live.ORDERS_PAPER,
                                 recovery_only=True)

    def test_accepted_timeout_keeps_same_intent_through_absence_and_restart(self):
        self.seed_state()
        self.broker.submit_error = TimeoutError("accepted but response lost")
        runner = self.runner()
        runner.step()
        intent = runner.state["orders"][f"{live.NOISE}:QQQ"]["pending"]
        original_id = intent["client_order_id"]
        self.assertTrue(intent["ambiguous"])
        self.assertEqual(len(self.broker.sent), 1)
        self.broker.visible = False
        self.broker.now += timedelta(seconds=90)
        runner.step()
        restarted = self.runner()
        restarted.step()
        self.assertEqual(len(self.broker.sent), 1)
        self.assertEqual(restarted.state["orders"][f"{live.NOISE}:QQQ"]["pending"]
                         ["client_order_id"], original_id)
        self.broker.visible = True
        order = self.broker.records[original_id]
        order.status, order.filled_qty, order.filled_avg_price = "filled", Decimal(10), Decimal(100)
        self.broker.held = 0
        restarted.step()
        book = restarted.state["orders"][f"{live.NOISE}:QQQ"]
        self.assertEqual(book["shares"], 0)
        self.assertIsNone(book["pending"])
        self.assertEqual(len(self.broker.sent), 1)

    def test_definite_rejection_can_retry_and_uses_existing_attempt_limit(self):
        class Rejected(Exception):
            status_code = 422

        self.seed_state()
        self.broker.submit_error = Rejected("broker refused")
        runner = self.runner()
        for _ in range(live.MAX_FLATTEN_ATTEMPTS + 2):
            runner.step()
        book = runner.state["orders"][f"{live.NOISE}:QQQ"]
        self.assertIsNone(book["pending"])
        self.assertEqual(len(self.broker.sent), live.MAX_FLATTEN_ATTEMPTS)
        self.assertEqual(book["disabled"], "exit_failed")
        failures = [row for row in live.read_journal(runner.journal_path) if row["kind"] == "order_failed"]
        self.assertTrue(all(row["failure_kind"] == "rejected" for row in failures))

    def test_recovery_closes_owned_shares_without_any_market_data(self):
        self.seed_state()
        (self.directory / live.FLATTEN_FILE).write_text("operator flatten", encoding="utf-8")
        runner = self.runner()
        runner.step()
        self.assertEqual(self.broker.bars_calls, 0)
        self.assertEqual([(row.side, int(row.qty)) for row in self.broker.sent], [("sell", 10)])

    def test_pending_reduction_is_worked_when_signal_data_raises(self):
        self.seed_state()
        runner = self.runner()
        # Exercise an already-running legacy signal loop. Public construction
        # cannot authorize this posture; reductions must still survive its errors.
        runner.recovery_only = False
        (self.directory / live.FLATTEN_FILE).write_text("operator flatten", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "market data unavailable"):
            runner.step()
        self.assertEqual([(row.side, int(row.qty)) for row in self.broker.sent], [("sell", 10)])

    def test_recovery_cannot_open_or_increase_a_position(self):
        self.seed_state()
        runner = self.runner()
        leg = runner.legs[0]
        runner._want(leg, self.broker.now, 20, 100)
        runner._align(leg, self.broker.now, self.broker.now.date())
        self.assertFalse(self.broker.sent)
        self.assertEqual(runner.state["orders"][leg.key]["desired"], 10)

    def test_recovery_preflight_works_during_market_data_outage_and_halt(self):
        self.seed_state()
        (self.directory / live.HALT_FILE).write_text("halt entries", encoding="utf-8")
        result = live.preflight(self.broker, (), orders=live.ORDERS_PAPER,
                                directory=self.directory, recovery_only=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.broker.bars_calls, 0)

    def test_recovery_refuses_foreign_shares(self):
        self.seed_state()
        self.broker.held = 20
        result = live.preflight(self.broker, (), orders=live.ORDERS_PAPER,
                                directory=self.directory, recovery_only=True)
        self.assertFalse(result["ok"])
        self.assertFalse(self.broker.sent)

    def test_reversed_broker_position_never_receives_a_local_reduction(self):
        self.seed_state()
        self.broker.held = -5
        runner = self.runner()
        runner.step()
        self.assertFalse(self.broker.sent)
        self.assertEqual(runner.state["orders"][runner.legs[0].key]["disabled"], "foreign_position")

    def test_recovery_requires_existing_owned_state(self):
        with self.assertRaisesRegex(ValueError, "saved owned shares or pending"):
            self.runner()


class StudyAuthorityTests(unittest.TestCase):
    def test_missing_main_proof_disables_order_construction(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "main proof/parity"):
                live.ShadowRunner(RecoveryBroker(), directory=folder, orders=live.ORDERS_PAPER)

    def test_book_tiers_and_forged_proof_do_not_enable_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "edge_book.json"
            for schema in (live.LEGACY_BOOK_SCHEMA, live.BOOK_SCHEMA):
                source.write_text(json.dumps({"schema": schema, "authorizing": True,
                    "proof": {"verified": True}, "legs": [{"rule": live.NOISE, "symbol": "QQQ",
                        "trade": True, "weight": 1, "tier": "confirmed"}]}), encoding="utf-8")
                legs = live.load_book(source)
                self.assertFalse(any(leg.trade for leg in legs))
                with self.assertRaisesRegex(RuntimeError, "main proof/parity"):
                    live.ShadowRunner(RecoveryBroker(), directory=folder, legs=legs,
                                      orders=live.ORDERS_PAPER)
                result = live.preflight(RecoveryBroker(), legs, orders=live.ORDERS_PAPER,
                                        directory=folder)
                self.assertFalse(result["ok"])

    def test_shadow_and_cli_refusal_do_not_require_broker_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = live.ShadowRunner(RecoveryBroker(), directory=folder)
            self.assertEqual(runner.orders, live.ORDERS_NONE)
            self.assertFalse(any(leg.trade for leg in live.DEFAULT_LEGS))
            with patch.object(live, "build_provider") as provider:
                self.assertEqual(live.main(["run", "--paper-orders", "--dir", folder]), 2)
                provider.assert_not_called()


class ShadowEvidenceTests(unittest.TestCase):
    def test_only_current_non_crossed_timestamped_quotes_are_measured_prices(self):
        broker = RecoveryBroker()
        with tempfile.TemporaryDirectory() as folder:
            runner = live.ShadowRunner(broker, directory=folder)
            for quote in (
                SimpleNamespace(timestamp=broker.now - timedelta(seconds=31), bid=99, ask=101),
                SimpleNamespace(timestamp=broker.now + timedelta(seconds=1), bid=99, ask=101),
                SimpleNamespace(timestamp=broker.now, bid=102, ask=101),
                SimpleNamespace(bid=99, ask=101),
            ):
                broker.quotes = lambda *args, **kwargs: {"QQQ": [quote]}
                self.assertEqual(runner._quote("QQQ", broker.now, live.LONG, 100), (100, "bar_fallback"))
            broker.quotes = lambda *args, **kwargs: {"QQQ": [SimpleNamespace(
                timestamp=broker.now, bid=99, ask=101)]}
            self.assertEqual(runner._quote("QQQ", broker.now, live.LONG, 100), (101, "quote"))
            self.assertEqual(runner._quote("QQQ", broker.now, live.SHORT, 100), (99, "quote"))

    def test_fallback_and_legacy_prices_are_excluded_from_forward_statistics(self):
        key = f"{live.NOISE}:QQQ"
        events = []
        for index, qualification in enumerate((True, False, None)):
            row = {"shadow_gross": 0.01, "shadow_round_trips": 1}
            if qualification is not None:
                row["pricing_qualified"] = qualification
            events.append({"kind": "session_summary", "full_session": True,
                           "day": f"2026-11-{index + 10}", "legs": {key: row}})
        result = live.forward_report(events)
        self.assertFalse(result["authorizing"])
        self.assertEqual(result["legs"][key]["sessions"], 1)
        self.assertEqual(result["legs"][key]["unpriced_sessions_excluded"], 2)
        self.assertEqual(result["legs"][key]["cumulative_pct"], 1)


if __name__ == "__main__":
    unittest.main()
