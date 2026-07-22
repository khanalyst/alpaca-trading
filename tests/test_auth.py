import time
import unittest
from unittest.mock import Mock, patch

import ccxt

from agent.engine import MAX_CREDENTIAL_FAILURES, Engine
from agent.exchange import CLOCK_SKEW_FATAL_MS, CredentialError, Exchange
from tests.helpers import valid_config


def bare_exchange(client):
    ex = Exchange.__new__(Exchange)
    ex.cfg = valid_config()
    ex.alerts = None
    ex.x = client
    return ex


class CountingClient:
    """Raises a fixed error and counts how many times it was called."""

    def __init__(self, error):
        self.error = error
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        raise self.error

    @staticmethod
    def fetch_time():
        return time.time() * 1000


class RetryClassificationTests(unittest.TestCase):
    def test_timestamp_rejection_is_not_retried(self):
        # OKX 50102 arrives as InvalidNonce, a NetworkError subclass. A wrong
        # clock cannot heal between retries, so it must fail fast instead.
        client = CountingClient(ccxt.InvalidNonce("50102 timestamp expired"))
        ex = bare_exchange(client)
        ex.x.fetch_time = CountingClient.fetch_time
        with self.assertRaises(CredentialError) as caught:
            ex.retry(client)
        self.assertEqual(client.calls, 1)
        self.assertIn("30s", str(caught.exception))

    def test_authentication_error_is_not_retried(self):
        client = CountingClient(ccxt.AuthenticationError("50114 no permission"))
        ex = bare_exchange(client)
        with self.assertRaises(CredentialError) as caught:
            ex.retry(client)
        self.assertEqual(client.calls, 1)
        self.assertIn("Trade permission", str(caught.exception))

    def test_network_errors_are_still_retried(self):
        client = CountingClient(ccxt.NetworkError("blip"))
        ex = bare_exchange(client)
        with patch("agent.exchange.time.sleep"):
            with self.assertRaises(ccxt.NetworkError):
                ex.retry(client)
        self.assertEqual(client.calls, 3)


class ClockCheckTests(unittest.TestCase):
    def _exchange_with_drift(self, drift_ms):
        client = Mock()
        client.fetch_time.return_value = time.time() * 1000 - drift_ms
        return bare_exchange(client)

    def test_drift_inside_the_window_passes(self):
        ex = self._exchange_with_drift(500)
        self.assertLess(abs(ex.check_clock()), CLOCK_SKEW_FATAL_MS)

    def test_drift_past_the_limit_refuses_to_start(self):
        ex = self._exchange_with_drift(CLOCK_SKEW_FATAL_MS + 5_000)
        with self.assertRaises(CredentialError) as caught:
            ex.check_clock()
        self.assertIn("50102", str(caught.exception))

    def test_unreachable_time_endpoint_is_not_a_clock_failure(self):
        client = Mock()
        client.fetch_time.side_effect = ccxt.NetworkError("down")
        self.assertEqual(bare_exchange(client).check_clock(), 0.0)

    def test_recheck_only_runs_once_per_interval(self):
        ex = self._exchange_with_drift(0)
        ex.recheck_clock_if_due()
        ex.recheck_clock_if_due()
        self.assertEqual(ex.x.fetch_time.call_count, 1)


class TradePermissionTests(unittest.TestCase):
    @staticmethod
    def _account_response(perm="read_only,trade", pos_mode="net_mode",
                          ip="203.0.113.10"):
        return {"code": "0", "data": [{
            "perm": perm, "posMode": pos_mode, "acctLv": "2", "ip": ip,
        }]}

    @staticmethod
    def _balance_response(usdt=10_000.0, non_usdt=0.0):
        details = [{"ccy": "USDT", "eqUsd": str(usdt)}]
        if non_usdt:
            details.append({"ccy": "BTC", "eqUsd": str(non_usdt)})
        return {"info": {"data": [{
            "totalEq": str(usdt + non_usdt), "details": details,
        }]}}

    def test_probe_is_read_only_and_checks_account_configuration(self):
        client = Mock()
        client.private_get_account_config.return_value = self._account_response()
        client.fetch_balance.return_value = self._balance_response()
        ex = bare_exchange(client)
        result = ex.verify_trade_permission()
        self.assertEqual(result["position_mode"], "net_mode")
        self.assertIn("trade", result["permissions"])
        client.private_get_account_config.assert_called_once()
        client.set_leverage.assert_not_called()
        client.create_order.assert_not_called()

    def test_demo_reports_mixed_collateral_without_refusing_to_start(self):
        # Live refuses; demo is where mixed collateral should be *found*, so it
        # measures and warns instead. Silence would let paper results describe
        # an account whose sizing the exchange would never have accepted.
        client = Mock()
        client.private_get_account_config.return_value = self._account_response()
        client.fetch_balance.return_value = self._balance_response(
            usdt=5_000.0, non_usdt=76_000.0)
        ex = bare_exchange(client)
        with self.assertLogs("exchange", level="WARNING") as logged:
            result = ex.verify_trade_permission()
        self.assertAlmostEqual(result["non_usdt_collateral_pct"], 93.83, places=1)
        self.assertIn("non-USDT", "\n".join(logged.output))

    def test_read_only_key_is_rejected(self):
        client = Mock()
        client.private_get_account_config.return_value = self._account_response(
            perm="read_only")
        with self.assertRaises(CredentialError):
            bare_exchange(client).verify_trade_permission()

    def test_hedge_mode_is_rejected_without_trying_to_change_it(self):
        client = Mock()
        client.private_get_account_config.return_value = self._account_response(
            pos_mode="long_short_mode")
        with self.assertRaisesRegex(RuntimeError, "net_mode"):
            bare_exchange(client).verify_trade_permission()
        client.set_position_mode.assert_not_called()

    def test_withdraw_permission_is_rejected(self):
        client = Mock()
        client.private_get_account_config.return_value = self._account_response(
            perm="read_only,trade,withdraw")
        with self.assertRaisesRegex(CredentialError, "Withdraw"):
            bare_exchange(client).verify_trade_permission()

    def test_live_account_requires_ip_binding(self):
        client = Mock()
        client.private_get_account_config.return_value = self._account_response(
            ip="")
        ex = bare_exchange(client)
        ex.cfg["mode"] = "live"
        ex.demo = False
        with self.assertRaisesRegex(CredentialError, "IP-bound"):
            ex.verify_trade_permission()

    def test_live_account_rejects_material_non_usdt_collateral(self):
        client = Mock()
        client.private_get_account_config.return_value = self._account_response()
        client.fetch_balance.return_value = {"info": {"data": [{
            "totalEq": "10000",
            "details": [
                {"ccy": "USDT", "eqUsd": "9000"},
                {"ccy": "BTC", "eqUsd": "1000"},
            ],
        }]}}
        ex = bare_exchange(client)
        ex.cfg["mode"] = "live"
        ex.demo = False
        with self.assertRaisesRegex(RuntimeError, "non-USDT"):
            ex.verify_trade_permission()


class CredentialFailureHandlingTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.engine.alerts = Mock()
        self.engine.ex = Mock()
        self.engine.ex.positions.return_value = [{"symbol": "BTC/USDT:USDT"}]
        self.engine._credential_failures = 0

    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.set_state")
    def test_loop_continues_below_the_threshold(self, set_state, log_event):
        stop = self.engine._on_credential_failure(CredentialError("bad key"))
        self.assertFalse(stop)
        set_state.assert_not_called()

    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.set_state")
    def test_repeated_failures_pause_rather_than_kill(self, set_state,
                                                      log_event):
        for _ in range(MAX_CREDENTIAL_FAILURES - 1):
            self.engine._on_credential_failure(CredentialError("bad key"))
        stop = self.engine._on_credential_failure(CredentialError("bad key"))
        self.assertTrue(stop)
        # A kill would try to flatten, which needs the API access we just
        # lost. Pausing leaves the exchange-side SL/TP doing its job.
        state_name, reason = set_state.call_args.args
        self.assertEqual(state_name, "PAUSED")
        self.assertIn("credentials rejected", reason)
        self.assertTrue(set_state.call_args.kwargs["operator_pause"])

    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.set_state")
    def test_stop_alert_names_the_unmanaged_positions(self, set_state,
                                                      log_event):
        for _ in range(MAX_CREDENTIAL_FAILURES):
            self.engine._on_credential_failure(CredentialError("bad key"))
        level, event, _, details = self.engine.alerts.send.call_args.args
        self.assertEqual(level, "critical")
        self.assertEqual(event, "credential_failure_stop")
        self.assertEqual(details["open_positions"], ["BTC/USDT:USDT"])


if __name__ == "__main__":
    unittest.main()
