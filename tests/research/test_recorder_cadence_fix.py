"""Regression tests for recorder cadence and causal availability boundaries."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from deploy import recorder
from deploy import recorder_market


class RecorderCadenceFixTests(unittest.TestCase):
    def test_forward_overrun_resumes_once_without_skipping_another_slot(self):
        clock = 0.0
        starts = []
        durations = iter([31, 1, 94, 3])

        def record(*args, telemetry, **kwargs):
            nonlocal clock
            starts.append(clock)
            if len(starts) == 5:
                raise KeyboardInterrupt
            clock += next(durations)
            telemetry["windows_completed"] = 1
            return 1

        def sleep(seconds):
            nonlocal clock
            clock += seconds

        with self._temporary_output() as output, \
                patch.dict("os.environ", {"ALPACA_RECORDER_CAPTURE_POLICY": "forward_only"}), \
                patch.object(recorder, "AlpacaProvider", return_value=object()), \
                patch.object(recorder, "record_once", side_effect=record), \
                patch.object(recorder.time, "monotonic", side_effect=lambda: clock), \
                patch.object(recorder.time, "sleep", side_effect=sleep):
            with self.assertRaises(KeyboardInterrupt):
                recorder.main(["--out", str(output), "--interval", "30"])
        self.assertEqual(starts, [0, 31, 61, 155, 185])

    def test_failed_cycles_keep_the_original_cadence_anchor(self):
        """Repeated failed reads stay 30 seconds apart instead of backing off."""
        with self.subTest("deadline arithmetic"):
            first = recorder._next_cadence_deadline(0.0, 1.0, 30.0)
            second = recorder._next_cadence_deadline(first, 31.0, 30.0)
            self.assertEqual(first, 30.0)
            self.assertEqual(second, 60.0)

        with self.subTest("service retry loop"), self._temporary_output() as output:
            clock = [0.0]
            attempts = [0]

            def monotonic():
                return clock[0]

            def record(*_args, **_kwargs):
                attempts[0] += 1
                clock[0] = 1.0 if attempts[0] == 1 else 31.0
                if attempts[0] < 3:
                    raise RuntimeError(
                        "first" if attempts[0] == 1 else "second")
                raise KeyboardInterrupt()

            sleeps = []

            def sleep(seconds):
                sleeps.append(float(seconds))
                clock[0] += float(seconds)

            with patch.object(recorder, "AlpacaProvider", return_value=object()), \
                    patch.object(recorder, "record_once", side_effect=record), \
                    patch.object(recorder.time, "monotonic",
                                 side_effect=monotonic), \
                    patch.object(recorder.time, "sleep", side_effect=sleep):
                with self.assertRaises(KeyboardInterrupt):
                    recorder.main([
                        "--config", "config.yaml", "--out", str(output),
                        "--interval", "30",
                    ])
            self.assertEqual(attempts[0], 3)
            self.assertEqual(sum(sleeps), 58.0)
            self.assertTrue(all(0 < value <= recorder.MAX_STOP_POLL_SECONDS
                                for value in sleeps))

    def test_rows_keep_source_specific_response_receipts(self):
        requested = datetime(2026, 10, 5, 13, 31, tzinfo=timezone.utc)
        bars_received = requested + timedelta(seconds=7)
        quotes_received = requested + timedelta(seconds=35)

        class Provider:
            data_feed = "iex"

            def bars(self, symbols, *, start, end, feed, **kwargs):
                return {"SPY": [SimpleNamespace(
                    timestamp=requested - timedelta(minutes=1),
                    open=100, high=101, low=99, close=100.5, volume=10,
                )]}

            def quotes(self, symbols, *, start, end, feed):
                return {"SPY": [SimpleNamespace(
                    timestamp=requested - timedelta(seconds=5),
                    bid=100, ask=101, last=100.5,
                )]}

        class ReceiptDatetime(datetime):
            receipts = iter((bars_received, quotes_received))

            @classmethod
            def now(cls, tz=None):
                value = next(cls.receipts)
                return value if tz is not None else value.replace(tzinfo=None)

        telemetry = {}
        with patch.object(recorder_market, "datetime", ReceiptDatetime):
            rows = list(recorder_market._rows(
                Provider(), ["SPY"], requested, feed="iex",
                start=requested - timedelta(minutes=1),
                observed_at=requested, telemetry=telemetry))

        bar, quote = rows
        self.assertEqual(bar["event_type"], "bar_1m")
        self.assertEqual(quote["event_type"], "quote")
        self.assertEqual(bar["observed_at"], bars_received.isoformat())
        self.assertEqual(quote["observed_at"], quotes_received.isoformat())
        self.assertEqual(bar["as_of"], requested.isoformat())
        self.assertEqual(quote["as_of"], quote["timestamp"])
        self.assertEqual(telemetry["bars_received_at"], bars_received.isoformat())
        self.assertEqual(telemetry["quotes_received_at"], quotes_received.isoformat())

    def _temporary_output(self):
        import tempfile

        return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
