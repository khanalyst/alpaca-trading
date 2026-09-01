"""Focused coverage for bounded research progress observability."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deploy import dashboard, health, scheduler, scheduler_output


def _progress(*, phase="discover", unit="rows", vehicle="equity", done=1,
              total=4, updated_ts=100.0):
    return {
        "schema": "research-progress.v1", "phase": phase, "unit": unit,
        "vehicle": vehicle, "done": done, "total": total,
        "updated_ts": updated_ts,
    }


class ResearchProgressTests(unittest.TestCase):
    def test_readiness_reports_separate_shadow_selection_and_confirmation(self):
        parsed = scheduler_output.structured_research_readiness({
            "schema": "research-readiness.v1", "state": "pending",
            "heldout_min_sessions": 30, "qualification_min_sessions": 30,
            "offline_required_sessions": 150, "shadow_min_sessions": 60,
            "shadow_selection_sessions": 30,
            "shadow_confirmation_sessions": 30,
            "required_sessions": 210, "recorded_sessions": 4,
        })
        readiness = scheduler_output.derive_research_readiness(readiness=parsed,
                                                                 now=100)
        self.assertEqual(readiness["offline_required_sessions"], 150)
        self.assertEqual(readiness["shadow_selection_sessions"], 30)
        self.assertEqual(readiness["shadow_confirmation_sessions"], 30)
        self.assertEqual(readiness["shadow_min_sessions"], 60)
        self.assertEqual(readiness["shadow_tail_sessions"], 60)
        self.assertEqual(readiness["required_sessions"], 210)

    def test_readiness_derives_minimums_and_caps_eta_without_audit_constant(self):
        readiness = scheduler_output.derive_research_readiness(
            {"schema": "research-progress.v1", "phase": "record",
             "unit": "sessions", "vehicle": "both", "done": 4,
             "total": 10, "updated_ts": 90},
            {"schema": "research-readiness.v1", "state": "pending",
             "heldout_min_sessions": 3, "shadow_min_sessions": 2,
             "shadow_tail_sessions": 2, "qualification_min_sessions": 2,
             "heldout_fraction": .5, "shadow_fraction": .5,
             "qualification_fraction": .5,
             "recorded_sessions": 4, "progress_rate_sessions_per_day": 1,
             "updated_ts": 90}, now=100)
        self.assertEqual(readiness["offline_required_sessions"], 6)
        self.assertEqual(readiness["required_sessions"], 8)
        self.assertEqual(readiness["sessions_remaining"], 4)
        self.assertEqual(readiness["state"], "pending")
        self.assertLessEqual(readiness["eta_ts"], 100 + 90 * 86400)

    def test_readiness_unknown_when_progress_has_no_session_count(self):
        readiness = scheduler_output.derive_research_readiness(
            _progress(done=1, total=1, unit="steps", updated_ts=99), now=100)
        self.assertEqual(readiness["state"], "unknown")
        self.assertIsNone(readiness["recorded_sessions"])
        self.assertIsNone(readiness["sessions_remaining"])

    def test_readiness_accepts_bounded_lane_minimum_and_fraction_aliases(self):
        payload = {
            "schema": "research-readiness.v1", "state": "pending",
            "minimums": {"heldout": {"sessions": 4},
                          "shadow": {"sessions": 5}},
            "fractions": {"heldout": .5, "shadow": .25},
            "qualification_min_sessions": 5,
            "qualification_fraction": .25,
            "recorded_session_count": 2,
        }
        parsed = scheduler_output.structured_research_readiness(payload)
        self.assertEqual(parsed["heldout_min_sessions"], 4)
        self.assertEqual(parsed["shadow_min_sessions"], 5)
        derived = scheduler_output.derive_research_readiness(readiness=parsed, now=100)
        self.assertEqual(derived["offline_required_sessions"], 20)
        self.assertEqual(derived["required_sessions"], 25)
        self.assertEqual(derived["sessions_remaining"], 23)

    def test_scheduler_captures_cycle_readiness_event_with_shadow_reason(self):
        payload = {
            "schema": "research-readiness.v1", "state": "pending",
            "reason": "candidate-specific shadow proof is still required",
            "recorded_sessions": 12, "heldout_min_sessions": 30,
            "qualification_min_sessions": 30, "shadow_min_sessions": 60,
            "heldout_fraction": .24, "qualification_fraction": .2,
            "development_fraction": .8, "required_sessions": 150,
            "sessions_remaining": 138, "progress_age_seconds": 0,
            "observed_session_rate_per_day": 1.0,
            "progress_rate_sessions_per_day": 1.0,
            "eta_ts": 1000, "deadline_ts": 2000, "updated_ts": 100,
        }
        parsed = scheduler_output.structured_research_readiness(payload)
        self.assertIsNotNone(parsed)
        capture = scheduler_output._BoundedCapture(500)
        capture.feed(json.dumps(payload) + "\n")
        self.assertEqual(capture.research_readiness["shadow_min_sessions"], 60)
        self.assertIn("candidate-specific shadow proof", capture.research_readiness["reason"])

    def test_parser_is_strict_and_bounded(self):
        valid = _progress()
        self.assertEqual(scheduler_output.structured_research_progress(valid),
                         valid)
        for phase in ("preparing", "backtest", "diagnosing", "evaluating",
                      "aggregating", "persisting"):
            with self.subTest(phase=phase):
                self.assertEqual(
                    scheduler_output.structured_research_progress(
                        {**valid, "phase": phase})["phase"], phase)
        iso = {**valid, "updated_ts": "2026-08-14T10:00:00+00:00"}
        self.assertEqual(
            scheduler_output.structured_research_progress(iso), iso)
        malformed = (
            {**valid, "schema": "research-progress.v2"},
            {**valid, "phase": "arbitrary-child-phase"},
            {**valid, "unit": {"nested": "data"}},
            {**valid, "vehicle": "crypto"},
            {**valid, "done": True},
            {**valid, "total": -1},
            {**valid, "done": 5, "total": 4},
            {**valid, "extra": "not allowed"},
            {**valid, "updated_ts": "100"},
            {**valid, "updated_ts": "2026-08-14T10:00:00"},
            {**valid, "updated_ts": 10 ** 1000},
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                self.assertIsNone(
                    scheduler_output.structured_research_progress(payload))

    def test_capture_retains_latest_progress_even_after_tail_truncation(self):
        capture = scheduler_output._BoundedCapture(12)
        capture.feed(json.dumps(_progress(done=1, updated_ts=1)) + "\n")
        capture.feed("x" * 1000)
        capture.feed(json.dumps(_progress(done=3, updated_ts=3)) + "\n")
        self.assertEqual(capture.research_progress,
                         _progress(done=3, updated_ts=3))
        self.assertTrue(capture.truncated)
        self.assertNotIn("research-progress.v1", capture.tail)

    def test_capture_detail_selects_latest_across_stdout_and_stderr(self):
        stdout = scheduler_output._BoundedCapture(100)
        stderr = scheduler_output._BoundedCapture(100)
        stdout.feed(json.dumps(_progress(done=2, updated_ts=20)) + "\n")
        stderr.feed(json.dumps(_progress(done=3, updated_ts=30,
                                         vehicle="option")) + "\n")
        self.assertEqual(scheduler_output._capture_detail(
            stdout, stderr)["research_progress"],
            _progress(done=3, updated_ts=30, vehicle="option"))

    def test_capture_detail_orders_iso_timestamps(self):
        stdout = scheduler_output._BoundedCapture(100)
        stderr = scheduler_output._BoundedCapture(100)
        stdout.feed(json.dumps(_progress(
            done=1, updated_ts="2026-08-14T10:00:00+00:00")) + "\n")
        stderr.feed(json.dumps(_progress(
            done=2, updated_ts="2026-08-14T10:00:01+00:00",
            vehicle="option")) + "\n")
        self.assertEqual(scheduler_output._capture_detail(
            stdout, stderr)["research_progress"]["done"], 2)

    def test_equal_timestamps_keep_the_later_event(self):
        capture = scheduler_output._BoundedCapture(100)
        capture.feed(json.dumps(_progress(
            phase="preparing", done=0, total=1, updated_ts=100)) + "\n")
        capture.feed(json.dumps(_progress(
            phase="validation", done=1, total=1, updated_ts=100)) + "\n")
        self.assertEqual(capture.research_progress["phase"], "validation")

    def test_scheduler_running_heartbeat_includes_progress(self):
        class Child:
            pid = 123

            def __init__(self):
                self.calls = 0

            def wait(self, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("child", timeout)
                return 0

        capture = scheduler_output._BoundedCapture(100)
        capture.feed(json.dumps(_progress(done=2, updated_ts=42)) + "\n")
        statuses = []
        with mock.patch.object(scheduler, "write_status",
                               side_effect=lambda path, status, **detail:
                               statuses.append((status, detail))):
            self.assertEqual(scheduler._wait_for_child_with_heartbeats(
                Child(), Path("unused-status"), heartbeat_seconds=0.001,
                stdout_capture=capture), 0)
        self.assertEqual(statuses[0][0], "running")
        self.assertEqual(statuses[0][1]["research_progress"],
                         _progress(done=2, updated_ts=42))

    def test_health_research_passes_progress_through(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            payload = {"status": "running", "updated_ts": 100,
                       "last_exit_code": None,
                       "research_progress": _progress(done=2, updated_ts=99)}
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = health.research(path, 60, now=100)
        self.assertTrue(result["ok"])
        self.assertEqual(result["research_status"], "degraded")
        self.assertEqual(result["research_readiness"]["state"], "unknown")
        self.assertEqual(result["research_progress"], payload["research_progress"])

    def test_health_drops_malformed_nested_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": 100,
                "research_progress": {"secret": ["not bounded"]},
            }), encoding="utf-8")
            result = health.research(path, 60, now=100)
        self.assertIsNone(result["research_progress"])

    def test_dashboard_heartbeat_only_exposes_valid_progress_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            payload = {
                "status": "running", "research_progress": _progress(),
                "arbitrary": {"secret": "must not escape"},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            safe = dashboard._safe_heartbeat(path)
        self.assertEqual(safe["research_progress"], payload["research_progress"])
        self.assertNotIn("arbitrary", safe)
        payload["research_progress"] = {
            **_progress(), "nested": {"secret": "must not escape"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotIn("research_progress", dashboard._safe_heartbeat(path))


if __name__ == "__main__":
    unittest.main()
