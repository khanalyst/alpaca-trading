import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from agent import state
from deploy import dashboard, health, scheduler


REPO = Path(__file__).resolve().parents[1]


class SecretFileTests(unittest.TestCase):
    def test_configured_secret_file_replaces_stale_shell_values(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "container.env"
            source.write_text(
                "OKX_API_KEY=file-key\nOPENAI_API_KEY=file-model-key\n",
                encoding="utf-8")
            source.chmod(0o600)
            environment = {
                main.SECRETS_FILE_ENV: str(source),
                "OKX_API_KEY": "stale-shell-key",
                "ANTHROPIC_API_KEY": "stale-shell-model-key",
            }
            with patch.dict(os.environ, environment, clear=True):
                main.load_secrets("demo")
                self.assertEqual(os.environ["OKX_API_KEY"], "file-key")
                self.assertEqual(os.environ["OPENAI_API_KEY"], "file-model-key")
                self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
                self.assertEqual(main.secrets_file(), source)

    def test_ordinary_live_secret_file_still_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "container.env"
            source.write_text("OKX_API_KEY=value\n", encoding="utf-8")
            source.chmod(0o644)
            with patch.dict(
                    os.environ, {main.SECRETS_FILE_ENV: str(source)}, clear=True):
                with self.assertRaises(PermissionError):
                    main.load_secrets("live")


class SignalAndHeartbeatTests(unittest.TestCase):
    def test_signal_received_before_engine_attach_is_delivered_after_attach(self):
        controller = main._ShutdownSignals()
        controller._handle(signal.SIGTERM, None)
        engine = Mock()
        controller.attach(engine)
        engine.request_shutdown.assert_called_once_with("SIGTERM")

    def test_mode_scoped_heartbeat_is_atomic_and_contains_no_state_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(
                    state, RUNTIME=root, RUNTIME_SCOPE="demo",
                    HEARTBEAT_FILE=root / "heartbeat.json"):
                result = state.write_heartbeat(
                    "running", run_id="run-1", research_available=True)
                stored = json.loads((root / "heartbeat.json").read_text())
            self.assertEqual(stored, result)
            self.assertEqual(stored["runtime_mode"], "demo")
            self.assertNotIn("OKX_API_KEY", stored)
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_engine_shutdown_request_persists_an_operator_pause(self):
        # Imported locally so the pure deployment probes remain runnable on a
        # host that has not installed the exchange client yet.
        from agent.engine import Engine
        from tests.helpers import valid_config

        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.shadow = Mock()
        engine.request_shutdown("SIGTERM")
        with patch("agent.engine.state.load_state", return_value={
                "state": state.RUNNING}), \
                patch("agent.engine.state.set_state") as set_state, \
                patch("agent.engine.state.write_heartbeat"):
            engine._pause_for_shutdown()
        set_state.assert_called_once_with(state.PAUSED, operator_pause=True)


class HealthProbeTests(unittest.TestCase):
    def test_recorder_requires_a_recent_csv_not_just_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(health.recorder(root, 60, now=100)["ok"])
            path = root / "order_book" / "day.csv"
            path.parent.mkdir()
            path.write_text("ts,inst_id\n", encoding="utf-8")
            os.utime(path, (90, 90))
            self.assertTrue(health.recorder(root, 60, now=100)["ok"])

    def test_trader_is_unhealthy_when_configured_research_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": 100,
                "research_expected": True, "research_available": False,
            }), encoding="utf-8")
            result = health.trader(path, 60, now=100)
            self.assertFalse(result["ok"])

    def test_failed_scheduled_research_remains_unhealthy_while_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            path.write_text(json.dumps({
                "status": "waiting", "updated_ts": 100,
                "last_exit_code": 5,
            }), encoding="utf-8")
            self.assertFalse(health.research(path, 60, now=100)["ok"])


class SchedulerTests(unittest.TestCase):
    def test_missed_daily_run_is_due_immediately_once(self):
        now = datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(scheduler.next_run(now, 3, 0, None), now)
        following = scheduler.next_run(now, 3, 0, "2026-07-31")
        self.assertEqual(following.date().isoformat(), "2026-08-01")
        self.assertEqual((following.hour, following.minute), (3, 0))

    def test_config_mode_is_authoritative_for_nightly_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("mode: live\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_MODE": "demo"}, clear=True):
                self.assertEqual(scheduler.configured_mode(path), "live")
                self.assertEqual(os.environ["AGENT_MODE"], "live")

    def test_long_child_heartbeats_stay_healthy_and_then_go_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            child = Mock(pid=4321)
            child.wait.side_effect = [
                subprocess.TimeoutExpired("nightly", 30),
                subprocess.TimeoutExpired("nightly", 30),
                7,
            ]
            with patch.object(
                    scheduler.time, "time", side_effect=[100.0, 200.0]):
                exit_code = scheduler._wait_for_child_with_heartbeats(
                    child, path, heartbeat_seconds=30,
                    started_ts=50.0, run_date="2026-08-01",
                    child_pid=child.pid, last_run_date="2026-07-31",
                    last_exit_code=0)

            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 7)
            self.assertEqual(child.wait.call_count, 3)
            self.assertEqual(stored["status"], "running")
            self.assertEqual(stored["updated_ts"], 200.0)
            self.assertEqual(stored["started_ts"], 50.0)
            self.assertEqual(stored["child_pid"], 4321)
            self.assertTrue(health.research(path, 180, now=379.0)["ok"])
            stale = health.research(path, 180, now=381.0)
            self.assertFalse(stale["ok"])
            self.assertFalse(stale["fresh"])

    def test_scheduler_preserves_child_exit_code_in_final_status(self):
        child = Mock(pid=4321)
        statuses = []

        def finish_child(*_args, **_kwargs):
            scheduler._running = False
            return 5

        def capture_status(_path, status, **detail):
            statuses.append((status, detail))
            return {"status": status, **detail}

        args = SimpleNamespace(
            config="config.yaml", status_file="research.json",
            hour=3, minute=0, script="research/nightly.sh", root=".")
        with patch.object(scheduler, "_running", True), \
                patch.object(scheduler, "configured_mode", return_value="demo"), \
                patch("main.load_secrets"), \
                patch.object(scheduler.signal, "signal"), \
                patch.object(scheduler, "next_run", side_effect=lambda now, *_: now), \
                patch.object(scheduler.subprocess, "Popen", return_value=child), \
                patch.object(
                    scheduler, "_wait_for_child_with_heartbeats",
                    side_effect=finish_child), \
                patch.object(
                    scheduler, "write_status", side_effect=capture_status):
            self.assertEqual(scheduler.run_scheduler(args), 0)

        self.assertEqual([status for status, _ in statuses],
                         ["running", "failed", "stopped"])
        self.assertEqual(statuses[1][1]["last_exit_code"], 5)
        self.assertEqual(statuses[2][1]["last_exit_code"], 5)


class DashboardTests(unittest.TestCase):
    def test_state_view_is_allowlisted_and_does_not_echo_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "state": "RUNNING",
                "secret": "must-not-appear",
                "kill_reason": "provider response must-not-appear",
                "active_trades": {
                    "BTC/USDT:USDT": {
                        "direction": "long", "qty": 1,
                        "entry_price": 100, "private": "hidden",
                    },
                },
            }), encoding="utf-8")
            result = dashboard._safe_state(path)
            self.assertNotIn("secret", result)
            self.assertNotIn("kill_reason", result)
            self.assertNotIn("private", result["active_trades"][0])
            self.assertEqual(
                result["active_trades"][0]["symbol"], "BTC/USDT:USDT")

    def test_report_reader_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "findings").mkdir()
            (root / "outside.md").write_text("secret", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                dashboard.report_file(root, "outside.md")

    def test_report_index_includes_nested_variant_scorecards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scorecard = root / "findings" / "momentum" / "candidate.md"
            scorecard.parent.mkdir(parents=True)
            scorecard.write_text("# Candidate\n", encoding="utf-8")
            self.assertIn(
                "findings/momentum/candidate.md",
                {row["path"] for row in dashboard._reports(root)})

    def test_findings_view_reads_assignments_outcomes_and_backup_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "findings.db"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE strategy_experiment_assignments (
                        assignment_id TEXT, strategy_id TEXT,
                        baseline_variant_id TEXT, candidate_variant_id TEXT,
                        started_ts REAL, minimum_duration_seconds REAL,
                        minimum_observations INTEGER);
                    CREATE TABLE strategy_experiment_assignment_events (
                        assignment_id TEXT, status TEXT, event_ts REAL);
                    CREATE TABLE strategy_experiment_observations (
                        assignment_id TEXT);
                    CREATE TABLE experiment_outcomes (
                        outcome_id TEXT, assignment_id TEXT, strategy_id TEXT,
                        baseline_variant_id TEXT, candidate_variant_id TEXT,
                        terminal_status TEXT, verdict TEXT, created_ts REAL,
                        payload_hash TEXT);
                    CREATE TABLE backup_runs (
                        backup_id TEXT, started_ts REAL, target_kind TEXT,
                        require_external INTEGER);
                    CREATE TABLE backup_run_events (
                        backup_id TEXT, status TEXT, event_ts REAL);
                    INSERT INTO strategy_experiment_assignments VALUES
                        ('a1','momentum','base','candidate',1,100,10);
                    INSERT INTO strategy_experiment_assignment_events VALUES
                        ('a1','OBSERVING',2);
                    INSERT INTO experiment_outcomes VALUES
                        ('o1','a1','momentum','base','candidate',
                         'COMPLETED','INCONCLUSIVE',3,'hash');
                    INSERT INTO backup_runs VALUES
                        ('b1',4,'configured_local',0);
                    INSERT INTO backup_run_events VALUES ('b1','VERIFIED',5);
                """)
            result = dashboard._findings(path)
            self.assertTrue(result["available"])
            self.assertEqual(result["assignments"][0]["assignment_id"], "a1")
            self.assertEqual(result["outcomes"][0]["verdict"], "INCONCLUSIVE")
            self.assertFalse(result["backup"]["off_host_verified"])

    def test_compose_keeps_dashboard_private_and_away_from_secrets(self):
        compose = (REPO / "compose.yaml").read_text(encoding="utf-8")
        dashboard_block = compose.split("  dashboard:", 1)[1].split(
            "\nconfigs:", 1)[0]
        self.assertIn('"127.0.0.1:', dashboard_block)
        self.assertNotIn("agent_credentials", dashboard_block)
        self.assertIn("replicas: 1", compose)
        dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER 10001:10001", dockerfile)


if __name__ == "__main__":
    unittest.main()
