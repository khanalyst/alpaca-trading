import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import state


class StateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.originals = (
            state.RUNTIME_SCOPE, state.RUNTIME, state.STATE_FILE,
            state.PID_FILE, state.DB_FILE, state.STATE_LOCK_FILE)
        self.original_context = state.journal_context()
        state._JOURNAL_CONTEXT.clear()
        runtime = Path(self.temp.name)
        state.RUNTIME_SCOPE = "test"
        state.RUNTIME = runtime
        state.STATE_FILE = runtime / "state.json"
        state.PID_FILE = runtime / "agent.pid"
        state.DB_FILE = runtime / "journal.db"
        state.STATE_LOCK_FILE = runtime / "state.lock"

    def tearDown(self):
        (state.RUNTIME_SCOPE, state.RUNTIME, state.STATE_FILE, state.PID_FILE,
         state.DB_FILE, state.STATE_LOCK_FILE) = self.originals
        state._JOURNAL_CONTEXT.clear()
        state._JOURNAL_CONTEXT.update(self.original_context)
        self.temp.cleanup()

    @staticmethod
    def _active_trade(entry_reason):
        return {
            "trade_id": "trade-1",
            "direction": "long",
            "opened_at": 1.0,
            "entry_price": 100.0,
            "entry_notional": 100.0,
            "qty": 1.0,
            "entry_reason": entry_reason,
        }

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

    def test_runtime_identity_is_bound_once_and_mismatch_fails_closed(self):
        state.RUNTIME_SCOPE = "demo"
        first = state.bind_runtime_identity("demo", "demo-key-a")
        self.assertEqual(
            state.load_state()["account_fingerprint"], first)

        with self.assertRaisesRegex(
                state.RuntimeIdentityError, "another OKX key"):
            state.bind_runtime_identity("demo", "demo-key-b")

        persisted = state.load_state()
        self.assertEqual(persisted["runtime_mode"], "demo")
        self.assertEqual(persisted["account_fingerprint"], first)

    def test_runtime_scope_must_match_configured_mode(self):
        state.RUNTIME_SCOPE = "live"
        with self.assertRaisesRegex(
                state.RuntimeIdentityError, "not demo"):
            state.bind_runtime_identity("demo", "demo-key")

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

    def test_active_trade_entry_reason_allows_300_characters(self):
        valid = dict(state.DEFAULT)
        valid["state"] = state.RUNNING
        reason = "r" * 300
        valid["active_trades"] = {
            "BTC/USDT:USDT": self._active_trade(reason),
        }

        state.save_state(valid)

        self.assertEqual(
            state.load_state()["active_trades"]["BTC/USDT:USDT"]
            ["entry_reason"],
            reason,
        )

    def test_active_trade_entry_reason_rejects_301_characters(self):
        invalid = dict(state.DEFAULT)
        invalid["state"] = state.RUNNING
        invalid["active_trades"] = {
            "BTC/USDT:USDT": self._active_trade("r" * 301),
        }

        with self.assertRaisesRegex(ValueError, "entry_reason"):
            state.save_state(invalid)

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

    def test_legacy_journal_is_migrated_in_place_without_losing_rows(self):
        state.RUNTIME.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(state.DB_FILE)
        db.execute(
            "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT)")
        db.execute(
            "CREATE TABLE trades ("
            "ts REAL, symbol TEXT, side TEXT, action TEXT, qty REAL, "
            "price REAL, notional REAL, leverage REAL, reason TEXT)")
        db.execute(
            "CREATE TABLE equity (ts REAL, equity REAL, state TEXT)")
        db.execute(
            "INSERT INTO events VALUES (1, 'legacy', 'kept')")
        db.execute(
            "INSERT INTO trades VALUES "
            "(1, 'BTC/USDT:USDT', 'buy', 'open', 1, 100, 100, 2, 'kept')")
        db.execute(
            "INSERT INTO equity VALUES (1, 1000, 'RUNNING')")
        db.commit()
        db.close()

        with state._db() as migrated:
            trade_columns = {
                row[1] for row in migrated.execute(
                    "PRAGMA table_info(trades)")}
            equity_columns = {
                row[1] for row in migrated.execute(
                    "PRAGMA table_info(equity)")}
            self.assertIn("strategy_id", trade_columns)
            self.assertIn("pnl_semantics", trade_columns)
            self.assertIn("basis_id", equity_columns)
            self.assertEqual(
                migrated.execute(
                    "SELECT payload FROM events WHERE kind='legacy'"
                ).fetchone()[0],
                "kept",
            )
            self.assertEqual(
                migrated.execute(
                    "SELECT reason FROM trades"
                ).fetchone()[0],
                "kept",
            )
            self.assertEqual(
                migrated.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key='journal_schema_version'"
                ).fetchone()[0],
                str(state.JOURNAL_SCHEMA_VERSION),
            )

    def test_journal_context_tags_trade_and_equity_rows(self):
        state.set_journal_context(
            run_id="run-a", cycle_id="cycle-a",
            strategy_id="momentum", strategy_version="v1",
            prompt_version="p1", config_version="cfg1",
            code_version="code1", equity_basis_id="basis-a",
            runtime_mode="demo", account_fingerprint="okx-demo-test")
        state.log_trade(
            "BTC/USDT:USDT", "buy", "open", 1, 100, 100, 2,
            "test", trade_id="trade-a")
        state.log_equity(1_000, state.RUNNING)

        with state._db() as db:
            trade = db.execute(
                "SELECT run_id, cycle_id, strategy_id, equity_basis_id, "
                "runtime_mode, account_fingerprint "
                "FROM trades").fetchone()
            equity = db.execute(
                "SELECT run_id, cycle_id, basis_id, runtime_mode, "
                "account_fingerprint FROM equity").fetchone()
        self.assertEqual(
            trade, ("run-a", "cycle-a", "momentum", "basis-a",
                    "demo", "okx-demo-test"))
        self.assertEqual(
            equity, ("run-a", "cycle-a", "basis-a",
                     "demo", "okx-demo-test"))

    def test_non_finite_setup_memory_is_rejected(self):
        invalid = dict(state.DEFAULT)
        invalid["recent_setups"] = {
            "setup-a": {
                "strategy_id": "momentum",
                "strategy_version": "v1",
                "setup_key": "key-a",
                "setup_type": "trend_continuation",
                "symbol": "BTC/USDT:USDT",
                "direction": "long",
                "signal_ts": float("nan"),
                "first_seen_at": 1.0,
                "last_seen_at": 1.0,
                "blocked_until": 0.0,
                "expires_at": 2.0,
                "status": "proposed",
            },
        }
        with self.assertRaisesRegex(ValueError, "recent_setups"):
            state.save_state(invalid)


if __name__ == "__main__":
    unittest.main()
