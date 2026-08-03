import unittest
from unittest.mock import Mock, patch

from agent import state
from agent.engine import Engine, PositionAgeUnknown
from agent.exchange import CredentialError
from tests.helpers import valid_config


class FakeReconciliationExchange:
    def __init__(self):
        self.ensure_calls = []
        self.cancelled = []
        self.x = Mock()
        self.x.market.return_value = {"contractSize": 1}

    @staticmethod
    def closed_position_summary(*args):
        return {
            "price": 105,
            "qty": 2,
            "fee_usd": 0.4,
            "funding_usd": -0.1,
            "realized_pnl_usd": 9.5,
            "status": "position_history",
        }

    def cancel_symbol(self, symbol):
        self.cancelled.append(symbol)

    @staticmethod
    def protection_status(symbol, contracts, side, mark):
        return {
            "stop_loss": False,
            "take_profit": False,
            "stop_price": None,
            "take_price": None,
        }

    def ensure_protection(self, symbol, side, contracts, sl, tp, mark):
        self.ensure_calls.append((symbol, contracts, sl, tp))
        return {
            "stop_loss": True,
            "take_profit": True,
            "stop_price": sl,
            "take_price": tp,
        }

    @staticmethod
    def position_opened_at(pos):
        return 900.0


class PositionReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.engine.ex = FakeReconciliationExchange()
        self.engine.alerts = Mock()

    @staticmethod
    def tracked_state():
        return {
            "opened_at": {"BTC/USDT:USDT": 1000},
            "cooldowns": {},
            "active_trades": {
                "BTC/USDT:USDT": {
                    "trade_id": "trade-1", "direction": "long",
                    "opened_at": 1000, "entry_price": 100,
                    "entry_notional": 200, "qty": 2, "leverage": 2,
                    "entry_fee_usd": 0.2, "risk_usd": 4,
                }
            },
            "protection": {
                "BTC/USDT:USDT": {
                    "side": "long", "contracts": 2,
                    "sl_price": 95, "tp_price": 110,
                }
            },
        }

    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.log_trade")
    def test_disappeared_position_is_closed_with_same_trade_id(
            self, log_trade, log_event):
        st = self.tracked_state()
        positions = self.engine._reconcile_positions([], st, startup=True)
        self.assertEqual(positions, [])
        self.assertEqual(st["active_trades"], {})
        self.assertEqual(log_trade.call_args.kwargs["trade_id"], "trade-1")
        self.assertEqual(log_trade.call_args.kwargs["realized_pnl_usd"], 9.5)
        log_event.assert_called_once()

    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.log_trade")
    def test_reconciled_exit_cancels_stale_protective_orders(
            self, log_trade, log_event):
        st = self.tracked_state()
        self.engine._reconcile_positions([], st, startup=True)
        # Separate fallback/restored SL+TP orders would otherwise survive an
        # exchange-side exit and ambush the next entry in this symbol.
        self.assertEqual(self.engine.ex.cancelled, ["BTC/USDT:USDT"])

    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.log_trade")
    def test_replaced_position_never_loses_its_own_protection(
            self, log_trade, log_event):
        st = self.tracked_state()
        st["active_trades"]["BTC/USDT:USDT"]["position_id"] = "old-pos"
        replacement = {
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 3,
            "entryPrice": 104, "markPrice": 104, "leverage": 2,
            "info": {"posId": "new-pos"},
        }
        # The replacement carries its own verified SL/TP; adoption keeps it.
        self.engine.ex.protection_status = lambda *a: {
            "stop_loss": True, "take_profit": True,
            "stop_price": 99.0, "take_price": 112.0,
        }
        self.engine._reconcile_positions([replacement], st, startup=True)
        # Cancelling here would strip the replacement position's own SL/TP.
        self.assertEqual(self.engine.ex.cancelled, [])

    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.log_trade")
    def test_partial_close_share_is_not_double_counted(
            self, log_trade, log_event):
        st = self.tracked_state()
        st["active_trades"]["BTC/USDT:USDT"][
            "partial_realized_pnl_usd"] = 3.5
        self.engine._reconcile_positions([], st, startup=True)
        # 9.5 total minus the 3.5 already journaled by the partial close.
        self.assertEqual(
            log_trade.call_args.kwargs["realized_pnl_usd"], 6.0)

    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.log_trade")
    def test_direct_final_close_journals_only_the_unrealized_remainder(
            self, log_trade, log_event, commit):
        st = self.tracked_state()
        trade = st["active_trades"]["BTC/USDT:USDT"]
        trade.update({
            "qty": 1,
            "initial_qty": 2,
            "entry_fee_remaining_usd": 0.1,
            "partial_realized_pnl_usd": 3.0,
        })
        self.engine.ex.close_position = Mock(return_value={
            "fully_closed": True,
            "filled": 1,
            "average": 105,
            "fee_usd": 0.2,
            "order_id": "close-1",
            "status": "closed",
            "slippage_usd": -0.05,
            "adverse_slippage_usd": 0.0,
        })
        self.engine._log_order_execution = Mock()
        self.engine._mark_setup_status = Mock()
        position = {
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": 1,
            "entryPrice": 100,
            "markPrice": 105,
            "leverage": 2,
            "info": {"fundingFee": "0"},
        }

        self.assertTrue(self.engine._close(
            position, "test close", st,
            close_trigger="thesis_invalidated",
            close_evidence="entry trend was up; current trend is down"))

        # Final remainder: +5 gross -0.10 remaining entry fee -0.20 exit fee.
        # The prior +3.00 partial row is used only for cumulative PnL/cooldown.
        self.assertAlmostEqual(
            log_trade.call_args.kwargs["realized_pnl_usd"], 4.7)
        self.assertAlmostEqual(log_trade.call_args.kwargs["pnl_pct"], 3.85)
        self.assertEqual(
            log_trade.call_args.kwargs["close_trigger"],
            "thesis_invalidated")
        self.assertIn(
            "current trend is down",
            log_trade.call_args.kwargs["close_evidence"])
        self.assertNotIn("BTC/USDT:USDT", st["active_trades"])

    def test_missing_protection_is_restored_from_durable_target(self):
        st = self.tracked_state()
        position = {
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2,
            "entryPrice": 100, "markPrice": 102, "leverage": 2,
        }
        positions = self.engine._reconcile_positions([position], st, startup=True)
        self.assertEqual(len(positions), 1)
        self.assertEqual(
            self.engine.ex.ensure_calls,
            [("BTC/USDT:USDT", 2.0, 95.0, 110.0)],
        )

    def test_failed_close_of_unprotected_position_stops_the_cycle(self):
        st = self.tracked_state()
        st["protection"]["BTC/USDT:USDT"] = {
            "side": "long", "contracts": 2,
            "sl_price": None, "tp_price": None,
        }
        position = {
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2,
            "entryPrice": 100, "markPrice": 102, "leverage": 2,
        }
        self.engine._close = Mock(return_value=False)
        with self.assertRaisesRegex(RuntimeError, "without verified protection"):
            self.engine._reconcile_positions([position], st, startup=True)

    @patch("agent.engine.state.log_trade")
    def test_unknown_adopted_position_age_pauses_instead_of_resetting_clock(
            self, log_trade):
        self.engine.ex.position_opened_at = Mock(return_value=None)
        self.engine.ex.protection_status = Mock(return_value={
            "stop_loss": True, "take_profit": True,
            "stop_price": 95, "take_price": 110,
        })
        st = {
            "opened_at": {}, "cooldowns": {}, "active_trades": {},
            "protection": {},
        }
        position = {
            "symbol": "ETH/USDT:USDT", "side": "long", "contracts": 2,
            "entryPrice": 100, "markPrice": 101, "leverage": 2,
        }

        with self.assertRaises(PositionAgeUnknown):
            self.engine._reconcile_positions([position], st, startup=True)

        adopted = st["active_trades"]["ETH/USDT:USDT"]
        self.assertFalse(adopted["age_known"])
        self.assertEqual(adopted["opened_at"], 0)
        self.assertNotIn("ETH/USDT:USDT", st["opened_at"])

    @patch("agent.engine.state.log_event")
    def test_reconciliation_closes_position_whose_stop_is_too_near_liquidation(
            self, log_event):
        st = self.tracked_state()
        position = {
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2,
            "entryPrice": 100, "markPrice": 100, "liquidationPrice": 94.5,
            "leverage": 2,
        }
        self.engine._close = Mock(return_value=True)

        positions = self.engine._reconcile_positions(
            [position], st, startup=True)

        self.assertEqual(positions, [])
        self.engine._close.assert_called_once_with(
            position, "reconciliation: unsafe liquidation buffer", st)
        self.assertEqual(log_event.call_args.args[0],
                         "liquidation_buffer_unsafe")


class FakeEmergencyExchange:
    def __init__(self):
        self.x = Mock()
        self.x.market.return_value = {"contractSize": 1}
        self.close_calls = 0

    @staticmethod
    def price(symbol):
        return 100

    @staticmethod
    def contracts_for_notional(symbol, notional, price):
        return 2

    @staticmethod
    def guarded_entry_limit(*args, **kwargs):
        return {"limit_price": 100.25, "spread_pct": 0.05, "mid": 100}

    @staticmethod
    def open_position(*args, **kwargs):
        return {
            "order_id": "entry", "status": "closed", "filled": 2,
            "average": 100, "partial": False, "fee_usd": 0.2,
            "slippage_usd": 0, "position_contracts": 2,
            "position_id": "position-1",
            "protection": {"stop_loss": False, "take_profit": False},
        }

    def close_position(self, pos):
        self.close_calls += 1
        return {
            "order_id": "exit", "status": "closed", "filled": 2,
            "average": 99, "partial": False, "fee_usd": 0.2,
            "slippage_usd": 2, "fully_closed": True,
            "remaining_contracts": 0,
        }

    @staticmethod
    def funding_since(symbol, since_ms):
        return 0

    @staticmethod
    def position(symbol, side=None):
        return None


class PartialEmergencyExchange(FakeEmergencyExchange):
    def close_position(self, pos):
        self.close_calls += 1
        remaining = 1 if self.close_calls == 1 else 0
        return {
            "order_id": f"exit-{self.close_calls}", "status": "closed",
            "filled": 1, "average": 99, "partial": bool(remaining),
            "fee_usd": 0.1, "slippage_usd": 1,
            "fully_closed": not remaining,
            "remaining_contracts": remaining,
        }

    def position(self, symbol, side=None):
        if self.close_calls < 2:
            return {
                "symbol": symbol, "side": side, "contracts": 1,
                "entryPrice": 100, "markPrice": 99, "leverage": 2,
                "info": {},
            }
        return None


class UnsafeLiquidationExchange(FakeEmergencyExchange):
    @staticmethod
    def open_position(*args, **kwargs):
        return {
            "order_id": "entry", "status": "closed", "filled": 2,
            "average": 100, "partial": False, "fee_usd": 0.2,
            "slippage_usd": 0, "position_contracts": 2,
            "position_id": "position-1", "mark_price": 100,
            "liquidation_price": 97.5,
            "protection": {"stop_loss": True, "take_profit": True},
        }


class EmergencyExecutionTests(unittest.TestCase):
    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.log_trade")
    @patch("agent.engine.state.commit")
    def test_post_fill_persistence_accepts_201_to_300_char_entry_reason(
            self, commit, log_trade, log_event):
        # Exercise the same validator used by state.commit while keeping the
        # test's journal writes isolated from the repository runtime.
        commit.side_effect = state._validate
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.ex = FakeEmergencyExchange()
        engine.alerts = Mock()
        st = {"opened_at": {}, "active_trades": {}, "protection": {},
              "cooldowns": {}}
        reason = "r" * 250
        plan = {
            "symbol": "BTC/USDT:USDT", "direction": "long",
            "leverage": 2, "sl_pct": 2, "tp_pct": 4,
            "confidence": 0.8, "reason": reason,
            "estimated_loss_pct": 2.7, "entry_equity_usd": 1_000,
        }
        execution = {
            "filled": 2, "average": 100, "fee_usd": 0.2,
            "position_contracts": 2, "position_id": "position-1",
            "status": "closed",
            "protection": {"stop_loss": True, "take_profit": True},
        }

        self.assertTrue(engine._settle_entry(
            plan, st, execution, plan["symbol"], "buy", 98, 104))

        commit.assert_called_once_with(st)
        self.assertEqual(
            st["active_trades"][plan["symbol"]]["entry_reason"], reason)

    @patch("agent.engine.state.load_state", return_value={"state": "RUNNING"})
    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_trade")
    def test_unprotected_fill_is_journaled_then_emergency_closed(
            self, log_trade, commit, load_state):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.ex = FakeEmergencyExchange()
        engine.alerts = Mock()
        st = {"opened_at": {}, "active_trades": {}, "protection": {},
              "cooldowns": {}}
        plan = {
            "symbol": "BTC/USDT:USDT", "direction": "long",
            "notional": 200, "price": 100, "leverage": 2,
            "sl_pct": 2, "tp_pct": 4, "confidence": 0.8,
            "reason": "test",
        }

        self.assertFalse(engine._execute_open(plan, st))
        self.assertEqual([call.args[2] for call in log_trade.call_args_list],
                         ["open", "close"])
        first_id = log_trade.call_args_list[0].kwargs["trade_id"]
        second_id = log_trade.call_args_list[1].kwargs["trade_id"]
        self.assertEqual(first_id, second_id)
        self.assertEqual(st["active_trades"], {})
        self.assertGreaterEqual(commit.call_count, 2)

    @patch("agent.engine.state.load_state", return_value={"state": "RUNNING"})
    @patch("agent.engine.state.commit", side_effect=OSError("disk full"))
    @patch("agent.engine.state.log_trade")
    def test_persistence_failure_cannot_block_unprotected_emergency_close(
            self, log_trade, commit, load_state):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.ex = FakeEmergencyExchange()
        engine.alerts = Mock()
        st = {"opened_at": {}, "active_trades": {}, "protection": {},
              "cooldowns": {}}
        plan = {
            "symbol": "BTC/USDT:USDT", "direction": "long",
            "notional": 200, "price": 100, "leverage": 2,
            "sl_pct": 2, "tp_pct": 4, "confidence": 0.8,
            "reason": "test", "estimated_loss_pct": 2.7,
        }

        with self.assertRaisesRegex(
                state.JournalError, "post-entry persistence failed"):
            engine._execute_open(plan, st)

        self.assertEqual(engine.ex.close_calls, 1)
        self.assertEqual(st["active_trades"], {})

    @patch("agent.engine.time.sleep")
    @patch("agent.engine.state.load_state", return_value={"state": "RUNNING"})
    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_trade")
    def test_unprotected_partial_close_is_retried_without_waiting_a_cycle(
            self, log_trade, commit, load_state, sleep):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.ex = PartialEmergencyExchange()
        engine.alerts = Mock()
        st = {"opened_at": {}, "active_trades": {}, "protection": {},
              "cooldowns": {}}
        plan = {
            "symbol": "BTC/USDT:USDT", "direction": "long",
            "notional": 200, "price": 100, "leverage": 2,
            "sl_pct": 2, "tp_pct": 4, "confidence": 0.8,
            "reason": "test", "estimated_loss_pct": 2.7,
        }

        self.assertFalse(engine._execute_open(plan, st))

        self.assertEqual(engine.ex.close_calls, 2)
        self.assertEqual(st["active_trades"], {})

    @patch("agent.engine.state.load_state", return_value={"state": "RUNNING"})
    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_trade")
    def test_protected_fill_with_unsafe_liquidation_buffer_is_closed(
            self, log_trade, commit, load_state):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.ex = UnsafeLiquidationExchange()
        engine.alerts = Mock()
        st = {"opened_at": {}, "active_trades": {}, "protection": {},
              "cooldowns": {}, "entry_failures": {}}
        plan = {
            "symbol": "BTC/USDT:USDT", "direction": "long",
            "notional": 200, "price": 100, "leverage": 2,
            "sl_pct": 2, "tp_pct": 4, "confidence": 0.8,
            "reason": "test", "estimated_loss_pct": 2.7,
        }

        self.assertFalse(engine._execute_open(plan, st))

        self.assertEqual(engine.ex.close_calls, 1)
        self.assertEqual(st["active_trades"], {})
        self.assertEqual(engine.alerts.send.call_args.args[1],
                         "liquidation_buffer_unsafe")


class AccountRiskGuardTests(unittest.TestCase):
    @patch("agent.engine.time.sleep")
    def test_largest_position_is_closed_until_imr_and_mmr_are_safe(
            self, sleep):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.alerts = Mock()
        engine.ex = Mock()
        engine.ex.account_risk_metrics.side_effect = [
            {
                "initial_margin_usage_pct": 70,
                "maintenance_margin_ratio": 2,
            },
            {
                "initial_margin_usage_pct": 20,
                "maintenance_margin_ratio": 10,
            },
        ]
        engine._close = Mock(return_value=True)
        large = {"symbol": "BTC/USDT:USDT", "notional": 500, "side": "long"}
        small = {"symbol": "ETH/USDT:USDT", "notional": 200, "side": "long"}

        kept = engine._manage_positions(
            [small, large], {"opened_at": {}}, 10_000)

        self.assertEqual(kept, [small])
        engine._close.assert_called_once_with(
            large, "account IMR/MMR guard", {"opened_at": {}})

    def test_unsafe_account_metrics_with_no_position_block_new_entries(self):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.alerts = Mock()
        engine.ex = Mock()
        engine.ex.account_risk_metrics.return_value = {
            "initial_margin_usage_pct": 70,
            "maintenance_margin_ratio": 2,
        }

        with self.assertRaisesRegex(
                RuntimeError, "account margin risk remains unsafe"):
            engine._manage_positions([], {"opened_at": {}}, 10_000)

    def test_account_risk_auth_failure_is_not_downgraded_to_generic_error(self):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.alerts = Mock()
        engine.ex = Mock()
        engine.ex.account_risk_metrics.side_effect = CredentialError(
            "bad key")

        with self.assertRaises(CredentialError):
            engine._manage_positions([], {"opened_at": {}}, 10_000)


if __name__ == "__main__":
    unittest.main()
