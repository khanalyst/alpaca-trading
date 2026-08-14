import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import strategy_factory as factory_module
from research.strategy_factory import run_factory

from tests.research.test_strategy_factory import losing_breakouts


def _write_rows(path: Path, rows) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
                    encoding="utf-8")


class FactoryWorkerViewTests(unittest.TestCase):
    def _corpus(self):
        rows = losing_breakouts(sessions=14)
        # Keep quotes in the full validation/hash corpus while the worker view
        # contains only replayable bars.  This exercises the shared resolver.
        quotes = []
        for row in rows:
            if str(row.get("kind", "bar")).lower() not in {"bar", "bar_1m"}:
                continue
            quote = {
                "kind": "quote", "provider": row.get("provider", "test"),
                "feed": row.get("feed", "sip"), "symbol": row["symbol"],
                "timestamp": row["timestamp"], "as_of": row["as_of"],
                "observed_at": row.get("observed_at", row["as_of"]),
                "bid": float(row["close"]) - .01,
                "ask": float(row["close"]) + .01,
            }
            quotes.append(quote)
        return rows + quotes

    def _run(self, source, *, db, worker_data=None, progress_callback=None):
        return run_factory(
            source, worker_data=worker_data, db_path=db, strategies=1,
            variants_per_strategy=2, workers=1, min_trades=1,
            min_sessions=1, alpha=1.0, progress_callback=progress_callback)

    def test_worker_view_preserves_gates_and_avoids_quote_rescans(self):
        rows = self._corpus()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full = root / "full.jsonl"
            replay = root / "replay.jsonl"
            _write_rows(full, rows)
            _write_rows(replay, [row for row in rows
                                 if str(row.get("kind", "bar")).lower() != "quote"])
            original_slice = factory_module.corpus_slice
            seen_sources = []

            def observe_slice(source, **kwargs):
                seen_sources.append(Path(source))
                return original_slice(source, **kwargs)

            # The thread fallback keeps this assertion in-process while still
            # exercising both diagnosis and evaluation worker paths.
            open_calls = []
            original_open = factory_module.SQLiteQuoteIndex.open_read_only
            def observe_open(descriptor):
                open_calls.append(descriptor)
                return original_open(descriptor)
            with patch.object(factory_module, "ProcessPoolExecutor",
                              side_effect=OSError), \
                 patch.object(factory_module, "corpus_slice",
                              side_effect=observe_slice), \
                 patch.object(factory_module.SQLiteQuoteIndex, "open_read_only",
                              side_effect=observe_open):
                replay_result = self._run(
                    full, worker_data=replay, db=root / "replay.sqlite3")
            self.assertEqual(len(open_calls), 2,
                             "diagnosis and evaluation should share read-only opens")
            self.assertTrue(seen_sources)
            self.assertTrue(all(source.resolve() == replay.resolve()
                                for source in seen_sources))
            self.assertNotIn('"kind": "quote"', replay.read_text())

            ordinary_result = self._run(rows, db=root / "ordinary.sqlite3")
            self.assertEqual(replay_result["dataset_hash"], ordinary_result["dataset_hash"])
            self.assertEqual(
                [(row["variant_id"], row["gate"]["gate_hash"], row["gate"]["passes"])
                 for row in replay_result["results"]],
                [(row["variant_id"], row["gate"]["gate_hash"], row["gate"]["passes"])
                 for row in ordinary_result["results"]])

    def test_progress_phases_are_ordered_and_bounded(self):
        rows = losing_breakouts(sessions=14)
        events = []
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(rows, db=Path(directory) / "factory.sqlite3",
                               progress_callback=lambda phase, done, total:
                               events.append((phase, done, total)))
        self.assertTrue(result["results"])
        phase_order = {"diagnosing": 0, "evaluating": 1,
                       "aggregating": 2, "persisting": 3}
        self.assertTrue(events)
        self.assertEqual([phase_order[item[0]] for item in events],
                         sorted(phase_order[item[0]] for item in events))
        for _phase, done, total in events:
            self.assertGreaterEqual(done, 0)
            self.assertLessEqual(done, total)
        for phase in phase_order:
            phase_events = [item for item in events if item[0] == phase]
            self.assertTrue(phase_events)
            self.assertEqual(phase_events[-1][1], phase_events[-1][2])

    def test_worker_view_must_match_full_replay_projection(self):
        rows = self._corpus()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full = root / "full.jsonl"
            replay = root / "replay.jsonl"
            db = root / "factory.sqlite3"
            replay_rows = [row for row in rows
                           if str(row.get("kind", "bar")).lower() != "quote"]
            _write_rows(full, rows)
            _write_rows(replay, replay_rows[:-1])
            with self.assertRaisesRegex(
                    ValueError, "does not match the full corpus replay projection"):
                self._run(full, worker_data=replay, db=db)
            self.assertFalse(db.exists(), "validation must precede proof persistence")

    def test_worker_view_rejects_quotes_and_in_memory_sources(self):
        rows = self._corpus()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full = root / "full.jsonl"
            replay = root / "replay.jsonl"
            _write_rows(full, rows)
            _write_rows(replay, rows)
            with self.assertRaisesRegex(
                    ValueError, "only bars and option snapshots"):
                self._run(full, worker_data=replay,
                          db=root / "quoted.sqlite3")
            with self.assertRaisesRegex(
                    ValueError, "requires a file or directory data source"):
                self._run(rows, worker_data=replay,
                          db=root / "memory.sqlite3")

    def test_worker_view_change_after_validation_cannot_reach_gates(self):
        rows = self._corpus()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full = root / "full.jsonl"
            replay = root / "replay.jsonl"
            db = root / "factory.sqlite3"
            replay_rows = [row for row in rows
                           if str(row.get("kind", "bar")).lower() != "quote"]
            _write_rows(full, rows)
            _write_rows(replay, replay_rows)
            original_validate = factory_module.validate_worker_projection

            def mutate_after_validation(*args, **kwargs):
                digest = original_validate(*args, **kwargs)
                with replay.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(replay_rows[-1], sort_keys=True) + "\n")
                return digest

            with patch.object(factory_module, "validate_worker_projection",
                              side_effect=mutate_after_validation), \
                 patch.object(factory_module, "ProcessPoolExecutor",
                              side_effect=OSError):
                result = self._run(full, worker_data=replay, db=db)
            self.assertEqual(result["status"], "partial_worker_failure")
            ledger = factory_module.FactoryLedger(db)
            self.assertEqual(ledger.status()["cycles"], 0)
            self.assertEqual(ledger.status()["accounts"], 0)


if __name__ == "__main__":
    unittest.main()
