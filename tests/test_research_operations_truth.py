"""Research services report failures and hung work instead of greenwashing."""

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from deploy import dashboard, health, scheduler
from tests.helpers import valid_config


class ResearchSchedulerTruthTests(unittest.TestCase):
    def test_structured_author_and_review_failures_are_operational_failures(self):
        author = scheduler.structured_failure({
            "status": "FAILED", "generation": 2, "accepted_count": 0,
            "attempt_id": "attempt", "error": "provider down",
        })
        review = scheduler.structured_failure({
            "status": "PARTIAL", "max_reviews": 8, "reviewed": 1,
            "retry_pending": 1, "failed": 0,
        })
        self.assertEqual(author["component"], "authoring")
        self.assertEqual(review["component"], "review")
        self.assertIsNone(scheduler.structured_failure({
            "status": "AUTHORED", "generation": 2, "accepted_count": 1,
        }))

    def test_output_capture_is_bounded_but_recognizes_evicted_failures(self):
        capture = scheduler._BoundedCapture(12)
        capture.feed(json.dumps({
            "status": "FAILED", "generation": 1, "accepted_count": 0,
            "error": "bad",
        }) + "\n")
        capture.feed("x" * 100)
        self.assertLessEqual(len(capture.tail), 12)
        self.assertTrue(capture.truncated)
        self.assertEqual(capture.structured_failures[0]["component"],
                         "authoring")

    def test_child_timeout_terminates_the_process_group_and_is_not_success(self):
        child = MagicMock(pid=4321)
        child.wait.side_effect = [
            subprocess.TimeoutExpired("nightly", 0), 0]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(scheduler.time, "monotonic", side_effect=[0.0, 1.0]), \
                patch.object(scheduler.os, "killpg") as killpg:
            with self.assertRaises(scheduler.ResearchJobTimeout):
                scheduler._wait_for_child_with_heartbeats(
                    child, Path(directory) / "research.json",
                    timeout_seconds=0.1, terminate_grace_seconds=0)
        self.assertEqual(killpg.call_args_list, [
            call(4321, scheduler.signal.SIGTERM),
            call(4321, scheduler.signal.SIGKILL),
        ])

    def test_research_health_detects_live_heartbeat_past_job_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": 100,
                "started_ts": 10, "deadline_ts": 90,
                "last_exit_code": 0, "job_id": "hung-job",
            }), encoding="utf-8")
            result = health.research(path, 60, now=100)
        self.assertFalse(result["ok"])
        self.assertTrue(result["hung"])
        self.assertEqual(result["job_id"], "hung-job")

    def test_masked_structured_failure_makes_one_shot_scheduler_nonzero(self):
        child = MagicMock(pid=4321, stdout=io.StringIO(json.dumps({
            "status": "FAILED", "generation": 3, "accepted_count": 0,
            "attempt_id": "audit", "error": "provider down",
        }) + "\n"), stderr=io.StringIO("provider unavailable\n"))
        child.wait.return_value = 0
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "research.json"
            args = SimpleNamespace(
                config="config.yaml", status_file=str(status), hour=3,
                minute=0, script="research/nightly.sh", root=".",
                timeout_seconds=60, output_limit_chars=100, once=True)
            with patch.object(scheduler, "_running", True), \
                    patch.object(scheduler, "configured_mode", return_value="demo"), \
                    patch("main.load_secrets"), \
                    patch.object(scheduler.signal, "signal"), \
                    patch.object(scheduler.subprocess, "Popen", return_value=child):
                exit_code = scheduler.run_scheduler(args)
            stored = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["last_exit_code"], 1)
        self.assertEqual(stored["structured_failures"][0]["component"],
                         "authoring")

    def test_trader_health_and_dashboard_expose_per_strategy_shadow_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": 100,
                "research_expected": True, "research_available": True,
                "research_status": "degraded",
                "strategy_shadow_errors": {
                    "momentum": {"type": "ValueError", "error": "bad row"}},
            }), encoding="utf-8")
            result = health.trader(path, 60, now=100)
            safe = dashboard._safe_heartbeat(path)
        self.assertFalse(result["ok"])
        self.assertIn("momentum", result["strategy_shadow_errors"])
        self.assertEqual(safe["research_status"], "degraded")
        self.assertIn("momentum", safe["strategy_shadow_errors"])


class RuntimeCapabilityTests(unittest.TestCase):
    def _build(self, execution_mode: str):
        cfg = valid_config()
        cfg["strategy"]["execution_mode"] = execution_mode
        exchange = MagicMock()
        with patch("agent.engine.Exchange", return_value=exchange), \
                patch("agent.engine.AlertManager", return_value=MagicMock()), \
                patch("agent.engine.RiskEngine"), \
                patch("agent.engine.Engine._build_shadow", return_value=None), \
                patch("agent.engine.brain.LLM") as llm, \
                patch("agent.engine.state.configure_runtime"), \
                patch("agent.engine.state.bind_runtime_identity"), \
                patch("agent.engine.state.check_journal"), \
                patch("agent.engine.state.set_journal_context"), \
                patch("agent.engine.state.new_run_id", return_value="run-test"), \
                patch("agent.engine.state.stable_fingerprint", return_value="cfg"), \
                patch("agent.engine.state.code_fingerprint", return_value="code"):
            from agent.engine import Engine
            engine = Engine(cfg)
        return engine, llm

    def test_deterministic_and_shadow_only_start_without_llm_client_or_preflight(self):
        for mode in ("deterministic", "shadow_only"):
            with self.subTest(mode):
                engine, llm = self._build(mode)
                llm.assert_not_called()
                self.assertFalse(hasattr(engine, "llm"))

    def test_analyst_start_still_requires_llm_preflight(self):
        _engine, llm = self._build("analyst")
        llm.assert_called_once()
        llm.return_value.preflight.assert_called_once_with()

    def test_strategy_shadow_failure_is_attributed_and_cleared_per_strategy(self):
        from agent.engine import Engine

        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine._strategy_shadow_errors = {}
        spec = SimpleNamespace(id="broken", version="v1", setup_types=("x",))
        with patch("agent.engine.contracts.EVIDENCE_BUILDERS", {
                "broken": MagicMock(side_effect=ValueError("bad evidence"))}), \
                patch("agent.engine.registry.spec_for", return_value=spec), \
                patch.object(engine, "_shadow_cfg", return_value={}), \
                patch("agent.engine.state.log_event"):
            engine._record_shadow_decisions({"BTC": {"price": 1}})
        self.assertEqual(engine._strategy_shadow_errors["broken"]["type"],
                         "ValueError")

        with patch("agent.engine.contracts.EVIDENCE_BUILDERS", {
                "broken": MagicMock(return_value={})}), \
                patch("agent.engine.registry.spec_for", return_value=spec), \
                patch.object(engine, "_shadow_cfg", return_value={}), \
                patch("agent.engine.state.log_event"):
            engine._record_shadow_decisions({"BTC": {"price": 1}})
        self.assertEqual(engine._strategy_shadow_errors, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
