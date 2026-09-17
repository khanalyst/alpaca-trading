"""Recorder timing evidence and real-time quote boundaries stay distinct."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy import health, recorder, recorder_market


class RecorderMeasurementTests(unittest.TestCase):
    def test_cadence_history_does_not_count_current_twice(self):
        result = health._recorder_cadence({"cadence": {
            "configured_interval_seconds": 30,
            "realized_intervals_seconds": [30, 45],
            "realized_interval_seconds": 45,
        }})
        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["realized_interval_seconds"], 45)
        fallback = health._recorder_cadence({"cadence": {
            "realized_intervals_seconds": [True, float("nan"), float("inf"), -1],
            "realized_interval_seconds": 30,
            "realized_interval_p95_seconds": float("nan"),
        }})
        self.assertEqual(fallback["samples"], 1)
        self.assertEqual(fallback["realized_interval_p95_seconds"], 30)

    def test_health_exposes_only_known_valid_cycle_telemetry(self):
        stamp = "2026-09-09T14:31:10+00:00"
        attempt = {"cycle_telemetry": {
            "schema": "recorder-cycle-telemetry.v1",
            "bars_fetch_seconds": 12.0, "quotes_fetch_seconds": float("nan"),
            "unique_rows": True, "projected_rows": 10,
            "index_json_save_seconds": -1,
            "recent_key_value_preparation_seconds": 0.003,
            "recent_key_insert_seconds": 0.004,
            "recent_key_expiry_delete_seconds": 0.005,
            "recent_key_count_seconds": 0.006,
            "recent_key_metadata_update_seconds": 0.007,
            "recent_key_transaction_commit_seconds": 0.008,
            "recent_key_incoming_count": 4,
            "recent_key_expired_count": 2,
            "recent_key_current_count": 8,
            "bars_received_at": stamp, "quotes_received_at": "invalid",
            "last_bar_market_ts": "2026-09-09T14:30:00",
            "api_key": "must-not-be-exposed", "arbitrary": {"payload": "secret"},
        }}
        expected = {"schema": "recorder-cycle-telemetry.v1",
                    "bars_fetch_seconds": 12.0, "projected_rows": 10.0,
                    "recent_key_value_preparation_seconds": 0.003,
                    "recent_key_insert_seconds": 0.004,
                    "recent_key_expiry_delete_seconds": 0.005,
                    "recent_key_count_seconds": 0.006,
                    "recent_key_metadata_update_seconds": 0.007,
                    "recent_key_transaction_commit_seconds": 0.008,
                    "recent_key_incoming_count": 4.0,
                    "recent_key_expired_count": 2.0,
                    "recent_key_current_count": 8.0,
                    "bars_received_at": stamp}
        self.assertEqual(health._recorder_cycle_telemetry(attempt), expected)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".recorder-status.json").write_text(json.dumps(attempt))
            result = health.recorder(root, 120)
        self.assertEqual(result["cycle_telemetry"], expected)

    def test_cycle_duration_is_not_start_to_start_cadence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            (path.parent / recorder.STATUS_NAME).write_text(json.dumps({
                "cadence": {"cycle_started_ts": 100.0,
                            "realized_intervals_seconds": [30.0]}}))
            result = recorder._cadence_telemetry(
                path, interval=30.0, cycle_started_ts=160.0, cycle_completed_ts=203.0)
        self.assertEqual(result["realized_interval_seconds"], 60.0)
        self.assertEqual(result["cycle_duration_seconds"], 43.0)
        self.assertEqual(result["cycle_overrun_seconds"], 13.0)

    def test_recent_key_commit_reports_bounded_phase_timings_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / recorder.RECENT_KEY_INDEX_NAME
            telemetry = {}
            with recorder.RecentKeyIndex(database, create=True) as recent:
                recent.add_many([("old", "2026-08-08T13:00:00+00:00")])
                recent.bind(signature="before")
                result = recent.commit_cycle(
                    [("new", "2026-08-08T13:02:00+00:00")],
                    floor=datetime(2026, 8, 8, 13, 1,
                                   tzinfo=timezone.utc),
                    signature="after", telemetry=telemetry)

            phase_fields = (
                "recent_key_value_preparation_seconds",
                "recent_key_insert_seconds",
                "recent_key_expiry_delete_seconds",
                "recent_key_count_seconds",
                "recent_key_metadata_update_seconds",
                "recent_key_transaction_commit_seconds",
            )
            for field in phase_fields:
                self.assertIn(field, telemetry)
                self.assertTrue(
                    isinstance(telemetry[field], (int, float)) and
                    telemetry[field] >= 0 and
                    telemetry[field] != float("inf") and
                    telemetry[field] == telemetry[field])
            self.assertEqual(result["count"], 1)
            self.assertEqual(telemetry["recent_key_incoming_count"], 1)
            self.assertEqual(telemetry["recent_key_expired_count"], 1)
            self.assertEqual(telemetry["recent_key_current_count"], 1)

    def test_recent_key_commit_failure_rolls_back_without_success_count(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / recorder.RECENT_KEY_INDEX_NAME
            telemetry = {}
            with recorder.RecentKeyIndex(database, create=True) as recent:
                recent.add_many([("old", "2026-08-08T13:00:00+00:00")])
                recent.bind(signature="before")
                before = (
                    list(recent.db.execute(
                        "SELECT event_key,event_ts FROM recent_keys "
                        "ORDER BY event_key")),
                    recent.metadata(),
                )

                def deny_commit(action, first, _second, _database, _source):
                    if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                recent.db.set_authorizer(deny_commit)
                with self.assertRaises(sqlite3.DatabaseError):
                    recent.commit_cycle(
                        [("new", "2026-08-08T13:02:00+00:00")],
                        floor=datetime(2026, 8, 8, 13, 1,
                                       tzinfo=timezone.utc),
                        signature="after", telemetry=telemetry)
                recent.db.set_authorizer(None)
                self.assertEqual(
                    list(recent.db.execute(
                        "SELECT event_key,event_ts FROM recent_keys "
                        "ORDER BY event_key")), before[0])
                self.assertEqual(recent.metadata(), before[1])

            self.assertEqual(telemetry.get("recent_key_current_count", 0), 0)
            self.assertEqual(telemetry.get("recent_key_expired_count", 0), 0)

    def test_recent_key_interrupt_rolls_back_before_connection_closes(self):
        original_phase = recorder._timed_cycle_phase

        @contextmanager
        def interrupt(telemetry, name):
            if name == "recent_key_metadata_update_seconds":
                raise KeyboardInterrupt()
            with original_phase(telemetry, name):
                yield

        with tempfile.TemporaryDirectory() as directory:
            with recorder.RecentKeyIndex(Path(directory) / "keys.sqlite3",
                                         create=True) as recent:
                recent.add_many([("old", "2026-08-08T13:00:00+00:00")])
                recent.bind(signature="before")
                with patch.object(recorder, "_timed_cycle_phase", interrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        recent.commit_cycle(
                            [("new", "2026-08-08T13:02:00+00:00")],
                            floor=datetime(2026, 8, 8, 13, 1, tzinfo=timezone.utc),
                            signature="after", telemetry={})
                self.assertFalse(recent.db.in_transaction)
                self.assertTrue(recent.contains("old"))
                self.assertFalse(recent.contains("new"))
                self.assertEqual(recent.metadata()["corpus_signature"], "before")


class RecorderQuoteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.started = datetime(2026, 9, 9, 14, 31, 10, tzinfo=timezone.utc)
        started = self.started

        class Clock(datetime):
            current = started

            @classmethod
            def now(cls, tz=None):
                return cls.current if tz is None else cls.current.astimezone(tz)

        self.clock = Clock
        self.requests = []
        requests = self.requests

        class Provider:
            data_feed = "iex"
            future = False

            def bars(self, symbols, **kwargs):
                requests.append(("bars", kwargs))
                Clock.current += timedelta(seconds=12)
                return {"SPY": [SimpleNamespace(
                    timestamp=stamp, open=100, high=101, low=99, close=100,
                    volume=1000, feed="iex") for stamp in (
                        started.replace(second=0) - timedelta(minutes=1),
                        started.replace(second=0))]}

            def quotes(self, symbols, **kwargs):
                requests.append(("quotes", kwargs))
                stamp = kwargs["end"] + timedelta(seconds=6 if self.future else -1)
                return {"SPY": [SimpleNamespace(
                    timestamp=stamp, bid=99.99, ask=100.01, last=100, feed="iex")]}

        self.provider = Provider()

    def _rows(self, **kwargs):
        with patch.object(recorder_market, "datetime", self.clock):
            return list(recorder_market._rows(
                self.provider, ["SPY"], self.started,
                start=self.started - timedelta(minutes=1), **kwargs))

    def test_live_quotes_refresh_after_bars_without_widening_bar_completion(self):
        telemetry = {}
        rows = self._rows(refresh_quote_end=True, telemetry=telemetry)
        end = self.started + timedelta(seconds=12)
        self.assertEqual(self.requests[0][1]["end"], self.started)
        self.assertEqual(self.requests[1][1]["end"], end)
        self.assertEqual([row["event_type"] for row in rows], ["bar_1m", "quote"])
        self.assertEqual(datetime.fromisoformat(rows[1]["timestamp"]),
                         end - timedelta(seconds=1))
        self.assertEqual(rows[1]["as_of"], rows[1]["timestamp"])
        self.assertEqual(rows[1]["observed_at"], end.isoformat())
        self.assertEqual(telemetry["quote_request_end_at"], end.isoformat())
        self.assertEqual(datetime.fromisoformat(telemetry["last_quote_market_ts"]),
                         datetime.fromisoformat(rows[1]["timestamp"]))
        for key in ("bars_fetch_seconds", "quotes_fetch_seconds", "equity_projection_seconds"):
            self.assertGreaterEqual(telemetry[key], 0)

    def test_explicit_boundary_stays_fixed_despite_slow_bars(self):
        rows = self._rows(refresh_quote_end=False)
        self.assertEqual(self.requests[1][1]["end"], self.started)
        self.assertEqual(rows[1]["observed_at"],
                         (self.started + timedelta(seconds=12)).isoformat())

    def test_exact_session_close_caps_refreshed_quote_end(self):
        closed = self.started + timedelta(seconds=5)
        self._rows(refresh_quote_end=True, quote_end_limit=closed)
        self.assertEqual(self.requests[1][1]["end"], closed)

    def test_future_quote_still_fails(self):
        self.provider.future = True
        with self.assertRaisesRegex(RuntimeError, "quote.*future"):
            self._rows(refresh_quote_end=True)


if __name__ == "__main__":
    unittest.main()
