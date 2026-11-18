"""Recorder stop signals finish durable work without starting new cycles."""

import json
from pathlib import Path
from types import SimpleNamespace
import signal
import tempfile
import unittest
from unittest.mock import patch

from deploy import recorder


class RecorderShutdownTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "universe": {"symbols": ["SPY"], "asset_classes": []},
        }
        self.provider = SimpleNamespace(data_feed="iex")

    def _signal_hook(self, callbacks):
        original = recorder.signal.signal

        def install(signum, handler):
            previous = original(signum, handler)
            callbacks[signum] = handler
            return previous

        return install

    def _main(self, directory, record_once, callbacks):
        with patch("main.load_cfg", return_value=self.config), \
                patch.object(recorder, "AlpacaProvider",
                             return_value=self.provider), \
                patch.object(recorder, "record_once", side_effect=record_once), \
                patch.object(recorder.signal, "signal",
                             side_effect=self._signal_hook(callbacks)):
            return recorder.main([
                "--config", "config.yaml", "--out", str(directory),
                "--interval", "30",
            ])

    def test_signal_during_cycle_finishes_and_skips_next_cycle(self):
        callbacks = {}
        calls = []

        def record(*_args, **_kwargs):
            calls.append(True)
            callbacks[signal.SIGTERM](signal.SIGTERM, None)
            return 4

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self._main(Path(directory), record, callbacks), 0)
            status = json.loads(
                (Path(directory) / recorder.STATUS_NAME).read_text())

        self.assertEqual(len(calls), 1)
        self.assertEqual(status["status"], "stopped")
        self.assertIs(status["shutdown_requested"], True)
        # Signal dispositions are restored after the long-running entrypoint
        # returns, rather than leaking the recorder callback into its caller.
        self.assertIn(signal.SIGTERM, callbacks)
        self.assertIn(signal.SIGINT, callbacks)

    def test_signal_interrupts_idle_wait_without_starting_another_cycle(self):
        callbacks = {}
        calls = []

        def record(*_args, **_kwargs):
            calls.append(True)
            return 1

        waits = []

        def interruptible_sleep(timeout):
            waits.append(timeout)
            callbacks[signal.SIGINT](signal.SIGINT, None)

        with patch.object(recorder.time, "sleep",
                          side_effect=interruptible_sleep):
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(self._main(Path(directory), record, callbacks), 0)

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(waits), 1)
        self.assertGreaterEqual(waits[0], 0)

    def test_stop_after_failed_cycle_keeps_retry_checkpoint(self):
        callbacks = {}
        calls = []

        def record(*_args, **_kwargs):
            calls.append(True)
            callbacks[signal.SIGTERM](signal.SIGTERM, None)
            raise RuntimeError("temporary market-data outage")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / recorder.PENDING_QUOTE_WINDOW_NAME
            pending_payload = {
                "schema": recorder.PENDING_QUOTE_WINDOW_SCHEMA,
                "start": "2026-11-18T14:00:00+00:00",
                "end": "2026-11-18T14:01:00+00:00",
                "capture_policy": "catch_up",
                "observed_at": "2026-11-18T14:01:00+00:00",
                "recorded": False,
            }
            pending.write_text(json.dumps(pending_payload), encoding="utf-8")
            self.assertEqual(self._main(root, record, callbacks), 1)
            status = json.loads((root / recorder.STATUS_NAME).read_text())

            self.assertEqual(json.loads(pending.read_text()), pending_payload)

        self.assertEqual(len(calls), 1)
        self.assertEqual(status["status"], "failed")
        self.assertIs(status["shutdown_requested"], True)
        self.assertTrue(status["retryable"])


if __name__ == "__main__":
    unittest.main()
