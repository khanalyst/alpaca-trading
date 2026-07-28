"""B0.4: the new attribution columns arrive without losing a row.

The journal is the corpus. A migration that dropped rows would not announce
itself - the replay would simply run against less data and report tighter
numbers on a smaller sample, which looks like a better result.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent import state


class JournalMigrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(state.configure_runtime, "_unconfigured")
        state.configure_runtime("test", base=Path(self.dir.name))

    def _legacy_journal(self) -> None:
        """A journal written before B0 existed: no attribution columns."""
        state.RUNTIME.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(state.DB_FILE)
        db.execute("CREATE TABLE events (ts REAL, kind TEXT, payload TEXT)")
        db.execute(
            "CREATE TABLE trades ("
            "ts REAL, symbol TEXT, side TEXT, action TEXT, qty REAL, "
            "price REAL, notional REAL, leverage REAL, reason TEXT)")
        db.execute("CREATE TABLE equity (ts REAL, equity REAL, state TEXT)")
        db.execute("INSERT INTO events VALUES (1, 'llm_input', 'corpus')")
        db.execute(
            "INSERT INTO trades VALUES "
            "(1, 'BTC/USDT:USDT', 'buy', 'open', 1, 100, 100, 2, 'kept')")
        db.commit()
        db.close()

    def test_variant_columns_are_added_in_place(self):
        self._legacy_journal()

        with state._db() as migrated:
            events = {row[1] for row in
                      migrated.execute("PRAGMA table_info(events)")}
            trades = {row[1] for row in
                      migrated.execute("PRAGMA table_info(trades)")}

        self.assertIn("variant_id", events)
        self.assertIn("strategy_config_version", events)
        self.assertIn("variant_id", trades)
        self.assertIn("strategy_config_version", trades)

    def test_no_row_is_lost(self):
        self._legacy_journal()

        with state._db() as migrated:
            self.assertEqual(
                migrated.execute(
                    "SELECT payload FROM events").fetchone()[0], "corpus")
            self.assertEqual(
                migrated.execute(
                    "SELECT reason FROM trades").fetchone()[0], "kept")

    def test_pre_existing_rows_carry_null_not_a_guess(self):
        """Rows written before attribution existed must not claim to be live.

        Back-filling them with "live" would be the more convenient choice and
        the wrong one: it would assert an attribution nobody recorded.
        """
        self._legacy_journal()

        with state._db() as migrated:
            self.assertIsNone(
                migrated.execute(
                    "SELECT variant_id FROM events").fetchone()[0])

    def test_new_events_carry_the_context_variant(self):
        try:
            state.set_journal_context(
                run_id="run-a", variant_id="live",
                strategy_config_version="sfp1")
            state.log_event("decisions", "[]")

            with state._db() as db:
                row = db.execute(
                    "SELECT variant_id, strategy_config_version "
                    "FROM events WHERE kind='decisions'").fetchone()
            self.assertEqual(row[0], "live")
            self.assertEqual(row[1], "sfp1")
        finally:
            state.set_journal_context(
                run_id=None, variant_id=None, strategy_config_version=None)

    def test_an_explicit_variant_id_overrides_the_context(self):
        """Shadow and replay writers name themselves, so nothing pools them."""
        try:
            state.set_journal_context(variant_id="live")
            state.log_event(
                "shadow_decision", "{}", variant_id="momentum.rr.fixed_2_5")

            with state._db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT variant_id FROM events "
                        "WHERE kind='shadow_decision'").fetchone()[0],
                    "momentum.rr.fixed_2_5")
        finally:
            state.set_journal_context(variant_id=None)

    def test_trades_carry_the_context_variant(self):
        try:
            state.set_journal_context(
                variant_id="live", strategy_config_version="sfp1")
            state.log_trade(
                "BTC/USDT:USDT", "buy", "open", 1, 100, 100, 2, "entry")

            with state._db() as db:
                row = db.execute(
                    "SELECT variant_id, strategy_config_version "
                    "FROM trades").fetchone()
            self.assertEqual(row[0], "live")
            self.assertEqual(row[1], "sfp1")
        finally:
            state.set_journal_context(
                variant_id=None, strategy_config_version=None)

    def test_schema_version_records_the_migration(self):
        self._legacy_journal()

        with state._db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT value FROM schema_meta "
                    "WHERE key='journal_schema_version'").fetchone()[0],
                str(state.JOURNAL_SCHEMA_VERSION))
        self.assertGreaterEqual(state.JOURNAL_SCHEMA_VERSION, 4)


if __name__ == "__main__":
    unittest.main()
