"""The deployed shadow process keeps its SQLite WAL reader mount alive."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from deploy import shadow as shadow_service


class ShadowWalLifetimeTests(unittest.TestCase):
    def test_missing_initialized_database_prevents_polling(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                shadow_service, "ShadowRunner") as runner, patch.object(
                shadow_service, "_write_health") as health:
            path = Path(directory) / "missing.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                shadow_service.main([
                    "--no-diagnostic", "--once", "--shadow-db", str(path)])
            runner.return_value.run_once.assert_not_called()
            health.assert_not_called()
            self.assertFalse(path.exists())

    def test_anchor_preserves_sidecars_and_readers_see_new_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.sqlite3"
            writer = sqlite3.connect(path)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE observations (value TEXT NOT NULL)")
            writer.commit()
            writer.close()

            anchor = shadow_service._open_shadow_wal_anchor(path)
            try:
                self.assertIsNone(anchor.isolation_level)
                self.assertEqual(anchor.execute(
                    "PRAGMA query_only").fetchone()[0], 1)
                self.assertFalse(anchor.in_transaction)

                writer = sqlite3.connect(path)
                writer.execute("INSERT INTO observations VALUES ('new')")
                writer.commit()
                writer.close()

                self.assertTrue(path.with_name(f"{path.name}-wal").is_file())
                self.assertTrue(path.with_name(f"{path.name}-shm").is_file())

                # The consumer cannot create or write sidecars; the writer's
                # anchor must make a normal SQLite read possible nonetheless.
                protected = [path, path.with_name(f"{path.name}-wal"),
                             path.with_name(f"{path.name}-shm"), path.parent]
                modes = {item: item.stat().st_mode & 0o777 for item in protected}
                for item in protected:
                    item.chmod(0o555 if item.is_dir() else 0o444)
                try:
                    reader = sqlite3.connect(
                        f"{path.resolve().as_uri()}?mode=ro", uri=True)
                    try:
                        self.assertEqual(reader.execute(
                            "SELECT value FROM observations").fetchall(),
                            [("new",)])
                        self.assertFalse(reader.in_transaction)
                    finally:
                        reader.close()
                finally:
                    for item, mode in modes.items():
                        item.chmod(mode)
            finally:
                anchor.close()

    def _run_main_with_tracking_anchor(self, *, once: bool,
                                       interrupt: bool = False,
                                       error: bool = False) -> tuple[int | None, object]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.sqlite3"
            opened: list[sqlite3.Connection] = []

            class Runner:
                def __init__(self, config):
                    db = sqlite3.connect(config.shadow_db)
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("CREATE TABLE marker (value TEXT)")
                    db.commit()
                    db.close()
                    self.store = SimpleNamespace(path=config.shadow_db)

                def run_once(self):
                    if interrupt:
                        raise KeyboardInterrupt()
                    if error:
                        raise RuntimeError("stop")
                    return {"candidate_errors": {}}

            original_anchor = shadow_service._open_shadow_wal_anchor

            class TrackingConnection:
                def __init__(self, connection):
                    self.connection = connection
                    self.closed = False

                def close(self):
                    self.closed = True
                    self.connection.close()

            def open_anchor(anchor_path):
                tracked = TrackingConnection(original_anchor(anchor_path))
                opened.append(tracked)
                return tracked

            args = ["--no-diagnostic", "--shadow-db", str(path)]
            if once:
                args.append("--once")
            with patch.object(shadow_service, "ShadowRunner", Runner), \
                 patch.object(shadow_service, "_open_shadow_wal_anchor",
                              side_effect=open_anchor), \
                 patch.object(shadow_service, "_write_health"), \
                 patch.object(shadow_service, "_record_acceptance",
                              return_value={}), \
                 patch("builtins.print"):
                if interrupt:
                    with self.assertRaises(KeyboardInterrupt):
                        shadow_service.main(args)
                    result = None
                else:
                    result = shadow_service.main(args)
            return result, opened[0]

    def test_once_success_closes_anchor(self):
        result, anchor = self._run_main_with_tracking_anchor(once=True)
        self.assertEqual(result, 0)
        self.assertTrue(anchor.closed)

    def test_once_error_closes_anchor(self):
        result, anchor = self._run_main_with_tracking_anchor(once=True, error=True)
        self.assertEqual(result, 1)
        self.assertTrue(anchor.closed)

    def test_unhandled_exit_closes_anchor(self):
        result, anchor = self._run_main_with_tracking_anchor(
            once=False, interrupt=True)
        self.assertIsNone(result)
        self.assertTrue(anchor.closed)


if __name__ == "__main__":
    unittest.main()
