import copy
import unittest
from unittest.mock import Mock, patch

import ccxt

from agent import state
from agent.engine import Engine, PositionAgeUnknown
from agent.exchange import (CredentialError, Exchange,
                            OrderSubmissionAmbiguousError)
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
            "symbol": "BTC/USDT:USDT",
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
                    "position_id": "position-1",
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

    def assert_reconciliation_pauses(self, st, positions):
        durable = copy.deepcopy(st)
        durable["state"] = state.RUNNING
        paused = copy.deepcopy(durable)
        paused.update({"state": state.PAUSED, "operator_pause": True})
        with patch("agent.engine.state.load_state", return_value=durable), \
                patch("agent.engine.state.set_state", return_value=paused) \
                as set_state:
            with self.assertRaises(OrderSubmissionAmbiguousError):
                self.engine._reconcile_positions(
                    positions, st, startup=True)
        set_state.assert_called_once()
        self.assertTrue(set_state.call_args.kwargs["operator_pause"])
        return st

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

    def test_reconciliation_preserves_auth_and_journal_failures(self):
        for error in (
                CredentialError("bad credentials"),
                ccxt.AuthenticationError("bad credentials"),
                state.JournalError("journal unavailable")):
            with self.subTest(error=type(error).__name__):
                st = self.tracked_state()
                self.engine.ex.closed_position_summary = Mock(
                    side_effect=error)
                with self.assertRaises(type(error)):
                    self.engine._reconcile_positions([], st, startup=True)

    def test_protection_restore_preserves_credentials(self):
        st = self.tracked_state()
        position = {
            "id": "position-1", "symbol": "BTC/USDT:USDT",
            "side": "long", "contracts": 2,
            "entryPrice": 100, "markPrice": 100, "leverage": 2,
            "info": {"posId": "position-1"},
        }
        self.engine.ex.protection_status = Mock(return_value={
            "stop_loss": False, "take_profit": False,
            "stop_price": None, "take_price": None,
        })
        self.engine.ex.ensure_protection = Mock(
            side_effect=CredentialError("bad credentials"))

        with self.assertRaises(CredentialError):
            self.engine._reconcile_positions([position], st, startup=True)

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

    def test_missing_position_identity_never_auto_matches(self):
        valid_live = {
            "id": "position-1", "symbol": "BTC/USDT:USDT",
            "side": "long", "contracts": 2, "entryPrice": 100,
            "markPrice": 100, "leverage": 2,
            "info": {"posId": "position-1"},
        }
        for missing in ("tracked", "live"):
            with self.subTest(missing=missing):
                st = self.tracked_state()
                live = copy.deepcopy(valid_live)
                if missing == "tracked":
                    st["active_trades"]["BTC/USDT:USDT"].pop(
                        "position_id")
                else:
                    live.pop("id")
                    live["info"].pop("posId")
                paused = self.assert_reconciliation_pauses(st, [live])
                self.assertIn("BTC/USDT:USDT", paused["active_trades"])

    def test_same_position_identity_with_quantity_drift_pauses(self):
        st = self.tracked_state()
        live = {
            "id": "position-1", "symbol": "BTC/USDT:USDT",
            "side": "long", "contracts": 3, "entryPrice": 100,
            "markPrice": 100, "leverage": 2,
            "info": {"posId": "position-1"},
        }

        paused = self.assert_reconciliation_pauses(st, [live])

        self.assertEqual(
            paused["active_trades"]["BTC/USDT:USDT"]["qty"], 2)

    def test_unresolved_close_keeps_trade_and_operator_pauses(self):
        st = self.tracked_state()
        self.engine.ex.closed_position_summary = Mock(
            side_effect=OrderSubmissionAmbiguousError(
                "no valid closing fills found yet"))

        paused = self.assert_reconciliation_pauses(st, [])

        self.assertIn("BTC/USDT:USDT", paused["active_trades"])

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
            "remaining_contracts": 0,
            "filled": 1,
            "average": 105,
            "fee_usd": 0.2,
            "order_id": "close-1",
            "client_order_id": "client-close-1",
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
            "id": "position-1", "symbol": "BTC/USDT:USDT",
            "side": "long", "contracts": 2,
            "entryPrice": 100, "markPrice": 102, "leverage": 2,
            "info": {"posId": "position-1"},
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
            "id": "position-1", "symbol": "BTC/USDT:USDT",
            "side": "long", "contracts": 2,
            "entryPrice": 100, "markPrice": 102, "leverage": 2,
            "info": {"posId": "position-1"},
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
            "id": "position-eth", "symbol": "ETH/USDT:USDT",
            "side": "long", "contracts": 2,
            "entryPrice": 100, "markPrice": 101, "leverage": 2,
            "info": {"posId": "position-eth"},
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
            "id": "position-1", "symbol": "BTC/USDT:USDT",
            "side": "long", "contracts": 2,
            "entryPrice": 100, "markPrice": 100, "liquidationPrice": 94.5,
            "leverage": 2,
            "info": {"posId": "position-1"},
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
            "symbol": "BTC/USDT:USDT", "order_id": "entry",
            "client_order_id": "client-entry", "status": "closed",
            "filled": 2,
            "average": 100, "partial": False, "fee_usd": 0.2,
            "slippage_usd": 0, "adverse_slippage_usd": 0,
            "position_contracts": 2,
            "position_id": "position-1",
            "protection": {"stop_loss": False, "take_profit": False},
        }

    def close_position(self, pos):
        self.close_calls += 1
        return {
            "order_id": "exit", "client_order_id": "client-exit",
            "status": "closed", "filled": 2,
            "average": 99, "partial": False, "fee_usd": 0.2,
            "slippage_usd": 2, "adverse_slippage_usd": 2,
            "fully_closed": True,
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
            "order_id": f"exit-{self.close_calls}",
            "client_order_id": f"client-exit-{self.close_calls}",
            "status": "closed",
            "filled": 1, "average": 99, "partial": bool(remaining),
            "fee_usd": 0.1, "slippage_usd": 1,
            "adverse_slippage_usd": 1,
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
            "symbol": "BTC/USDT:USDT", "order_id": "entry",
            "client_order_id": "client-entry", "status": "closed",
            "filled": 2,
            "average": 100, "partial": False, "fee_usd": 0.2,
            "slippage_usd": 0, "adverse_slippage_usd": 0,
            "position_contracts": 2,
            "position_id": "position-1", "mark_price": 100,
            "liquidation_price": 97.5,
            "protection": {"stop_loss": True, "take_profit": True},
        }


class EmergencyExecutionTests(unittest.TestCase):
    @staticmethod
    def valid_entry_execution(**updates):
        execution = {
            "symbol": "BTC/USDT:USDT", "order_id": "entry-1",
            "client_order_id": "client-entry-1", "status": "closed",
            "filled": 2, "average": 100, "partial": False,
            "fee_usd": 0.2, "slippage_usd": 0,
            "adverse_slippage_usd": 0, "position_contracts": 2,
            "position_id": "position-1",
            "protection": {"stop_loss": True, "take_profit": True},
        }
        execution.update(updates)
        return execution

    @staticmethod
    def valid_plan():
        return {
            "symbol": "BTC/USDT:USDT", "direction": "long",
            "notional": 200, "price": 100, "leverage": 2,
            "sl_pct": 2, "tp_pct": 4, "confidence": 0.8,
            "reason": "test", "estimated_loss_pct": 2.7,
            "entry_equity_usd": 1_000,
        }

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
            "symbol": "BTC/USDT:USDT", "order_id": "entry",
            "client_order_id": "client-entry",
            "filled": 2, "average": 100, "fee_usd": 0.2,
            "position_contracts": 2, "position_id": "position-1",
            "status": "closed", "partial": False,
            "slippage_usd": 0, "adverse_slippage_usd": 0,
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

    def test_close_preserves_credentials_and_journal_failures(self):
        pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1}
        for error in (
                CredentialError("bad credentials"),
                ccxt.AuthenticationError("bad credentials"),
                state.JournalError("journal unavailable")):
            with self.subTest(error=type(error).__name__):
                engine = Engine.__new__(Engine)
                engine.ex = Mock()
                engine.ex.close_position.side_effect = error
                with self.assertRaises(type(error)):
                    engine._close(pos, "test", {})

    def test_ambiguous_close_durably_pauses_and_preserves_type(self):
        engine = Engine.__new__(Engine)
        engine.ex = Mock()
        engine.ex.close_position.side_effect = OrderSubmissionAmbiguousError(
            "close state unknown", {"order_id": "close-1"})
        st = {"state": state.RUNNING}
        paused = {"state": state.PAUSED, "operator_pause": True}

        with patch("agent.engine.state.load_state", return_value={
                "state": state.RUNNING}), patch(
                    "agent.engine.state.set_state", return_value=paused
                ) as set_state:
            with self.assertRaises(OrderSubmissionAmbiguousError):
                engine._close(
                    {"symbol": "BTC/USDT:USDT", "side": "long",
                     "contracts": 1}, "test", st)

        set_state.assert_called_once()
        self.assertTrue(set_state.call_args.kwargs["operator_pause"])
        self.assertEqual(st["state"], state.PAUSED)

    def test_malformed_close_schema_pauses_and_preserves_tracked_trade(self):
        base = {
            "order_id": "close-1", "client_order_id": "client-close-1",
            "status": "canceled", "filled": 1, "average": 99,
            "remaining_contracts": 1, "fully_closed": False,
            "fee_usd": 0.1, "slippage_usd": 0,
            "adverse_slippage_usd": 0,
        }
        cases = (
            {**base, "fully_closed": True},
            {key: value for key, value in base.items()
             if key != "remaining_contracts"},
            {**base, "remaining_contracts": "bad"},
            {**base, "remaining_contracts": 3},
            {**base, "filled": 0.5},
        )
        for result in cases:
            with self.subTest(result=result):
                engine = Engine.__new__(Engine)
                engine.ex = Mock()
                engine.ex.close_position.return_value = result
                trade = {"trade_id": "trade-1", "direction": "long",
                         "qty": 2}
                st = {"state": state.RUNNING,
                      "active_trades": {"BTC/USDT:USDT": trade}}
                durable = copy.deepcopy(st)
                paused = copy.deepcopy(st)
                paused.update({"state": state.PAUSED,
                               "operator_pause": True})
                with patch("agent.engine.state.load_state",
                           return_value=durable), patch(
                               "agent.engine.state.set_state",
                               return_value=paused) as set_state:
                    with self.assertRaises(OrderSubmissionAmbiguousError):
                        engine._close(
                            {"symbol": "BTC/USDT:USDT", "side": "long",
                             "contracts": 2}, "test", st)
                set_state.assert_called_once()
                self.assertIn("BTC/USDT:USDT", st["active_trades"])

    def test_entry_schema_rejects_quantity_drift_and_string_booleans(self):
        cases = (
            self.valid_entry_execution(position_contracts=3),
            self.valid_entry_execution(protection={
                "stop_loss": "true", "take_profit": True}),
            self.valid_entry_execution(partial="false"),
        )
        for execution in cases:
            with self.subTest(execution=execution):
                engine = Engine.__new__(Engine)
                engine.cfg = valid_config()
                engine.ex = FakeEmergencyExchange()
                engine.alerts = Mock()
                st = {"state": state.RUNNING, "opened_at": {},
                      "active_trades": {}, "protection": {},
                      "cooldowns": {}}
                durable = copy.deepcopy(st)
                paused = copy.deepcopy(st)
                paused.update({"state": state.PAUSED,
                               "operator_pause": True})
                with patch("agent.engine.state.load_state",
                           return_value=durable), patch(
                               "agent.engine.state.set_state",
                               return_value=paused) as set_state:
                    with self.assertRaises(OrderSubmissionAmbiguousError):
                        engine._settle_entry(
                            self.valid_plan(), st, execution,
                            "BTC/USDT:USDT", "buy", 98, 104)
                set_state.assert_called_once()
                self.assertEqual(st["active_trades"], {})

    def test_generic_post_fill_settlement_error_becomes_typed_pause(self):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.ex = FakeEmergencyExchange()
        engine.alerts = Mock()
        engine._liquidation_stop_check = Mock(
            side_effect=ValueError("malformed liquidation metadata"))
        st = {"state": state.RUNNING, "opened_at": {},
              "active_trades": {}, "protection": {}, "cooldowns": {}}
        durable = copy.deepcopy(st)
        paused = copy.deepcopy(st)
        paused.update({"state": state.PAUSED, "operator_pause": True})

        with patch("agent.engine.state.load_state", return_value=durable), \
                patch("agent.engine.state.set_state", return_value=paused) \
                as set_state:
            with self.assertRaises(OrderSubmissionAmbiguousError):
                engine._settle_entry(
                    self.valid_plan(), st, self.valid_entry_execution(),
                    "BTC/USDT:USDT", "buy", 98, 104)

        set_state.assert_called_once()
        self.assertEqual(st["state"], state.PAUSED)

    def test_direct_open_partial_then_post_cancel_read_failure_pauses_unsettled(self):
        verifier = Exchange.__new__(Exchange)
        verifier.cfg = valid_config()
        verifier.cfg["execution"]["fill_timeout_seconds"] = 0
        verifier.alerts = None
        verifier.x = Mock()
        verifier.x.cancel_order.return_value = {}
        verifier.x.fetch_order.side_effect = ccxt.RequestTimeout("timeout")
        verifier.retry = lambda fn, *args, **kwargs: fn(*args, **kwargs)

        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.cfg["execution"]["maker_first_enabled"] = False
        engine.alerts = Mock()
        engine.ex = Mock()
        engine.ex.price.return_value = 100
        engine.ex.contracts_for_notional.return_value = 5
        engine.ex.guarded_entry_limit.return_value = {
            "mid": 100, "limit_price": 100, "spread_pct": 0,
        }
        engine.ex.open_position.side_effect = lambda *args, **kwargs: (
            verifier.verify_fill(
                {"id": "entry-1", "status": "open", "filled": 2,
                 "remaining": 3, "average": 100},
                "BTC/USDT:USDT", requested=5))
        engine._settle_entry = Mock()
        st = {"state": state.RUNNING}
        plan = {
            "symbol": "BTC/USDT:USDT", "direction": "long",
            "notional": 500, "price": 100, "leverage": 2,
            "sl_pct": 2, "tp_pct": 4, "confidence": 0.8,
            "reason": "test", "estimated_loss_pct": 2.7,
        }
        paused = {"state": state.PAUSED, "operator_pause": True}

        with patch("agent.engine.state.load_state", return_value={
                "state": state.RUNNING}), patch(
                    "agent.engine.state.set_state", return_value=paused
                ) as set_state, patch("agent.engine.state.log_event"):
            self.assertFalse(engine._execute_open(plan, st))

        set_state.assert_called_once()
        engine._settle_entry.assert_not_called()
        self.assertEqual(st["state"], state.PAUSED)

    def test_unprotected_fill_auth_failure_pauses_immediately(self):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.alerts = Mock()
        engine.ex = Mock()
        error = CredentialError("bad credentials")
        error._post_fill_unsettled = True
        error._order_audit = {"order_id": "entry-1"}
        engine.ex.open_position.side_effect = error
        engine.ex.price.return_value = 100
        engine.ex.contracts_for_notional.return_value = 1
        engine.ex.guarded_entry_limit.return_value = {
            "mid": 100, "limit_price": 100, "spread_pct": 0,
        }
        st = {"state": state.RUNNING}
        paused = {"state": state.PAUSED, "operator_pause": True}
        plan = {
            "symbol": "BTC/USDT:USDT", "direction": "long",
            "notional": 100, "price": 100, "leverage": 2,
            "sl_pct": 2, "tp_pct": 4, "confidence": 0.8,
            "reason": "test", "estimated_loss_pct": 2.7,
        }

        with patch("agent.engine.state.load_state", return_value={
                "state": state.RUNNING}), patch(
                    "agent.engine.state.set_state", return_value=paused
                ) as set_state:
            with self.assertRaises(CredentialError):
                engine._execute_open(plan, st)

        set_state.assert_called_once()
        self.assertEqual(st["state"], state.PAUSED)

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
