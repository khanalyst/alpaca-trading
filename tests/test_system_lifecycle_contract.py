"""One deterministic trade across decision, persistence, and restart safety."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from agent import state
from agent.engine import Engine
from agent.risk import RiskEngine
from tests.helpers import valid_config
from tests.research.test_forward_models import market_row
from tests.test_reconciliation import FakeReconciliationExchange


SYMBOL = "BTC/USDT:USDT"


class DeterministicLifecycleContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        previous_scope = state.RUNTIME_SCOPE
        previous_base = state.RUNTIME.parent

        def restore_runtime():
            state.configure_runtime(previous_scope, base=previous_base)

        self.addCleanup(restore_runtime)
        state.configure_runtime("test", base=Path(self.tmp.name))
        state.set_state(
            state.RUNNING, kill_reason=None, operator_pause=False,
            flatten_on_kill=True)
        state.set_journal_context(
            run_id="run-lifecycle", cycle_id="cycle-entry",
            strategy_id="momentum", strategy_version="phase1-v3",
            prompt_version="deterministic", config_version="cfg-lifecycle",
            code_version="code-lifecycle", variant_id="live",
            strategy_config_version="strategy-cfg-lifecycle")

    @staticmethod
    def _engine(cfg):
        engine = Engine.__new__(Engine)
        engine.cfg = cfg
        engine.strategy_id = "momentum"
        engine.run_id = "run-lifecycle"
        engine.risk = RiskEngine(cfg)
        engine.alerts = Mock()
        engine.ex = FakeReconciliationExchange()
        return engine

    def test_trade_survives_restart_reconciles_outcome_and_obeys_kill(self):
        cfg = valid_config()
        cfg["strategy"]["execution_mode"] = "deterministic"
        snapshot = {SYMBOL: market_row()}
        st = state.load_state()
        engine = self._engine(cfg)

        decisions = engine._deterministic_decisions(snapshot, max_new=1)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["proposal_source"],
                         "deterministic_contract")
        prepared, refusal = engine._prepare_setup_decision(
            decisions[0], snapshot, st)
        self.assertIsNone(refusal)
        self.assertIsNotNone(prepared)
        plan, veto = engine.risk.vet_open(
            prepared, 10_000, [], snapshot, {}, 0, {}, {}, {})
        self.assertIsNone(veto)
        self.assertIsNotNone(plan)
        plan["entry_equity_usd"] = 10_000

        engine.ex.price = Mock(return_value=100.0)
        engine.ex.contracts_for_notional = Mock(return_value=2.0)
        engine.ex.guarded_entry_limit = Mock(return_value={
            "mid": 100.0, "limit_price": 100.1, "spread_pct": 0.02,
        })
        engine.ex.open_position = Mock(return_value={
            "symbol": SYMBOL,
            "order_id": "order-entry-1",
            "client_order_id": "client-entry-1",
            "position_id": "position-1",
            "status": "closed",
            "partial": False,
            "requested": 2.0,
            "filled": 2.0,
            "average": 100.0,
            "position_contracts": 2.0,
            "fee_usd": 0.2,
            "slippage_usd": 0.0,
            "adverse_slippage_usd": 0.0,
            "mark_price": 100.0,
            "liquidation_price": 50.0,
            "protection": {
                "stop_loss": True,
                "take_profit": True,
                "stop_price": 98.85,
                "take_price": 102.3,
            },
        })
        engine._mark_setup_status(st, plan["setup_id"], "attempted")
        self.assertTrue(engine._execute_open(plan, st))
        engine._mark_setup_status(st, plan["setup_id"], "opened")

        persisted = state.load_state()
        trade = persisted["active_trades"][SYMBOL]
        self.assertEqual(trade["position_id"], "position-1")
        self.assertEqual(persisted["protection"][SYMBOL]["contracts"], 2.0)

        state.set_journal_context(cycle_id="cycle-restart")
        restarted = self._engine(cfg)
        live_position = {
            "id": "position-1", "symbol": SYMBOL, "side": "long",
            "contracts": 2.0, "entryPrice": 100.0, "markPrice": 100.0,
            "liquidationPrice": 50.0, "leverage": 2,
            "info": {"posId": "position-1"},
        }
        reconciled = restarted._reconcile_positions(
            [live_position], persisted, startup=True)
        self.assertEqual(reconciled, [live_position])
        self.assertEqual(len(restarted.ex.ensure_calls), 1)
        state.commit(persisted)

        state.set_journal_context(cycle_id="cycle-exit")
        self.assertEqual(
            restarted._reconcile_positions([], persisted, startup=True), [])
        state.commit(persisted)
        self.assertNotIn(SYMBOL, state.load_state()["active_trades"])

        with closing(sqlite3.connect(state.DB_FILE)) as conn:
            trades = conn.execute(
                "SELECT action, trade_id, realized_pnl_usd FROM trades "
                "ORDER BY rowid").fetchall()
            events = {row[0] for row in conn.execute(
                "SELECT kind FROM events").fetchall()}
        self.assertEqual([row[0] for row in trades], ["open", "close"])
        self.assertEqual(trades[0][1], trades[1][1])
        self.assertEqual(trades[1][2], 9.5)
        self.assertTrue({
            "setup_proposed", "order_execution", "reconciled_close",
            "setup_status",
        }.issubset(events))

        killed = state.set_state(state.KILLED, "lifecycle test kill")
        restarted.ex.price = Mock(return_value=100.0)
        self.assertFalse(restarted._execute_open(plan, killed))
        restarted.ex.price.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
