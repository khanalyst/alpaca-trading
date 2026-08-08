"""Regression coverage for SQLite handles owned by research persistence."""

import gc
import sqlite3
import tempfile
import unittest
import warnings
from contextlib import closing
from pathlib import Path

from agent.staging import StagingStore
from research import backup
from research.findings import FindingsStore
from research.prices import MINUTE_MS, PriceCache, day_of
from tests.agent.test_bounded_staging_lifecycle import proposal


class SqliteLifecycleHygieneTests(unittest.TestCase):
    def test_backup_findings_and_staging_release_every_connection(self):
        with tempfile.TemporaryDirectory() as directory, \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            gc.collect()
            caught.clear()
            root = Path(directory)
            source = root / "source.db"
            with closing(sqlite3.connect(source)) as conn, conn:
                conn.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
                conn.execute("INSERT INTO evidence VALUES ('durable')")

            copied = root / "copied.db"
            backup._online_sqlite_backup(source, copied)
            self.assertTrue(backup._sqlite_verification(copied)["ok"])

            findings = FindingsStore(root / "findings.db")
            self.assertGreater(findings.schema_version(), 0)
            self.assertTrue(findings.backup(root / "findings-copy.db").is_file())

            staging = StagingStore(root / "staging.db")
            staging.register(proposal(), generation=0, now=1)
            self.assertEqual(staging.summary()["active"], 1)
            staging.retire("crowded-carry", "lifecycle complete", now=2)
            self.assertEqual(staging.summary()["retired"], 1)

            del staging, findings
            gc.collect()
            unclosed_databases = [
                warning for warning in caught
                if "unclosed database" in str(warning.message).lower()
            ]
            self.assertEqual(unclosed_databases, [])

    def test_price_cache_releases_read_and_write_connections(self):
        with tempfile.TemporaryDirectory() as directory, \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            gc.collect()
            caught.clear()
            cache = PriceCache(Path(directory) / "prices.db")
            timestamp = 1_759_968_000_000
            symbol = "BTC/USDT:USDT"
            cache.store(symbol, day_of(timestamp), [
                [timestamp, 100, 101, 99, 100.5, 10],
            ])
            self.assertEqual(cache.bars(
                symbol, timestamp, timestamp + MINUTE_MS)[0].close, 100.5)
            self.assertTrue(cache.covers(
                symbol, timestamp, timestamp + MINUTE_MS))

            del cache
            gc.collect()
            unclosed_databases = [
                warning for warning in caught
                if "unclosed database" in str(warning.message).lower()
            ]
            self.assertEqual(unclosed_databases, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
