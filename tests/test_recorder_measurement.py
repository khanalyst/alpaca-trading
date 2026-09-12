"""Recorder timing evidence and real-time quote boundaries stay distinct."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
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
            "bars_received_at": stamp, "quotes_received_at": "invalid",
            "last_bar_market_ts": "2026-09-09T14:30:00",
            "api_key": "must-not-be-exposed", "arbitrary": {"payload": "secret"},
        }}
        expected = {"schema": "recorder-cycle-telemetry.v1",
                    "bars_fetch_seconds": 12.0, "projected_rows": 10.0,
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
