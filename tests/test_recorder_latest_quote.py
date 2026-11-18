"""Causal additive latest-quote observations preserve full-tick recording."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import json

from deploy import recorder, recorder_market


class RecorderLatestQuoteTests(unittest.TestCase):
    def test_prefetch_is_before_bars_and_keeps_historical_quote_window(self):
        requested = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
        latest_received = requested + timedelta(seconds=2)
        bars_received = requested + timedelta(seconds=5)
        quotes_received = requested + timedelta(seconds=8)
        calls = []

        class Provider:
            data_feed = "iex"

            def latest_quotes(self, symbols, *, feed):
                calls.append("latest_quotes")
                return {"SPY": [{
                    "symbol": "SPY", "timestamp": requested - timedelta(seconds=1),
                    "bid": 100, "ask": 101, "last": 100.5, "feed": feed,
                }]}

            def bars(self, symbols, *, start, end, feed):
                calls.append("bars")
                return {"SPY": [SimpleNamespace(
                    timestamp=requested - timedelta(minutes=1), open=100,
                    high=101, low=99, close=100.5, volume=10, feed=feed)]}

            def quotes(self, symbols, *, start, end, feed):
                calls.append("quotes")
                return {"SPY": [SimpleNamespace(
                    timestamp=requested - timedelta(seconds=2), bid=100,
                    ask=101, last=100.5, feed=feed)]}

        class ReceiptDatetime(datetime):
            receipts = iter((latest_received, bars_received, quotes_received))

            @classmethod
            def now(cls, tz=None):
                value = next(cls.receipts)
                return value if tz is not None else value.replace(tzinfo=None)

        telemetry = {}
        with patch.object(recorder_market, "datetime", ReceiptDatetime):
            rows = list(recorder_market._rows(
                Provider(), ["SPY"], requested, feed="iex",
                start=requested - timedelta(minutes=1),
                prefetch_latest_quote=True, telemetry=telemetry))

        self.assertEqual(calls, ["latest_quotes", "bars", "quotes"])
        self.assertEqual([row["event_type"] for row in rows],
                         ["quote", "bar_1m", "quote"])
        self.assertTrue(rows[0]["_prefetched_quote"])
        self.assertEqual(datetime.fromisoformat(rows[0]["timestamp"]),
                         requested - timedelta(seconds=1))
        self.assertEqual(rows[0]["observed_at"], latest_received.isoformat())
        self.assertEqual(rows[1]["observed_at"], bars_received.isoformat())
        self.assertEqual(rows[2]["observed_at"], quotes_received.isoformat())
        self.assertEqual(telemetry["latest_quotes_received_at"],
                         latest_received.isoformat())
        self.assertEqual(telemetry["bars_received_at"], bars_received.isoformat())
        self.assertEqual(telemetry["quotes_received_at"], quotes_received.isoformat())

    def test_latest_failure_does_not_interrupt_full_tick_capture(self):
        requested = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)

        class Provider:
            data_feed = "iex"

            def latest_quotes(self, symbols, *, feed):
                raise RuntimeError("latest endpoint unavailable")

            def bars(self, symbols, *, start, end, feed):
                return {"SPY": [SimpleNamespace(
                    timestamp=requested - timedelta(minutes=1), open=100,
                    high=101, low=99, close=100.5, volume=10, feed=feed)]}

            def quotes(self, symbols, *, start, end, feed):
                return {"SPY": [SimpleNamespace(
                    timestamp=requested - timedelta(seconds=2), bid=100,
                    ask=101, last=100.5, feed=feed)]}

        telemetry = {}
        rows = list(recorder_market._rows(
            Provider(), ["SPY"], requested, feed="iex",
            start=requested - timedelta(minutes=1),
            prefetch_latest_quote=True, telemetry=telemetry))
        self.assertEqual([row["event_type"] for row in rows], ["bar_1m", "quote"])
        self.assertEqual(telemetry["latest_quote_prefetch_status"], "failed")
        self.assertIn("unavailable", telemetry["latest_quote_prefetch_error"])

    def test_stale_latest_sample_is_dropped_before_publication(self):
        requested = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)

        class Provider:
            data_feed = "iex"

            def latest_quotes(self, symbols, *, feed):
                return {"SPY": [SimpleNamespace(
                    timestamp=requested - timedelta(minutes=2), bid=100,
                    ask=101, last=100.5, feed=feed)]}

            def bars(self, symbols, *, start, end, feed):
                return {"SPY": [SimpleNamespace(
                    timestamp=requested - timedelta(minutes=1), open=100,
                    high=101, low=99, close=100.5, volume=10, feed=feed)]}

            def quotes(self, symbols, *, start, end, feed):
                return {"SPY": [SimpleNamespace(
                    timestamp=requested - timedelta(seconds=5), bid=100,
                    ask=101, last=100.5, feed=feed)]}

        telemetry = {}
        rows = list(recorder_market._rows(
            Provider(), ["SPY"], requested, feed="iex",
            start=requested - timedelta(minutes=1),
            prefetch_latest_quote=True, telemetry=telemetry))
        self.assertEqual([row["event_type"] for row in rows], ["bar_1m", "quote"])

    def test_recorder_publishes_prefetch_with_bar_and_retains_full_tick_rows(self):
        now = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
        latest_stamp = now - timedelta(seconds=30)
        later_tick = now - timedelta(seconds=10)
        calls = []

        class Provider:
            data_feed = "iex"

            def latest_quotes(self, symbols, *, feed):
                calls.append("latest")
                return {"SPY": [SimpleNamespace(
                    timestamp=latest_stamp, bid=100, ask=101, last=100.5,
                    feed=feed)]}

            def bars(self, symbols, *, start, end, feed):
                calls.append("bars")
                return {"SPY": [SimpleNamespace(
                    timestamp=now - timedelta(minutes=1), open=100,
                    high=101, low=99, close=100.5, volume=10, feed=feed)]}

            def quotes(self, symbols, *, start, end, feed):
                calls.append("quotes")
                return {"SPY": [
                    SimpleNamespace(timestamp=latest_stamp, bid=100, ask=101,
                                     last=100.5, feed=feed),
                    SimpleNamespace(timestamp=later_tick, bid=100.2, ask=101.2,
                                     last=100.7, feed=feed),
                ]}

        class ReceiptDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = now + timedelta(seconds=1)
                return value if tz is not None else value.replace(tzinfo=None)

        with TemporaryDirectory() as directory, \
                patch.object(recorder.shutil, "disk_usage",
                             return_value=SimpleNamespace(free=10 * 1024**3)), \
                patch.dict("os.environ", {
                    "ALPACA_RECORDER_CAPTURE_POLICY": "forward_only",
                    "ALPACA_RECORDER_LATEST_QUOTE_PREFETCH": "true",
                    "ALPACA_RECORDER_FETCH_WINDOW_MINUTES": "1",
                }), \
                patch.object(recorder_market, "datetime", ReceiptDatetime):
            output = Path(directory) / "market.csv"
            telemetry = {}
            recorder.record_once(Provider(), ["SPY"], output, now=now,
                                 telemetry=telemetry)
            rows = list(recorder.iter_corpus_rows(output))
            index = recorder._prepare_index(output)
            from deploy.research_dataset import build_views
            from research.live_shadow import _normalize_row, _row_visible
            view_root = Path(directory) / "views"
            view_root.mkdir()
            outputs = {name: view_root / f"{name}.jsonl"
                       for name in ("normalized", "bars", "quotes",
                                    "options", "replay")}
            build_views(
                partition_root=Path(directory) / "sessions",
                input_format="csv", recorded_root=Path(directory),
                from_recorder=True, **outputs)
            normalized = [json.loads(line) for line in outputs["normalized"].read_text(
                encoding="utf-8").splitlines()]
            shadow_events = [_normalize_row({
                **row, "event_type": row.get("kind")})[1]
                            for row in normalized]
            visible = [row for row in normalized
                       if _row_visible(row, now + timedelta(seconds=1))]

        self.assertEqual(calls, ["latest", "bars", "quotes"])
        self.assertEqual(sorted(row["event_type"] for row in rows),
                         ["bar_1m", "quote", "quote"])
        self.assertEqual(index["watermark"], later_tick.isoformat())
        self.assertEqual(telemetry["latest_quote_prefetch_status"], "ok")
        self.assertEqual(len(shadow_events), 3)
        self.assertEqual(sorted(row["kind"] for row in visible),
                         ["bar", "quote", "quote"])

    def test_latest_quote_does_not_move_historical_watermark(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "market.csv"
            old = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
            current = old + timedelta(hours=2)
            rows = [
                {"event_key": recorder_market._event_key(
                    "quote", "SPY", old),
                 "event_type": "quote", "symbol": "SPY",
                 "timestamp": old.isoformat(), "as_of": old.isoformat(),
                 "observed_at": current.isoformat(), "provider": "alpaca",
                 "feed": "iex", "bid": "100", "ask": "101"},
                {"event_key": recorder_market._event_key(
                    "quote", "SPY", current),
                 "event_type": "quote", "symbol": "SPY",
                 "timestamp": current.isoformat(), "as_of": current.isoformat(),
                 "observed_at": current.isoformat(), "provider": "alpaca",
                 "feed": "iex", "bid": "100", "ask": "101"},
            ]
            recorder._append_partitions(output, rows)
            index = recorder._scan_corpus(output)
        self.assertEqual(index["watermark"], current.isoformat())

if __name__ == "__main__":
    unittest.main()
