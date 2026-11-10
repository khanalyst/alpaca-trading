"""Exit intent must survive cancellation, ambiguous submits and restart."""

from unittest.mock import patch
from types import SimpleNamespace

from agent import state
from tests.test_broker_protection import ProtectionHarness


class MonitorCloseRecoveryTests(ProtectionHarness):
    def _start(self, name="monitor-recovery", profile="shares"):
        self._bind_engine(profile=profile, runtime_name=name)
        if profile == "options":
            self._open_option_position()
        else:
            self._open_bracketed_position()
        self.engine.market.should_force_flat = lambda now: True

    def _restart(self, name="monitor-recovery", profile="shares"):
        self.engine.close()
        self._bind_engine(profile=profile, runtime_name=name)
        self.engine.market.should_force_flat = lambda now: True
        self.engine.reconcile()

    def _monitor(self):
        return self.engine._monitor_positions(self.NOW, list(self.provider.positions_live))

    def test_crash_after_cancels_leaves_retryable_durable_intent(self):
        self._start()
        with patch.object(self.engine, "_close_position", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self._monitor()
        saved = state.load_state()
        trade = saved["active_trades"]["SPY"]
        intent_key = trade["close_intent_id"]
        intent = saved["orders"][intent_key]
        self.assertEqual(intent["status"], "intent")
        self.assertEqual(intent["entry_order_id"], trade["order_id"])
        self.assertEqual(len(self.provider.cancelled), 2)
        self.assertEqual(self.provider.close_requests, [])

        self._restart()
        result = self._monitor()
        self.assertEqual(result["closed"], [{"symbol": "SPY", "reason": "before_close"}])
        self.assertEqual(self.provider.close_requests[0].client_order_id, intent["client_order_id"])
        current = state.load_state()
        self.assertNotIn(intent_key, current["orders"])
        self.assertNotIn("close_intent_id", current["active_trades"]["SPY"])
        self._monitor()
        self.assertEqual(len(self.provider.close_requests), 1)

    def test_broker_accept_then_process_death_is_adopted_without_resubmit(self):
        self._start()
        submit = self.provider.close_position

        def accepted_then_crash(*args, **kwargs):
            submit(*args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(self.provider, "close_position", side_effect=accepted_then_crash):
            with self.assertRaises(KeyboardInterrupt):
                self._monitor()
        self.assertEqual(len(self.provider.close_requests), 1)
        client = self.provider.close_requests[0].client_order_id
        self._restart()
        trade = state.load_state()["active_trades"]["SPY"]
        order = self.provider.orders_by_id[trade["closing_order_id"]]
        self.assertEqual(order.client_order_id, client)
        self.assertNotIn("close_intent_id", trade)
        self._monitor()
        self.assertEqual(len(self.provider.close_requests), 1)

    def test_none_response_is_failed_and_does_not_create_a_submitted_close(self):
        self._start()
        with patch.object(self.provider, "close_position", return_value=None):
            result = self._monitor()
        self.assertEqual(result["closed"], [])
        self.assertIn("no broker order", result["failed"][0]["error"])
        current = state.load_state()
        self.assertTrue(current["operator_pause"])
        trade = current["active_trades"]["SPY"]
        intent = current["orders"][trade["close_intent_id"]]
        self.assertEqual(intent["status"], "intent")
        self.assertFalse(any(row.get("action") == "close" and row.get("status") == "submitted"
                             for row in current["orders"].values()))
        self._monitor()
        self.assertEqual(len(self.provider.close_requests), 1)
        self.assertEqual(self.provider.close_requests[0].client_order_id, intent["client_order_id"])

    def test_unknown_response_preserves_intent_without_competing_stop(self):
        self._start()
        response = SimpleNamespace(id="unknown-close", status="mystery")
        before = len(self.provider.submitted)
        with patch.object(self.provider, "close_position", return_value=response):
            result = self._monitor()
        self.assertEqual(result["closed"], [])
        self.assertIn("unknown broker status", result["failed"][0]["error"])
        current = state.load_state()
        intent = current["orders"][current["active_trades"]["SPY"]["close_intent_id"]]
        self.assertEqual(intent["status"], "intent")
        self.assertTrue(current["operator_pause"])
        self.assertEqual(len(self.provider.submitted), before)
        self._restart()
        self._monitor()
        self.assertEqual(len(self.provider.close_requests), 1)
        self.assertEqual(self.provider.close_requests[0].client_order_id, intent["client_order_id"])

    def _assert_unconfirmed_rearm_is_refused(self, response):
        self._start()
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertTrue(self.engine._cancel_protective_legs("SPY", trade["protective_legs"]))
        with patch.object(self.provider, "submit_order", return_value=response) as submit:
            leg = self.engine._rearm_protective_stop(
                "SPY", trade, self.provider.positions_live[0], reason="before_close")
        self.assertIsNone(leg)
        self.assertEqual(submit.call_count, 1)
        self.assertFalse(any(leg.get("rearmed") for leg in
                             state.load_state()["active_trades"]["SPY"]["protective_legs"]))

    def test_none_stop_response_is_not_confirmed_protection(self):
        self._assert_unconfirmed_rearm_is_refused(None)

    def test_idless_stop_response_is_not_confirmed_protection(self):
        self._assert_unconfirmed_rearm_is_refused(SimpleNamespace(id="", status="accepted"))

    def test_pending_cancel_stop_is_not_confirmed_protection(self):
        self._assert_unconfirmed_rearm_is_refused(
            SimpleNamespace(id="stop-pending", status="pending_cancel", filled_qty=0))

    def test_filled_stop_response_cannot_trigger_a_second_stop(self):
        self._assert_unconfirmed_rearm_is_refused(
            SimpleNamespace(id="stop-filled", status="filled", filled_qty=10))

    def test_intent_storage_failure_precedes_every_broker_mutation(self):
        self._start()
        with patch("agent.execution_lifecycle.state.update_state", side_effect=OSError("disk full")):
            result = self._monitor()
        self.assertEqual(result["closed"], [])
        self.assertTrue(result["failed"])
        self.assertEqual(self.provider.cancelled, [])
        self.assertEqual(self.provider.close_requests, [])

    def test_option_failed_close_retries_original_cause_after_restart(self):
        self._start(profile="options")
        with patch.object(self.provider, "close_position", side_effect=RuntimeError("close rejected")):
            result = self._monitor()
        self.assertEqual(result["closed"], [])
        current = state.load_state()
        trade = current["active_trades"][self.OPTION]
        intent = current["orders"][trade["close_intent_id"]]
        self.assertTrue(current["operator_pause"])
        self._restart(profile="options")
        self.engine.market.should_force_flat = lambda now: False
        self.engine._protection_observation = lambda *args, **kwargs: (103.0, {})
        result = self._monitor()
        self.assertEqual(result["closed"], [{"symbol": self.OPTION, "reason": "before_close"}])
        self.assertEqual(len(self.provider.close_requests), 1)
        self.assertEqual(self.provider.close_requests[0].client_order_id, intent["client_order_id"])

    def test_filled_close_with_lagging_position_does_not_close_twice(self):
        self._start()
        self._monitor()
        current = state.load_state()
        order_id = current["active_trades"]["SPY"]["closing_order_id"]
        self.provider.set_order(order_id, status="filled", filled_qty=10, filled_avg_price=99)
        self.engine.reconcile()
        self._monitor()
        self.assertEqual(len(self.provider.close_requests), 1)

    def test_cancel_failure_keeps_the_same_retry_identity(self):
        self._start()
        self.provider.cancel_error = "cancel unavailable"
        result = self._monitor()
        self.assertEqual(result["closed"], [])
        current = state.load_state()
        intent = current["orders"][current["active_trades"]["SPY"]["close_intent_id"]]
        self.provider.cancel_error = None
        self._restart()
        self._monitor()
        self.assertEqual(len(self.provider.close_requests), 1)
        self.assertEqual(self.provider.close_requests[0].client_order_id, intent["client_order_id"])
