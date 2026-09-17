from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deploy import recorder_corpus_path
from deploy.dashboard import _reports
from deploy.dashboard_workbench import workbench
from deploy.market_observations import append_observations
from tests.test_market_observations import bar


class WorkbenchTests(unittest.TestCase):
    def test_configured_epoch_is_shared_and_old_chart_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, close in (("recorded", 101), ("recorded-fresh", 102)):
                recorded = root / "runtime/research" / name
                sessions = recorded / "sessions"
                sessions.mkdir(parents=True)
                partition = sessions / "market-2026-09-08.csv"
                partition.write_text("event_type\n")
                marker = {"schema": "recorder-partition-calendar.v1",
                          "partition": partition.name, "source": "alpaca_calendar",
                          "status": "open", "open": "2026-09-08T13:30:00+00:00",
                          "close": "2026-09-08T20:00:00+00:00"}
                partition.with_name(partition.name + ".calendar.json").write_text(json.dumps(marker))
                append_observations(recorded / "bar-observations.sqlite3", [bar(close=close)])
            fresh = root / "runtime/research/recorded-fresh"
            for value in ("runtime/research/recorded-fresh", str(fresh)):
                with self.subTest(value=value), patch.dict(
                        os.environ, {"ALPACA_RECORDER_CORPUS_ROOT": value}):
                    self.assertEqual(recorder_corpus_path(root), fresh.resolve())
                    result = workbench(root, {"symbol": "SPY", "feed": "iex",
                        "start_date": "2026-09-08", "as_of": "2026-09-08T13:32:00+00:00"})
                    candles = result["candles"]
                    self.assertTrue(candles["available"], candles)
                    self.assertEqual(candles["series"]["1m"][0]["close"], 102)
                    self.assertEqual(candles["corpus_root"], "runtime/research/recorded-fresh")

    def test_chart_epoch_cannot_escape_runtime_by_path_or_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime").mkdir()
            (root / "outside").mkdir()
            (root / "runtime/escape").symlink_to(root / "outside", target_is_directory=True)
            for value in ("outside", "runtime/escape"):
                with self.subTest(value=value), patch.dict(
                        os.environ, {"ALPACA_RECORDER_CORPUS_ROOT": value}):
                    with self.assertRaisesRegex(ValueError, "inside runtime"):
                        recorder_corpus_path(root)
                    result = workbench(root, {"symbol": "SPY", "feed": "iex",
                        "start_date": "2026-09-08"})
                    self.assertFalse(result["candles"]["available"])

    def test_reports_and_research_records_are_not_relabelled_as_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "research/results"
            reports.mkdir(parents=True)
            (reports / "freshly-copied-old-results.md").write_text("net: -123\n")
            rows = _reports(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["epoch_binding"], "unverified")
            self.assertEqual(rows[0]["evidence_scope"], "historical_or_unbound")
            self.assertFalse(rows[0]["authorizing"])
            evidence = workbench(root, {"source": "research"})["evidence"]
            self.assertEqual(evidence["evidence_scope"], "retained_history_across_epochs")
            self.assertFalse(evidence["authorizing"])

    def test_partial_parent_is_excluded_and_net_retry_fills_are_summed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "runtime/paper/journal.db"
            path.parent.mkdir(parents=True)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE trades(ts REAL, action TEXT, symbol TEXT, setup_id TEXT, qty REAL, runtime_mode TEXT, variant_id TEXT, entry_feed TEXT, net_pnl REAL, fees REAL, gross_pnl REAL)")
                rows = [(1, "open", "SPY", "a", 10, "paper", "v1", "iex", None, None, None),
                        (2, "close", "SPY", "a", 4, "paper", "v1", "iex", 3.6, .4, 4),
                        (3, "open", "SPY", "b", 10, "live", "v1", "iex", None, None, None),
                        (4, "close", "SPY", "b", 10, "live", "v1", "iex", 900, 1, 901)]
                db.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.assertEqual(workbench(root, {"variant_id": "v1"})["evidence"]["parents"], [])
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO trades VALUES (5,'close','SPY','a',6,'paper','v1','iex',11.4,.6,12)")
            result = workbench(root, {"variant_id": "v1"})["evidence"]["parents"]
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["net"], 15)
            self.assertEqual(result[0]["fees"], 1)
            self.assertEqual(workbench(root, {"variant_id": "missing"})["evidence"]["parents"], [])

    def test_candles_exclude_incomplete_higher_timeframe_and_future_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorded = root / "runtime/research/recorded"
            sessions = recorded / "sessions"
            sessions.mkdir(parents=True)
            p = sessions / "market-2026-09-08.csv"
            p.write_text("event_type\n")
            marker = {"schema": "recorder-partition-calendar.v1", "partition": p.name,
                      "source": "alpaca_calendar", "status": "open",
                      "open": "2026-09-08T13:30:00+00:00", "close": "2026-09-08T20:00:00+00:00"}
            p.with_name(p.name + ".calendar.json").write_text(json.dumps(marker))
            opened = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
            rows = [bar(timestamp=(opened + timedelta(minutes=i)).isoformat(),
                        as_of=(opened + timedelta(minutes=i+1)).isoformat(),
                        observed_at=(opened + timedelta(minutes=i+1, seconds=1)).isoformat())
                    for i in range(6)]
            append_observations(recorded / "bar-observations.sqlite3", rows)
            result = workbench(root, {"symbol": "SPY", "feed": "iex", "start_date": "2026-09-08",
                "as_of": "2026-09-08T13:36:05+00:00"})["candles"]
            self.assertTrue(result["available"], result)
            self.assertEqual(len(result["series"]["1m"]), 6)
            self.assertEqual(len(result["series"]["5m"]), 1)
            self.assertEqual(result["series"]["15m"], [])
            self.assertEqual(result["series"]["1d"], [])

    def test_order_timing_preserves_initial_fields_and_respects_receipt_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / 'runtime/paper/journal.db'
            p.parent.mkdir(parents=True)
            with closing(sqlite3.connect(p)) as db, db:
                db.execute('CREATE TABLE orders(ts REAL, order_id TEXT, symbol TEXT, runtime_mode TEXT, decision_ts REAL, broker_filled_ts REAL)')
                db.execute("INSERT INTO orders VALUES (1,'o','SPY','paper',0.5,NULL)")
                db.execute("INSERT INTO orders VALUES (3,'o','SPY','paper',NULL,2)")
            before = workbench(root, {'as_of': '1970-01-01T00:00:02+00:00'})['evidence']['order_timing']
            self.assertEqual(len(before), 1)
            self.assertIsNone(before[0]['broker_filled_ts'])
            after = workbench(root)['evidence']['order_timing']
            self.assertEqual(after[0]['decision_ts'], .5)
            self.assertEqual(after[0]['broker_filled_ts'], 2)

    def test_invalid_source_and_reversed_dates_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for query in ({"source": "all"}, {"start_date": "2026-09-08", "end_date": "2026-09-07"}):
                with self.assertRaises(ValueError):
                    workbench(Path(tmp), query)


if __name__ == "__main__":
    unittest.main()
