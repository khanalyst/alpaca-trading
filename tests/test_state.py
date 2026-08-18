"""Durable paper-runtime identity and single-process ownership checks."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agent import journal
from agent import state
from agent import state_store


class StateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpaca-state-")
        state.configure_runtime("paper", Path(self.tmp.name))
        state.ensure_ready()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        state.configure_runtime("paper")
        self.tmp.cleanup()

    def test_runtime_lock_allows_exactly_one_owner(self):
        first = state.acquire_run_lock()
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(state.acquire_run_lock())
        finally:
            state.release_run_lock(first)
        second = state.acquire_run_lock()
        self.assertIsNotNone(second)
        state.release_run_lock(second)

    def test_paper_account_identity_cannot_change_silently(self):
        first = state.account_fingerprint("paper", "first-account")
        second = state.account_fingerprint("paper", "second-account")
        state.bind_account_identity(first)
        with self.assertRaises(state.RuntimeIdentityError):
            state.bind_account_identity(second)

    def test_corrupt_state_fails_closed_and_is_not_replaced(self):
        corrupt = "{not-json"
        state.STATE_FILE.write_text(corrupt, encoding="utf-8")
        with self.assertRaises(state.StateCorruptionError):
            state.load_state()
        with self.assertRaises(state.StateCorruptionError):
            state.bind_account_identity(
            state.account_fingerprint("paper", "paper-account"))
        self.assertEqual(state.STATE_FILE.read_text(encoding="utf-8"), corrupt)

    def test_journal_insert_preserves_state_corruption_failure(self):
        corrupt = "{not-json"
        state.STATE_FILE.write_text(corrupt, encoding="utf-8")
        with self.assertRaises(state.StateCorruptionError):
            state.log_event("event", "payload")
        self.assertEqual(state.STATE_FILE.read_text(encoding="utf-8"), corrupt)

    def test_persisted_state_missing_operator_pause_fails_closed(self):
        original = json.loads(state.STATE_FILE.read_text(encoding="utf-8"))
        original.pop("operator_pause")
        state.STATE_FILE.write_text(json.dumps(original), encoding="utf-8")
        persisted = state.STATE_FILE.read_text(encoding="utf-8")
        with self.assertRaises(state.StateCorruptionError):
            state.load_state()
        with self.assertRaises(state.StateCorruptionError):
            state.ensure_ready()
        self.assertEqual(state.STATE_FILE.read_text(encoding="utf-8"), persisted)

    def test_paper_and_live_state_are_mode_isolated(self):
        root = Path(self.tmp.name) / "scoped"
        state.configure_runtime("paper", root)
        state.ensure_ready()
        state.bind_account_identity(
            state.account_fingerprint("paper", "shared-key"))
        paper_path = state.STATE_FILE
        state.configure_runtime("live", root)
        state.ensure_ready()
        live = state.load_state()
        self.assertEqual(live["runtime_mode"], "live")
        self.assertIsNone(live["account_fingerprint"])
        state.bind_account_identity(
            state.account_fingerprint("live", "shared-key"))
        self.assertNotEqual(
            state.load_state()["account_fingerprint"],
            __import__("json").loads(paper_path.read_text())["account_fingerprint"])

    def test_transactional_update_preserves_unrelated_fields(self):
        state.commit({"operator_pause": True})
        updated = state.update_state(
            lambda current: {**current, "state": state.RUNNING})
        self.assertTrue(updated["operator_pause"])
        self.assertEqual(updated["state"], state.RUNNING)

    def test_store_primitives_are_facade_exports(self):
        for name in ("RUNNING", "PAUSED", "DAY_STOPPED", "KILLED", "DEFAULT",
                     "StateCorruptionError", "_validated", "_atomic_write", "_read"):
            self.assertIs(getattr(state, name), getattr(state_store, name))

    def test_validation_drops_unknown_fields_and_is_deepcopy_isolated(self):
        source = {"unknown": "ignored"}
        result = state_store._validated(source)
        self.assertNotIn("unknown", result)
        result["active_trades"]["AAPL"] = {"qty": 2}
        self.assertNotIn("AAPL", state_store.DEFAULT["active_trades"])
        fallback = {"active_trades": {}}
        loaded = state_store._read(Path(self.tmp.name) / "missing.json", fallback)
        loaded["active_trades"]["AAPL"] = {"qty": 1}
        self.assertNotIn("AAPL", fallback["active_trades"])

    def test_atomic_write_is_private_and_rejects_non_finite_json_without_replace(self):
        path = Path(self.tmp.name) / "atomic.json"
        state_store._atomic_write(path, {"state": state.PAUSED})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        original = path.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            state_store._atomic_write(path, {"value": float("nan")})
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_runtime_path_facade_globals_follow_each_configuration(self):
        first_root = Path(self.tmp.name) / "first"
        second_root = Path(self.tmp.name) / "second"

        state.configure_runtime("paper", first_root)
        self.assertEqual(state.RUNTIME_SCOPE, "paper")
        self.assertEqual(state.RUNTIME, first_root / "paper")
        self.assertEqual(state.STATE_FILE, first_root / "paper" / "state.json")
        self.assertEqual(state.PID_FILE, first_root / "paper" / "agent.pid")
        self.assertEqual(state.HEARTBEAT_FILE,
                         first_root / "paper" / "heartbeat.json")
        self.assertEqual(state.STATE_LOCK_FILE,
                         first_root / "paper" / "state.lock")
        self.assertEqual(state.EVENTS_FILE,
                         first_root / "paper" / "events.jsonl")
        self.assertEqual(state.JOURNAL_FILE,
                         first_root / "paper" / "journal.db")

        state.configure_runtime("live", second_root)
        self.assertEqual(state.RUNTIME_SCOPE, "live")
        self.assertEqual(state.RUNTIME, second_root / "live")
        self.assertEqual(state.STATE_FILE, second_root / "live" / "state.json")
        self.assertEqual(state.PID_FILE, second_root / "live" / "agent.pid")
        self.assertEqual(state.HEARTBEAT_FILE,
                         second_root / "live" / "heartbeat.json")
        self.assertEqual(state.STATE_LOCK_FILE,
                         second_root / "live" / "state.lock")
        self.assertEqual(state.EVENTS_FILE,
                         second_root / "live" / "events.jsonl")
        self.assertEqual(state.JOURNAL_FILE,
                         second_root / "live" / "journal.db")

    def test_same_runtime_rebind_preserves_sticky_observability(self):
        runtime = Path(self.tmp.name) / "sticky"
        state.configure_runtime("paper", runtime)
        state.mark_observability_degraded("config_audit_unavailable")
        state.configure_runtime("paper", runtime)
        self.assertEqual(state.OBSERVABILITY_DEGRADED_REASON,
                         "config_audit_unavailable")
        state.configure_runtime("paper", Path(self.tmp.name) / "other")
        self.assertIsNone(state.OBSERVABILITY_DEGRADED_REASON)

    def test_concurrent_journal_initialization_migrates_trade_key_once(self):
        runtime = Path(self.tmp.name) / "concurrent"
        state.configure_runtime("paper", runtime)

        def initialize():
            return state.initialize_journal()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _item: initialize(), range(12)))
        self.assertEqual(results, [state.JOURNAL_FILE] * 12)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
            indexes = [row[1] for row in db.execute("PRAGMA index_list(trades)")]
        self.assertIn("trade_id", columns)
        self.assertIn("trades_trade_id_unique", indexes)

    def test_initialize_journal_is_idempotent_and_migrates_legacy_schema(self):
        root = Path(self.tmp.name) / "legacy"
        state.configure_runtime("paper", root)
        state.RUNTIME.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            db.executescript(
                """
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    action TEXT
                );
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    symbol TEXT,
                    action TEXT,
                    qty REAL
                );
                """
            )
            db.execute("INSERT INTO events(ts, kind, payload) VALUES (?, ?, ?)",
                       (1.0, "legacy", "preserve me"))
            db.execute("INSERT INTO orders(ts, action) VALUES (?, ?)",
                       (2.0, "submit"))
            db.execute("INSERT INTO trades(ts, symbol, action, qty) VALUES (?, ?, ?, ?)",
                       (3.0, "SPY", "open", 2.0))
            db.commit()

        self.assertEqual(state.initialize_journal(), state.JOURNAL_FILE)
        # A second migration must not duplicate or rewrite existing rows.
        self.assertEqual(state.initialize_journal(), state.JOURNAL_FILE)

        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            event = db.execute(
                "SELECT ts, kind, payload FROM events ORDER BY id"
            ).fetchall()
            order = db.execute(
                "SELECT ts, action FROM orders ORDER BY id"
            ).fetchall()
            trade = db.execute(
                "SELECT ts, symbol, action, qty FROM trades ORDER BY id"
            ).fetchall()
            schema = db.execute(
                "SELECT value FROM schema_meta WHERE key = 'runtime_schema'"
            ).fetchone()
            columns = {
                table: {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for table in ("events", "orders", "trades", "equity")
            }

        self.assertEqual(event, [(1.0, "legacy", "preserve me")])
        self.assertEqual(order, [(2.0, "submit")])
        self.assertEqual(trade, [(3.0, "SPY", "open", 2.0)])
        self.assertEqual(schema, ("2",))
        self.assertTrue({"run_id", "cycle_id", "runtime_mode",
                         "account_fingerprint"}.issubset(columns["events"]))
        self.assertTrue({"reason", "run_id", "cycle_id", "runtime_mode",
                         "account_fingerprint", "setup_id", "execution_profile",
                         "vehicle", "reference_price", "entry_reference",
                         "exit_reference", "requested_qty", "planned_qty",
                         "fill_fraction"}.issubset(columns["orders"]))
        self.assertTrue({"run_id", "cycle_id", "runtime_mode",
                         "account_fingerprint", "execution_profile", "vehicle",
                         "reference_price", "entry_reference", "exit_reference",
                         "requested_qty", "planned_qty", "fill_fraction"}.issubset(columns["trades"]))
        self.assertTrue({"run_id", "cycle_id", "runtime_mode",
                         "account_fingerprint"}.issubset(columns["equity"]))

    def test_journal_ready_and_ensure_ready_form_explicit_readiness_contract(self):
        root = Path(self.tmp.name) / "readiness"
        state.configure_runtime("paper", root)
        self.assertTrue(state.journal_ready())
        self.assertFalse(state.STATE_FILE.exists())

        state.ensure_ready()
        self.assertTrue(state.STATE_FILE.exists())
        self.assertTrue(state.journal_ready())
        # Repeated startup checks are safe and preserve the validated state.
        before = state.STATE_FILE.read_text(encoding="utf-8")
        state.ensure_ready()
        self.assertEqual(state.STATE_FILE.read_text(encoding="utf-8"), before)

    def test_journal_readiness_failure_is_reported_as_not_ready(self):
        root = Path(self.tmp.name) / "unavailable"
        runtime = root / "paper"
        (runtime / "journal.db").mkdir(parents=True)
        state.configure_runtime("paper", root)

        self.assertFalse(state.journal_ready())
        with self.assertRaises(state.JournalNotReady):
            state.ensure_ready()

    def test_regular_file_runtime_path_is_not_ready(self):
        root = Path(self.tmp.name) / "regular-runtime"
        root.mkdir()
        (root / "paper").write_text("not a directory", encoding="utf-8")
        state.configure_runtime("paper", root)

        with self.assertRaises(state.JournalNotReady):
            state.initialize_journal()
        self.assertFalse(state.journal_ready())

    def test_runtime_error_from_sqlite_connect_is_normalized(self):
        with patch.object(state.sqlite3, "connect",
                          side_effect=RuntimeError("connect unavailable")):
            with self.assertRaises(state.JournalNotReady):
                state.initialize_journal()
            self.assertFalse(state.journal_ready())

    def test_runtime_error_from_insert_connection_is_normalized(self):
        with patch.object(state, "ensure_ready"), \
                patch.object(state.sqlite3, "connect",
                             side_effect=RuntimeError("connect unavailable")):
            with self.assertRaises(state.JournalNotReady):
                state._journal_insert("events", {
                    "ts": 1.0, "kind": "event", "payload": "payload",
                })

    def test_malformed_existing_schema_fails_closed_without_repair(self):
        root = Path(self.tmp.name) / "malformed"
        state.configure_runtime("paper", root)
        state.RUNTIME.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            db.execute(
                "CREATE TABLE events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts REAL NOT NULL, kind TEXT NOT NULL)"
            )
            db.commit()
        before = state.JOURNAL_FILE.read_bytes()

        self.assertFalse(state.journal_ready())
        self.assertEqual(state.JOURNAL_FILE.read_bytes(), before)
        with self.assertRaises(state.JournalNotReady):
            state.ensure_ready()
        self.assertEqual(state.JOURNAL_FILE.read_bytes(), before)

    def test_log_apis_all_intercept_through_journal_insert(self):
        order = SimpleNamespace(
            id="order-1", client_order_id="client-1", symbol="SPY",
            side="buy", qty=2, type="market", time_in_force="day",
            status="filled", filled_qty=2, filled_avg_price=101.5,
        )
        with patch.object(state, "_journal_insert") as insert, \
                patch.object(state, "_append") as append:
            state.log_event("heartbeat", "ok", run_id="run-1")
            state.log_order(order, action="submit", setup_id="setup-1")
            state.log_equity("123.45", "running", run_id="run-1")
            state.log_trade("SPY", "buy", "open", 2, price="101.5",
                            trade_id="trade-1")

        self.assertEqual([item.args[0] for item in insert.call_args_list],
                         ["events", "orders", "equity", "trades"])
        payloads = {item.args[0]: item.args[1] for item in insert.call_args_list}
        self.assertEqual(payloads["events"]["run_id"], "run-1")
        self.assertEqual(payloads["orders"]["setup_id"], "setup-1")
        self.assertEqual(payloads["equity"]["equity"], 123.45)
        self.assertEqual(payloads["trades"]["trade_id"], "trade-1")
        # Event/trade mirrors are attempted only after the journal call.
        self.assertEqual(append.call_count, 2)

    def test_sqlite_failure_prevents_event_and_trade_jsonl_mirrors(self):
        failure = state.JournalNotReady("journal unavailable")
        with patch.object(state, "_journal_insert", side_effect=failure), \
                patch.object(state, "_append") as append:
            with self.assertRaises(state.JournalNotReady):
                state.log_event("event", "payload")
            with self.assertRaises(state.JournalNotReady):
                state.log_trade("SPY", "buy", "open", 1)
        append.assert_not_called()
        self.assertFalse(state.EVENTS_FILE.exists())

    def test_jsonl_mirror_failure_does_not_mask_successful_sqlite_insert(self):
        with patch.object(state, "_append", side_effect=OSError("disk full")):
            state.log_event("event", "payload")
            state.log_trade("SPY", "buy", "open", 1)

        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            events = db.execute(
                "SELECT kind, payload FROM events ORDER BY id"
            ).fetchall()
            trades = db.execute(
                "SELECT symbol, action, qty FROM trades ORDER BY id"
            ).fetchall()
        self.assertEqual(events, [("event", "payload")])
        self.assertEqual(trades, [("SPY", "open", 1.0)])

    def test_latest_heartbeat_write_survives_history_append_failure(self):
        state.write_heartbeat("starting", sequence=1)
        with patch.object(state, "append_operational_history",
                          side_effect=OSError("history disk full")):
            latest = state.write_heartbeat("running", sequence=2)

        self.assertEqual(json.loads(state.HEARTBEAT_FILE.read_text(encoding="utf-8")),
                         latest)
        self.assertEqual(latest["status"], "running")
        self.assertEqual(latest["sequence"], 2)

    def test_best_effort_mirrors_ignore_non_os_errors(self):
        with patch.object(state, "append_operational_history",
                          side_effect=ValueError("history payload rejected")):
            latest = state.write_heartbeat("running", sequence=3)
        self.assertEqual(
            json.loads(state.HEARTBEAT_FILE.read_text(encoding="utf-8")),
            latest,
        )

        with patch.object(state, "_append",
                          side_effect=RuntimeError("event mirror unavailable")):
            state.log_event("event", "payload")
        with patch.object(state, "_append",
                          side_effect=ValueError("trade mirror rejected")):
            state.log_trade("SPY", "buy", "open", 1)

        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT kind, payload FROM events ORDER BY id DESC LIMIT 1"
                ).fetchone(),
                ("event", "payload"),
            )
            self.assertEqual(
                db.execute(
                    "SELECT symbol, action, qty FROM trades ORDER BY id DESC LIMIT 1"
                ).fetchone(),
                ("SPY", "open", 1.0),
            )

    def test_test_runtime_heartbeat_reports_scoped_mode(self):
        state.configure_runtime("test", Path(self.tmp.name) / "heartbeat-test")
        payload = state.write_heartbeat("starting")
        self.assertEqual(payload["runtime_mode"], "test")

    def test_journal_contract_symbols_and_connection_closure_are_available(self):
        self.assertTrue(issubclass(state.JournalNotReady, RuntimeError))
        self.assertTrue(issubclass(state._ClosingConnection, sqlite3.Connection))
        self.assertTrue({"events", "orders", "trades", "equity", "runs",
                         "schema_meta"}.issubset(state._JOURNAL_TABLES))

        path = Path(self.tmp.name) / "closed.sqlite3"
        db = sqlite3.connect(path, factory=state._ClosingConnection)
        with db:
            db.execute("CREATE TABLE sample (value INTEGER)")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

    def test_journal_facade_exports_preserve_module_identity(self):
        self.assertIs(state.JournalNotReady, journal.JournalNotReady)
        self.assertIs(state._ClosingConnection, journal._ClosingConnection)
        self.assertIs(state._JOURNAL_TABLES, journal._JOURNAL_TABLES)
        self.assertIs(state._JOURNAL_REQUIRED_COLUMNS,
                      journal._JOURNAL_REQUIRED_COLUMNS)
        self.assertIs(state._journal_columns, journal._journal_columns)
        self.assertIs(state._validate_existing_journal_schema,
                      journal._validate_existing_journal_schema)
        self.assertNotIn("state", journal.__dict__)

    def test_journal_wrappers_use_rebound_paths_and_sqlite_seams(self):
        root = Path(self.tmp.name) / "rebound"
        state.configure_runtime("paper", root)
        seen = []
        real_connect = sqlite3.connect

        def fake_connect(path, **kwargs):
            seen.append((Path(path), kwargs))
            return real_connect(path, **kwargs)

        with patch.object(state.sqlite3, "connect", side_effect=fake_connect):
            state.initialize_journal()
        self.assertEqual(seen[0][0], state.JOURNAL_FILE)
        self.assertIs(seen[0][1]["factory"], state._ClosingConnection)

    def test_journal_ready_calls_state_initializer_before_adapter_probe(self):
        order = []

        def initialize():
            order.append("initialize")
            return state.JOURNAL_FILE

        def ready(*args, **kwargs):
            order.append("ready")
            return True

        with patch.object(state, "initialize_journal", side_effect=initialize), \
                patch.object(journal, "journal_ready", side_effect=ready):
            self.assertTrue(state.journal_ready())
        self.assertEqual(order, ["initialize", "ready"])

    def test_journal_insert_calls_state_readiness_before_adapter_insert(self):
        order = []

        def ensure():
            order.append("ensure")

        def insert(*args, **kwargs):
            order.append("insert")

        with patch.object(state, "ensure_ready", side_effect=ensure), \
                patch.object(journal, "insert", side_effect=insert):
            state._journal_insert("events", {
                "ts": 1.0, "kind": "event", "payload": "payload",
            })
        self.assertEqual(order, ["ensure", "insert"])

    def test_journal_insert_filters_payload_to_existing_columns(self):
        state.initialize_journal()
        state._journal_insert("events", {
            "ts": 4.0,
            "kind": "filtered",
            "payload": "kept",
            "runtime_mode": "paper",
            "unknown_column": "discarded",
            123: "discarded",
        })

        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            row = db.execute(
                "SELECT ts, kind, payload, runtime_mode FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row, (4.0, "filtered", "kept", "paper"))


if __name__ == "__main__":
    unittest.main()
