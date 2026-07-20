import unittest
from unittest.mock import Mock, patch

from agent.engine import Engine
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


class FakeEmergencyExchange:
    def __init__(self):
        self.x = Mock()
        self.x.market.return_value = {"contractSize": 1}

    @staticmethod
    def price(symbol):
        return 100

    @staticmethod
    def contracts_for_notional(symbol, notional, price):
        return 2

    @staticmethod
    def open_position(*args, **kwargs):
        return {
            "order_id": "entry", "status": "closed", "filled": 2,
            "average": 100, "partial": False, "fee_usd": 0.2,
            "slippage_usd": 0, "position_contracts": 2,
            "position_id": "position-1",
            "protection": {"stop_loss": False, "take_profit": False},
        }

    @staticmethod
    def close_position(pos):
        return {
            "order_id": "exit", "status": "closed", "filled": 2,
            "average": 99, "partial": False, "fee_usd": 0.2,
            "slippage_usd": 2, "fully_closed": True,
            "remaining_contracts": 0,
        }

    @staticmethod
    def funding_since(symbol, since_ms):
        return 0


class EmergencyExecutionTests(unittest.TestCase):
    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_trade")
    def test_unprotected_fill_is_journaled_then_emergency_closed(
            self, log_trade, commit):
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


if __name__ == "__main__":
    unittest.main()
