"""Independent execution and recovery fault regressions.

These cases exercise durable boundaries that ordinary happy-path lifecycle
tests do not cross: a process restart after a close journal commit, a retry
close with explicit whole-trade costs, and watchdog/supervisor lock handoff
when broker or status persistence fails.
"""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent import state
from agent.alpaca_domain import OrderRequest, Position
from agent.engine import Engine
from deploy import trader_supervisor as supervisor
from deploy import watchdog
from research.edge_ledger import EdgeLedger

from tests.test_execution_lifecycle import LifecycleProvider, _config


class ExecutionRestartFaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpaca-execution-fault-")
        self.root = Path(self.tmp.name)
        self.edge_db = self.root / "edge.sqlite3"
        self.provider = LifecycleProvider()
        self.old_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = self.root / "runtime"
        self.engine = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.engine is not None:
            self.engine.close()
        state.RUNTIME_BASE = self.old_runtime_base
        state.configure_runtime("paper")
        self.tmp.cleanup()

    def bind(self):
        self.engine = Engine(_config(self.edge_db), light=True,
                             provider=self.provider)
        state.ensure_ready()

    def open_position_and_submit_close(self, *, variant_id=None,
                                       candidate_id=None):
        self.bind()
        request = OrderRequest("SPY", Decimal("10"), "buy",
                               client_order_id="entry-fault")
        entry = self.provider.submit_order(request)
        plan = {
            "execution_profile": "shares", "direction": "long",
            "entry_price": 100, "stop_price": 99, "target_price": 105,
            "underlying_stop_price": 99, "underlying_target_price": 105,
            "underlying_symbol": "SPY", "contract_multiplier": 1,
            "setup_id": "entry-fault", "setup_type": "ibr",
            "risk_usd": 10, "notional": 1000,
        }
        if variant_id is not None:
            plan.update(variant_id=variant_id, candidate_id=candidate_id)
        self.engine._record_open_order(request, entry, plan)
        self.provider.set_order(entry.id, status="filled", filled_qty=10,
                                filled_avg_price=100)
        position = Position("SPY", Decimal("10"), "long",
                            avg_entry_price=Decimal("100"),
                            current_price=Decimal("98"))
        self.provider.positions_live = [position]
        self.engine.reconcile()
        self.engine._monitor_positions(
            datetime(2026, 8, 7, 14, tzinfo=timezone.utc), [position])
        close_request = self.provider.close_requests[-1]
        close_order = next(order for order in self.provider.orders_by_id.values()
                           if order.client_order_id == close_request.client_order_id)
        return entry, position, close_order

    def close_rows(self):
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            return db.execute(
                "SELECT action, qty, price, fees, net_pnl, trade_id "
                "FROM trades ORDER BY id").fetchall()

    def test_partial_close_retry_keeps_costs_and_journal_once_after_restart(self):
        _, _, first_close = self.open_position_and_submit_close()

        # Fees/slippage are whole-entry telemetry.  The two close attempts
        # must each carry only their proportional share.
        state.update_state(lambda current: {
            **current,
            "active_trades": {
                symbol: {**trade, "fees": 1.0, "slippage": 2.0}
                for symbol, trade in current["active_trades"].items()
            },
        })
        self.engine._runtime_state = state.load_state()
        self.provider.set_order(first_close.id, status="canceled",
                                filled_qty=4, filled_avg_price=101)
        self.provider.positions_live = [Position(
            "SPY", Decimal("6"), "long", avg_entry_price=Decimal("100"),
            current_price=Decimal("98"))]
        self.engine.reconcile()
        self.assertEqual([row[1] for row in self.close_rows()
                          if row[0] == "close"], [4.0])

        # Restart with the same durable state and broker snapshot.  Replaying
        # the terminal first attempt must not append it a second time.
        self.engine.close()
        self.engine = Engine(_config(self.edge_db), light=True,
                             provider=self.provider)
        self.engine.reconcile()
        self.assertEqual([row[1] for row in self.close_rows()
                          if row[0] == "close"], [4.0])

        self.engine._monitor_positions(
            datetime(2026, 8, 7, 14, tzinfo=timezone.utc),
            list(self.provider.positions_live))
        retry_request = self.provider.close_requests[-1]
        retry_order = next(order for order in self.provider.orders_by_id.values()
                           if order.client_order_id == retry_request.client_order_id)
        self.provider.set_order(retry_order.id, status="filled",
                                filled_qty=6, filled_avg_price=102)
        self.provider.positions_live = []
        self.engine.reconcile()

        rows = self.close_rows()
        close_rows = [row for row in rows if row[0] == "close"]
        self.assertEqual([row[1] for row in close_rows], [4.0, 6.0])
        self.assertAlmostEqual(sum(row[3] for row in close_rows), 1.0)
        self.assertAlmostEqual(sum(row[4] for row in close_rows), 15.0)
        self.assertEqual(state.load_state()["active_trades"], {})

    def test_close_journal_and_outcome_are_idempotent_across_process_restart(self):
        candidate = EdgeLedger(self.edge_db).register_candidate(
            "ibr.target.1_5r", vehicle="equity", hypothesis="restart fault",
            config={"strategy": {"target_r": 1.5}})
        _, _, close_order = self.open_position_and_submit_close(
            variant_id="ibr.target.1_5r", candidate_id=candidate["candidate_id"])
        self.provider.set_order(close_order.id, status="filled", filled_qty=10,
                                filled_avg_price=99)
        self.provider.positions_live = []

        # Simulate a process dying after SQLite has committed the close row but
        # before JSON active-trade removal.  A fresh Engine must replay safely.
        with patch.object(state, "update_state",
                          side_effect=OSError("crash after close journal")):
            with self.assertRaises(OSError):
                self.engine.reconcile()
        self.engine.close()
        self.engine = Engine(_config(self.edge_db), light=True,
                             provider=self.provider)
        self.engine.reconcile()
        self.engine.reconcile()

        rows = self.close_rows()
        self.assertEqual([row[0] for row in rows], ["open", "close"])
        with closing(sqlite3.connect(self.edge_db)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM paper_outcomes").fetchone()[0], 1)
        self.assertEqual(state.load_state()["active_trades"], {})


class HandoverFaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpaca-handover-fault-")
        self.old_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.tmp.name)
        state.configure_runtime("paper")
        state.ensure_ready()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        state.RUNTIME_BASE = self.old_runtime_base
        state.configure_runtime("paper")
        self.tmp.cleanup()

    @staticmethod
    def cfg():
        return {"mode": "paper", "broker": {"paper": True}}

    def test_terminated_child_network_failure_pauses_and_releases_lock(self):
        provider = Mock()
        provider.session.api_key = "paper-key"
        provider.account.side_effect = RuntimeError("broker offline")
        with self.assertRaises(watchdog.WatchdogError):
            watchdog.run_once(self.cfg(), provider, max_age=300,
                              terminated_child=True)
        runtime = state.load_state()
        self.assertTrue(runtime["operator_pause"])
        self.assertEqual(runtime["state"], state.PAUSED)
        handle = state.acquire_run_lock()
        self.assertIsNotNone(handle)
        state.release_run_lock(handle)

    def test_transferred_lock_is_released_when_flatten_fails(self):
        class Broker:
            paper = True

            class Session:
                api_key = "paper-key"

            session = Session()

            def account(self):
                return {"id": "paper-account"}

            def positions(self):
                return [type("Position", (), {"symbol": "SPY"})()]

        fake_engine = Mock()
        fake_engine.flatten_all.side_effect = RuntimeError("flatten unavailable")
        with patch.object(watchdog, "_engine", return_value=fake_engine):
            with self.assertRaisesRegex(RuntimeError, "flatten unavailable"):
                watchdog.run_once(self.cfg(), Broker(), max_age=300,
                                  terminated_child=True)
        handle = state.acquire_run_lock()
        self.assertIsNotNone(handle)
        state.release_run_lock(handle)

    def test_supervisor_status_failure_reaps_real_child_before_recovery(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True)
        recovered_after_exit = []

        def recover(_config):
            recovered_after_exit.append(child.poll() is not None)
            return {"flattened": False}

        try:
            with patch("main.load_cfg", return_value={"mode": "paper"}), \
                 patch.object(supervisor.state, "configure_runtime"), \
                 patch.object(supervisor, "wait_until_resumed", return_value=True), \
                 patch.object(supervisor.subprocess, "Popen", return_value=child), \
                 patch.object(supervisor, "monitor", return_value="unresponsive"), \
                 patch.object(supervisor, "write_status",
                              side_effect=OSError("status disk full")), \
                 patch.object(supervisor, "recover", side_effect=recover):
                self.assertEqual(supervisor.supervise(
                    "test.json", grace=1.0), 1)
            self.assertEqual(recovered_after_exit, [True])
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                supervisor.terminate_child(child, grace=1.0)


if __name__ == "__main__":
    unittest.main()
