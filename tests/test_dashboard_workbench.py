from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from deploy.dashboard_workbench import workbench
from deploy.market_observations import append_observations
from tests.test_market_observations import bar


class WorkbenchTests(unittest.TestCase):
    def test_partial_parent_is_excluded_and_net_retry_fills_are_summed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "runtime/paper/journal.db"
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE trades(ts REAL, action TEXT, symbol TEXT, setup_id TEXT, qty REAL, runtime_mode TEXT, variant_id TEXT, entry_feed TEXT, net_pnl REAL, fees REAL, gross_pnl REAL)")
                rows = [(1, "open", "SPY", "a", 10, "paper", "v1", "iex", None, None, None),
                        (2, "close", "SPY", "a", 4, "paper", "v1", "iex", 3.6, .4, 4),
                        (3, "open", "SPY", "b", 10, "live", "v1", "iex", None, None, None),
                        (4, "close", "SPY", "b", 10, "live", "v1", "iex", 900, 1, 901)]
                db.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.assertEqual(workbench(root, {"variant_id": "v1"})["evidence"]["parents"], [])
            with sqlite3.connect(path) as db:
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

    def test_invalid_source_and_reversed_dates_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for query in ({"source": "all"}, {"start_date": "2026-09-08", "end_date": "2026-09-07"}):
                with self.assertRaises(ValueError):
                    workbench(Path(tmp), query)


if __name__ == "__main__":
    unittest.main()
