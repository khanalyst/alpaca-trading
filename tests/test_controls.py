import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import main
from agent import state
from agent.engine import Engine
from tests.helpers import valid_config


class CredentialFreeControlTests(unittest.TestCase):
    @patch("agent.exchange.Exchange")
    @patch("main.state.pid_alive", return_value=False)
    @patch("main.state.read_pid", return_value=None)
    @patch("main.state.load_state")
    def test_status_hides_legacy_benchmarks_until_rebased(
            self, load_state, read_pid, pid_alive, exchange_cls):
        load_state.return_value = {
            "state": state.RUNNING,
            "high_water_mark": 81_000.06,
            "day_start_equity": 81_000.06,
            "equity_basis": None,
            "kill_reason": None,
        }
        exchange_cls.return_value.equity_usdt.return_value = 72_232.06
        exchange_cls.return_value.positions.return_value = []
        output = StringIO()

        with redirect_stdout(output):
            self.assertEqual(main.cmd_status(Mock(), valid_config()), 0)

        shown = output.getvalue()
        self.assertIn("Equity: 72,232.06 USDT", shown)
        self.assertIn("pending one-time USDT-only rebase", shown)
        self.assertNotIn("Day PnL", shown)
        self.assertNotIn("Drawdown", shown)

    @patch("main.setup_logging")
    @patch("main.state.set_state")
    def test_pause_works_without_env_file(self, set_state, setup_logging):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / ".env"
            with patch.object(main, "ENV_FILE", missing), \
                    patch.object(sys, "argv", ["main.py", "pause"]):
                self.assertEqual(main.main(), 0)
        set_state.assert_called_once_with(state.PAUSED, operator_pause=True)

    @patch("main.setup_logging")
    @patch("main.state.set_state")
    def test_keep_positions_kill_works_without_env_file(
            self, set_state, setup_logging):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / ".env"
            with patch.object(main, "ENV_FILE", missing), patch.object(
                    sys, "argv", ["main.py", "kill", "--keep-positions"]):
                self.assertEqual(main.main(), 0)
        self.assertFalse(set_state.call_args.kwargs["flatten_on_kill"])
        self.assertTrue(set_state.call_args.kwargs["operator_pause"])

    def test_live_env_permissions_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("OKX_API_KEY=test\n")
            env_file.chmod(0o644)
            with patch.object(main, "ENV_FILE", env_file):
                with self.assertRaisesRegex(PermissionError, "chmod 600"):
                    main.load_secrets("live")

    def test_shell_secret_missing_from_env_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("OKX_API_KEY=from-file\n")
            env_file.chmod(0o600)
            with patch.object(main, "ENV_FILE", env_file), patch.dict(
                    os.environ, {"OPENAI_API_KEY": "from-shell"}, clear=True):
                main.load_secrets("demo")
                self.assertEqual(os.getenv("OKX_API_KEY"), "from-file")
                self.assertIsNone(os.getenv("OPENAI_API_KEY"))

    @patch("main.state.release_run_lock")
    @patch("main._flatten", return_value=True)
    @patch("main.load_exchange_cfg", return_value=valid_config())
    @patch("main.state.acquire_run_lock", return_value=object())
    @patch("main.state.set_state")
    def test_kill_holds_run_lock_during_direct_flatten(
            self, set_state, acquire, load_cfg, flatten, release):
        args = Mock(keep_positions=False, config="config.yaml")

        self.assertEqual(main.cmd_kill(args, None), 0)

        flatten.assert_called_once()
        release.assert_called_once_with(acquire.return_value)

    @patch("main._flatten")
    @patch("main.state.acquire_run_lock", return_value=None)
    @patch("main.state.set_state")
    def test_kill_leaves_flatten_to_the_locked_running_loop(
            self, set_state, acquire, flatten):
        args = Mock(keep_positions=False, config="config.yaml")

        self.assertEqual(main.cmd_kill(args, None), 0)

        flatten.assert_not_called()


class KillSemanticsTests(unittest.TestCase):
    @patch("agent.engine.state.log_event")
    def test_legacy_equity_benchmarks_rebase_once(self, log_event):
        st = {
            "state": state.RUNNING,
            "high_water_mark": 81_000.06,
            "day_start_equity": 81_000.06,
            "day": "2026-07-22",
            "last_ledger_ts": 1,
        }
        now = 1_753_228_800.0

        changed = Engine._ensure_equity_basis(st, 72_232.06, now)

        self.assertTrue(changed)
        self.assertEqual(st["equity_basis"], state.EQUITY_BASIS)
        self.assertEqual(st["high_water_mark"], 72_232.06)
        self.assertEqual(st["day_start_equity"], 72_232.06)
        self.assertEqual(st["last_ledger_ts"], int(now * 1000))
        self.assertEqual(log_event.call_args.args[0],
                         "equity_basis_migration")

        self.assertFalse(Engine._ensure_equity_basis(st, 72_100, now + 60))
        self.assertEqual(st["high_water_mark"], 72_232.06)
        self.assertEqual(log_event.call_count, 1)

    @patch("agent.engine.state.load_state")
    def test_keep_positions_kill_exits_without_flattening(self, load_state):
        load_state.return_value = {
            "state": state.KILLED, "kill_reason": "manual kill",
            "flatten_on_kill": False, "operator_pause": True,
        }
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.alerts = Mock()
        engine.ex = Mock()
        engine.flatten_all = Mock()

        engine.run(run_lock=object())

        engine.flatten_all.assert_not_called()

    @patch("agent.engine.state.set_state")
    @patch("agent.engine.state.load_state")
    def test_single_journal_failure_pauses_and_stops_loop(
            self, load_state, set_state):
        load_state.return_value = {
            "state": state.RUNNING, "kill_reason": None,
            "flatten_on_kill": True, "operator_pause": False,
        }
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.alerts = Mock()
        engine.ex = Mock()
        engine._credential_failures = 0
        engine.cycle = Mock(side_effect=state.JournalError("disk full"))

        engine.run(run_lock=object())

        set_state.assert_called_once()
        self.assertEqual(set_state.call_args.args[0], state.PAUSED)
        self.assertTrue(set_state.call_args.kwargs["operator_pause"])
        self.assertEqual(engine.alerts.send.call_args.args[1],
                         "journal_failure_stop")

    @patch("agent.engine.state.load_state", return_value={"state": state.KILLED})
    @patch("agent.engine.state.log_equity")
    @patch("agent.engine.state.commit")
    def test_drawdown_kill_is_durable_before_flatten_attempt(
            self, commit, log_equity, load_state):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.alerts = Mock()
        engine.ex = Mock()
        engine.ex.equity_usdt.return_value = 80
        engine.ex.positions.return_value = []
        engine.ex.transfers_since.return_value = (0, 1)
        engine._reconcile_positions = Mock(return_value=[])
        engine._startup_reconciled = False

        def fail_flatten(reason):
            kill_calls = [call for call in commit.call_args_list
                          if call.kwargs.get("kill")]
            self.assertTrue(kill_calls)
            raise RuntimeError("exchange unavailable")

        engine.flatten_all = Mock(side_effect=fail_flatten)
        st = {
            "state": state.RUNNING,
            "high_water_mark": 100,
            "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "day_start_equity": 100,
            "equity_basis": state.EQUITY_BASIS,
            "last_ledger_ts": 0,
            "cooldowns": {}, "opened_at": {}, "active_trades": {},
            "protection": {},
        }

        with self.assertRaisesRegex(RuntimeError, "exchange unavailable"):
            engine.cycle(st)

        self.assertIn("max drawdown", commit.call_args_list[-1].kwargs["kill"])


if __name__ == "__main__":
    unittest.main()
