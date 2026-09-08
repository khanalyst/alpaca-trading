"""Cadence and entrypoint contracts for the deployed shadow wrapper."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from deploy import shadow as shadow_service


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = float(now)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


class ShadowServiceCadenceTests(unittest.TestCase):
    def _repeating_poll(self, duration: float) -> tuple[_Clock, list[float]]:
        clock = _Clock()
        starts: list[float] = []

        class Runner:
            def __init__(self, _config):
                pass

            def run_once(self):
                starts.append(clock.now)
                if len(starts) == 1:
                    clock.now += duration
                    return {"candidate_errors": {}}
                raise KeyboardInterrupt()

        with patch.object(shadow_service, "ShadowRunner", Runner), \
             patch.object(shadow_service, "_write_health"), \
             patch.object(shadow_service.time, "monotonic",
                          side_effect=clock.monotonic), \
             patch.object(shadow_service.time, "sleep",
                          side_effect=clock.sleep), \
             patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                shadow_service.main(["--no-diagnostic", "--interval", "30"])
        return clock, starts

    def test_fast_poll_waits_only_to_anchored_deadline(self):
        clock, starts = self._repeating_poll(29.0)

        self.assertEqual(starts, [100.0, 130.0])
        self.assertEqual(clock.sleeps, [1.0])

    def test_slow_poll_skips_missed_slots(self):
        clock, starts = self._repeating_poll(65.0)

        self.assertEqual(starts, [100.0, 190.0])
        self.assertEqual(clock.sleeps, [25.0])

    def test_failure_waits_full_interval_then_resets_anchor(self):
        clock = _Clock()
        starts: list[float] = []

        class Runner:
            def __init__(self, _config):
                pass

            def run_once(self):
                starts.append(clock.now)
                if len(starts) == 1:
                    clock.now += 5.0
                    raise RuntimeError("temporary failure")
                if len(starts) == 2:
                    clock.now += 29.0
                    return {"candidate_errors": {}}
                raise KeyboardInterrupt()

        with patch.object(shadow_service, "ShadowRunner", Runner), \
             patch.object(shadow_service, "_write_health"), \
             patch.object(shadow_service.time, "monotonic",
                          side_effect=clock.monotonic), \
             patch.object(shadow_service.time, "sleep",
                          side_effect=clock.sleep), \
             patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                shadow_service.main(["--no-diagnostic", "--interval", "30"])

        self.assertEqual(starts, [100.0, 135.0, 165.0])
        self.assertEqual(clock.sleeps, [30.0, 1.0])

    def test_once_preserves_exit_and_health_projection_without_sleep(self):
        class SuccessRunner:
            def __init__(self, _config):
                pass

            def run_once(self):
                return {
                    "events": 4,
                    "candidate_errors": {"candidate": "failed"},
                    "unpublished_detail": "excluded",
                }

        with patch.object(shadow_service, "ShadowRunner", SuccessRunner), \
             patch.object(shadow_service, "_write_health") as health, \
             patch.object(shadow_service.time, "monotonic", return_value=100.0), \
             patch.object(shadow_service.time, "sleep") as sleep, \
             patch("builtins.print"):
            self.assertEqual(shadow_service.main([
                "--no-diagnostic", "--once",
            ]), 0)
        sleep.assert_not_called()
        self.assertEqual(health.call_args.args[1], "degraded")
        self.assertEqual(health.call_args.kwargs["events"], 4)
        self.assertEqual(health.call_args.kwargs["candidate_errors"], {
            "candidate": "failed",
        })
        self.assertNotIn("unpublished_detail", health.call_args.kwargs)

        class FailureRunner:
            def __init__(self, _config):
                pass

            def run_once(self):
                raise RuntimeError("failed")

        with patch.object(shadow_service, "ShadowRunner", FailureRunner), \
             patch.object(shadow_service, "_write_health") as health, \
             patch.object(shadow_service.time, "monotonic", return_value=100.0), \
             patch.object(shadow_service.time, "sleep") as sleep, \
             patch("builtins.print"):
            self.assertEqual(shadow_service.main([
                "--no-diagnostic", "--once",
            ]), 1)
        sleep.assert_not_called()
        self.assertEqual(health.call_args.args[1], "degraded")
        self.assertEqual(health.call_args.kwargs["last_error"],
                         "RuntimeError: failed")

    def test_wrapper_uses_normalized_one_second_config_floor(self):
        for raw in ("0", "0.25", "nan", "inf"):
            with self.subTest(raw=raw):
                observed = []

                class Runner:
                    def __init__(self, config):
                        observed.append(config.poll_seconds)

                    def run_once(self):
                        return {"candidate_errors": {}}

                with patch.object(shadow_service, "ShadowRunner", Runner), \
                     patch.object(shadow_service, "_write_health"), \
                     patch.object(shadow_service.time, "monotonic",
                                  return_value=100.0), \
                     patch.object(shadow_service.time, "sleep") as sleep, \
                     patch("builtins.print"):
                    self.assertEqual(shadow_service.main([
                        "--no-diagnostic", "--once", f"--interval={raw}",
                    ]), 0)
                self.assertEqual(observed, [1.0])
                sleep.assert_not_called()

    def test_compose_runs_the_deployed_shadow_wrapper(self):
        compose = Path("compose.yaml").read_text(encoding="utf-8")
        shadow = compose.split("\n  shadow:\n", 1)[1].split(
            "\n  dashboard:\n", 1)[0]

        self.assertIn("      - deploy/shadow.py", shadow)
        self.assertNotIn("research/live_shadow.py", shadow)


if __name__ == "__main__":
    unittest.main()
