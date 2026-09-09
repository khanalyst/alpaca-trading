"""Exact, bounded and crash-safe migration of the recorder's overlap cache."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deploy import recorder
from tests.test_deploy import _corpus_rows


class RecentKeyLayoutTests(unittest.TestCase):
    def fixture(self, root):
        corpus = root / "market.csv"
        rows = _corpus_rows(sessions=1, per_session=4)
        recorder._append_partitions(corpus, rows)
        recorder._save_index(corpus, recorder._scan_corpus(corpus))
        database = root / recorder.RECENT_KEY_INDEX_NAME
        with sqlite3.connect(database) as db:
            db.execute("DROP INDEX recent_keys_event_ts")
            db.execute("ALTER TABLE recent_keys RENAME TO current_keys")
            db.execute("CREATE TABLE recent_keys(event_key TEXT PRIMARY KEY, "
                       "event_ts TEXT NOT NULL) WITHOUT ROWID")
            db.execute("INSERT INTO recent_keys SELECT * FROM current_keys")
            db.execute("DROP TABLE current_keys")
            db.execute("CREATE INDEX recent_keys_event_ts ON recent_keys(event_ts)")
            db.execute("UPDATE metadata SET value=? WHERE key='schema'",
                       (recorder.LEGACY_RECENT_KEY_INDEX_SCHEMA,))
        index_file = root / recorder.INDEX_NAME
        index = json.loads(index_file.read_text())
        index["recent_key_index"]["schema"] = recorder.LEGACY_RECENT_KEY_INDEX_SCHEMA
        index_file.write_text(json.dumps(index))
        return corpus, database, index, rows

    def test_valid_legacy_cache_migrates_without_reading_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, database, before, rows = self.fixture(root)
            with patch.object(recorder, "_scan_corpus", side_effect=AssertionError(
                    "valid legacy cache must not scan market history")), \
                    patch.object(recorder, "_validated_corpus_rows", side_effect=AssertionError(
                        "migration must stream recent keys only")):
                after = recorder._prepare_index(corpus)
            expected = deepcopy(before)
            expected["recent_key_index"]["schema"] = recorder.RECENT_KEY_INDEX_SCHEMA
            self.assertEqual(after, expected)
            with recorder.RecentKeyIndex(database, read_only=True) as recent:
                self.assertEqual(recent.schema, recorder.RECENT_KEY_INDEX_SCHEMA)
                self.assertEqual(recent.count(), len(rows))
                self.assertTrue(all(recent.contains(row["event_key"]) for row in rows))
                self.assertEqual(recent.db.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(recent.db.execute("SELECT count(rowid) FROM recent_keys").fetchone()[0], len(rows))
            with patch.object(recorder, "_build_recent_key_index", side_effect=AssertionError(
                    "restart must not rebuild an already migrated cache")):
                self.assertEqual(recorder._prepare_index(corpus), after)

    def test_legacy_layout_is_not_mislabeled_by_a_normal_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, database, before, _rows = self.fixture(Path(directory))
            with recorder.RecentKeyIndex(database) as recent:
                result = recent.commit_cycle([], floor=None, signature="same-schema")
                self.assertEqual(result["schema"], recorder.LEGACY_RECENT_KEY_INDEX_SCHEMA)
                self.assertEqual(recent.metadata()["schema"], result["schema"])

    def test_replacement_failure_leaves_previous_exact_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, database, before, rows = self.fixture(root)
            replace = recorder.os.replace

            def fail_database(source, target):
                if Path(target) == database:
                    raise OSError("injected layout replacement failure")
                return replace(source, target)

            with patch.object(recorder.os, "replace", side_effect=fail_database):
                with self.assertRaisesRegex(OSError, "injected"):
                    recorder._prepare_index(corpus)
            self.assertEqual(recorder._load_index(corpus), before)
            with recorder.RecentKeyIndex(database, read_only=True) as recent:
                self.assertEqual(recent.count(), len(rows))
                self.assertTrue(all(recent.contains(row["event_key"]) for row in rows))
                self.assertEqual(recent.schema, recorder.LEGACY_RECENT_KEY_INDEX_SCHEMA)

    def test_crash_before_json_publication_recovers_from_authoritative_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, database, before, rows = self.fixture(root)
            replace = recorder.os.replace

            def fail_json(source, target):
                if Path(target).name == recorder.INDEX_NAME:
                    raise OSError("injected JSON publication failure")
                return replace(source, target)

            with patch.object(recorder.os, "replace", side_effect=fail_json):
                with self.assertRaisesRegex(OSError, "injected"):
                    recorder._prepare_index(corpus)
            self.assertIsNone(recorder._load_index(corpus))
            recovered = recorder._prepare_index(corpus)
            self.assertEqual(recovered["recent_key_index"]["schema"], recorder.RECENT_KEY_INDEX_SCHEMA)
            self.assertEqual(recovered["recent_key_index"]["count"], len(rows))
            self.assertEqual(recovered["partition_sources"], before["partition_sources"])
            self.assertEqual(recovered["session_calendar"], before["session_calendar"])

    def test_mismatched_legacy_count_cannot_authorize_cache_only_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, database, _before, rows = self.fixture(Path(directory))
            with sqlite3.connect(database) as db:
                db.execute("DELETE FROM recent_keys WHERE event_key=?", (rows[-1]["event_key"],))
            self.assertIsNone(recorder._load_index(corpus))
            with patch.object(recorder, "_scan_corpus", wraps=recorder._scan_corpus) as scan:
                repaired = recorder._prepare_index(corpus)
            scan.assert_called_once_with(corpus)
            self.assertEqual(repaired["recent_key_index"]["count"], len(rows))

    def test_corrupt_legacy_timestamp_falls_back_to_the_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, database, _before, rows = self.fixture(Path(directory))
            with sqlite3.connect(database) as db:
                db.execute("UPDATE recent_keys SET event_ts='not-a-timestamp' WHERE event_key=?",
                           (rows[-1]["event_key"],))
            with patch.object(recorder, "_scan_corpus", wraps=recorder._scan_corpus) as scan:
                repaired = recorder._prepare_index(corpus)
            scan.assert_called_once_with(corpus)
            self.assertEqual(repaired["recent_key_index"]["count"], len(rows))

    def test_invalid_index_or_primary_key_is_not_a_trusted_cache(self):
        for corrupt in ("missing_time_index", "wrong_primary_key", "nonunique_v2"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                corpus, database, _before, rows = self.fixture(root)
                with sqlite3.connect(database) as db:
                    db.execute("DROP INDEX recent_keys_event_ts")
                    if corrupt != "missing_time_index":
                        db.execute("ALTER TABLE recent_keys RENAME TO old_keys")
                        suffix = ", PRIMARY KEY(event_key,event_ts)) WITHOUT ROWID" if corrupt == "wrong_primary_key" else ")"
                        db.execute("CREATE TABLE recent_keys(event_key TEXT NOT NULL,event_ts TEXT NOT NULL" + suffix)
                        db.execute("INSERT INTO recent_keys SELECT * FROM old_keys")
                        db.execute("DROP TABLE old_keys")
                        db.execute("CREATE INDEX recent_keys_event_ts ON recent_keys(event_ts)")
                self.assertIsNone(recorder._load_index(corpus))
                repaired = recorder._prepare_index(corpus)
                self.assertEqual(repaired["recent_key_index"]["count"], len(rows))
                with recorder.RecentKeyIndex(database) as recent:
                    with self.assertRaisesRegex(RuntimeError, "repeats"):
                        recent.add_many([(rows[-1]["event_key"], rows[-1]["timestamp"])])


if __name__ == "__main__":
    unittest.main()
