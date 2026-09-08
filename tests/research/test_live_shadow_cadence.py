"""Deterministic cadence checks for the live shadow service loop."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from research import live_shadow


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = float(now)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


class LiveShadowCadenceTests(unittest.TestCase):
    def _repeating_poll(self, duration: float) -> tuple[_Clock, list[float]]:
        clock = _Clock()
        starts: list[float] = []

        def run(_config):
            starts.append(clock.now)
            if len(starts) == 1:
                clock.now += duration
                return {"status": "ok"}
            raise KeyboardInterrupt()

        with patch.object(live_shadow, "run_shadow_once", side_effect=run), \
             patch.object(live_shadow.time, "monotonic",
                          side_effect=clock.monotonic), \
             patch.object(live_shadow.time, "sleep",
                          side_effect=clock.sleep), \
             patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                live_shadow.main([
                    "--no-diagnostic", "--interval", "30",
                ])
        return clock, starts

    def test_fast_poll_sleeps_only_to_start_anchored_deadline(self):
        clock, starts = self._repeating_poll(29.0)

        self.assertEqual(starts, [100.0, 130.0])
        self.assertEqual(clock.sleeps, [1.0])

    def test_slow_poll_skips_elapsed_slots_without_busy_catchup(self):
        clock, starts = self._repeating_poll(65.0)

        self.assertEqual(starts, [100.0, 190.0])
        self.assertEqual(clock.sleeps, [25.0])

    def test_failure_waits_full_interval_and_resets_anchor(self):
        clock = _Clock()
        starts: list[float] = []

        def run(_config):
            starts.append(clock.now)
            if len(starts) == 1:
                clock.now += 5.0
                raise RuntimeError("temporary failure")
            raise KeyboardInterrupt()

        with patch.object(live_shadow, "run_shadow_once", side_effect=run), \
             patch.object(live_shadow.time, "monotonic",
                          side_effect=clock.monotonic), \
             patch.object(live_shadow.time, "sleep",
                          side_effect=clock.sleep), \
             patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                live_shadow.main([
                    "--no-diagnostic", "--interval", "30",
                ])

        self.assertEqual(clock.sleeps, [30.0])
        self.assertEqual(starts, [100.0, 135.0])

    def test_once_returns_without_sleep_on_success_or_failure(self):
        with patch.object(live_shadow.time, "monotonic", return_value=100.0), \
             patch.object(live_shadow.time, "sleep") as sleep, \
             patch("builtins.print"), \
             patch.object(live_shadow, "run_shadow_once",
                          return_value={"status": "ok"}) as run:
            self.assertEqual(live_shadow.main([
                "--no-diagnostic", "--once", "--interval", "30",
            ]), 0)
        run.assert_called_once()
        sleep.assert_not_called()

        with patch.object(live_shadow.time, "monotonic", return_value=100.0), \
             patch.object(live_shadow.time, "sleep") as sleep, \
             patch("builtins.print"), \
             patch.object(live_shadow, "run_shadow_once",
                          side_effect=RuntimeError("failed")):
            self.assertEqual(live_shadow.main([
                "--no-diagnostic", "--once", "--interval", "30",
            ]), 1)
        sleep.assert_not_called()

    def test_interval_floor_and_nonfinite_values_are_config_consistent(self):
        for raw in ("0", "-2", "0.25", "nan", "inf", "-inf"):
            with self.subTest(raw=raw):
                observed = []

                def run(config):
                    observed.append(config.poll_seconds)
                    return {"status": "ok"}

                with patch.object(live_shadow.time, "monotonic",
                                  return_value=100.0), \
                     patch.object(live_shadow.time, "sleep") as sleep, \
                     patch("builtins.print"), \
                    patch.object(live_shadow, "run_shadow_once",
                                  side_effect=run):
                    self.assertEqual(live_shadow.main([
                        "--no-diagnostic", "--once", f"--interval={raw}",
                    ]), 0)
                self.assertEqual(observed, [1.0])
                sleep.assert_not_called()

        self.assertEqual(live_shadow._poll_interval(30), 30.0)
        self.assertEqual(live_shadow._poll_interval(math.nan), 1.0)
        self.assertEqual(
            live_shadow._next_shadow_cadence_deadline(100, 129, 30), 130)
        self.assertEqual(
            live_shadow._next_shadow_cadence_deadline(100, 165, 30), 190)


if __name__ == "__main__":
    unittest.main()
