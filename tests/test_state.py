import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import state


class StateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.originals = (
            state.RUNTIME, state.STATE_FILE, state.PID_FILE, state.DB_FILE)
        runtime = Path(self.temp.name)
        state.RUNTIME = runtime
        state.STATE_FILE = runtime / "state.json"
        state.PID_FILE = runtime / "agent.pid"
        state.DB_FILE = runtime / "journal.db"

    def tearDown(self):
        (state.RUNTIME, state.STATE_FILE, state.PID_FILE,
         state.DB_FILE) = self.originals
        self.temp.cleanup()

    def test_corrupt_state_is_preserved_and_forces_killed(self):
        state.RUNTIME.mkdir(parents=True, exist_ok=True)
        state.STATE_FILE.write_text("{not-json")

        loaded = state.load_state()

        self.assertEqual(loaded["state"], state.KILLED)
        self.assertTrue(loaded["operator_pause"])
        self.assertIn("state file was corrupt", loaded["kill_reason"])
        backups = list(state.RUNTIME.glob("state.corrupt.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{not-json")
        persisted = json.loads(state.STATE_FILE.read_text())
        self.assertEqual(persisted["state"], state.KILLED)

    def test_invalid_state_cannot_be_saved(self):
        invalid = dict(state.DEFAULT)
        invalid["state"] = "RUNNIG"
        with self.assertRaisesRegex(ValueError, "invalid state"):
            state.save_state(invalid)

    def test_valid_json_with_malformed_trade_metadata_fails_closed(self):
        state.RUNTIME.mkdir(parents=True, exist_ok=True)
        malformed = dict(state.DEFAULT)
        malformed["state"] = state.RUNNING
        malformed["active_trades"] = {
            "BTC/USDT:USDT": {"trade_id": "x", "direction": "sideways"}
        }
        state.STATE_FILE.write_text(json.dumps(malformed))
        loaded = state.load_state()
        self.assertEqual(loaded["state"], state.KILLED)

    def test_unknown_equity_basis_fails_closed(self):
        invalid = dict(state.DEFAULT)
        invalid["equity_basis"] = "account_total_usd"
        with self.assertRaisesRegex(ValueError, "equity_basis"):
            state.save_state(invalid)

    def test_liquidity_feedback_survives_restart(self):
        valid = dict(state.DEFAULT)
        valid["entry_feedback"] = {
            "KAITO/USDT:USDT": {
                "reason": "insufficient_depth",
                "direction": "long",
                "last_rejected_at": 1000.0,
                "expires_at": 2800.0,
                "blocked_until": 1900.0,
                "consecutive_rejections": 2,
                "requested_contracts": 28_530.0,
                "available_contracts": 11_015.0,
                "requested_notional_usdt": 28_530.0,
                "available_notional_usdt": 11_015.0,
                "available_ratio": 11_015 / 28_530,
                "max_retry_size_pct_equity": 4.2,
                "max_slippage_pct": 0.35,
            },
        }

        state.save_state(valid)

        loaded = state.load_state()
        self.assertEqual(
            loaded["entry_feedback"]["KAITO/USDT:USDT"]
            ["consecutive_rejections"], 2)

    def test_non_finite_liquidity_feedback_is_rejected(self):
        invalid = dict(state.DEFAULT)
        invalid["entry_feedback"] = {
            "KAITO/USDT:USDT": {
                "reason": "insufficient_depth",
                "direction": "long",
                "last_rejected_at": 1000.0,
                "expires_at": 2800.0,
                "blocked_until": 0.0,
                "consecutive_rejections": 1,
                "requested_contracts": 100.0,
                "available_contracts": 50.0,
                "requested_notional_usdt": 100.0,
                "available_notional_usdt": 50.0,
                "available_ratio": 0.5,
                "max_retry_size_pct_equity": float("nan"),
                "max_slippage_pct": 0.35,
            },
        }
        with self.assertRaisesRegex(ValueError, "entry_feedback"):
            state.save_state(invalid)

    def test_execution_failure_backoff_survives_restart(self):
        valid = dict(state.DEFAULT)
        valid["entry_failures"] = {
            "CL/USDT:USDT": {
                "reason": "exchange_rejected",
                "direction": "long",
                "stage": "attached_entry",
                "classification": "permanent",
                "error_code": "51001",
                "error_message": "Instrument does not exist",
                "last_failed_at": 1000.0,
                "blocked_until": 4600.0,
                "expires_at": 15400.0,
                "consecutive_failures": 2,
            },
        }

        state.save_state(valid)

        loaded = state.load_state()
        failure = loaded["entry_failures"]["CL/USDT:USDT"]
        self.assertEqual(failure["classification"], "permanent")
        self.assertEqual(failure["error_code"], "51001")

    def test_invalid_execution_failure_backoff_is_rejected(self):
        invalid = dict(state.DEFAULT)
        invalid["entry_failures"] = {
            "AAVE/USDT:USDT": {
                "reason": "exchange_rejected",
                "direction": "long",
                "stage": "attached_entry",
                "classification": "transient",
                "error_code": None,
                "error_message": "temporary",
                "last_failed_at": 1000.0,
                "blocked_until": float("nan"),
                "expires_at": 2000.0,
                "consecutive_failures": 1,
            },
        }
        with self.assertRaisesRegex(ValueError, "entry_failures"):
            state.save_state(invalid)

    def test_only_one_agent_run_lock_can_be_held(self):
        first = state.acquire_run_lock()
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(state.acquire_run_lock())
        finally:
            state.release_run_lock(first)
        second = state.acquire_run_lock()
        self.assertIsNotNone(second)
        state.release_run_lock(second)

    def test_keep_positions_intent_is_durable(self):
        saved = state.set_state(
            state.KILLED, "operator", flatten_on_kill=False,
            operator_pause=True)
        self.assertFalse(saved["flatten_on_kill"])
        self.assertFalse(state.load_state()["flatten_on_kill"])

    def test_journal_failure_is_never_silently_swallowed(self):
        with patch("agent.state._db", side_effect=OSError("disk full")):
            with self.assertRaises(state.JournalError):
                state.log_event("test", "payload")

    def test_journal_preflight_verifies_writes_without_leaving_an_event(self):
        state.check_journal()
        with state._db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind='journal_preflight'"
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
