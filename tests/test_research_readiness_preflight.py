"""Research cycle early forward-evidence readiness behavior."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy import scheduler, scheduler_output


ROOT = Path(__file__).resolve().parents[1]


def _config(path: Path, *, llm_enabled: bool) -> None:
    path.write_text(json.dumps({
        "mode": "paper",
        "broker": {"paper": True, "allow_live": False,
                   "data_feed": "iex", "options_feed": "opra"},
        "universe": {"asset_classes": ["us_equity"]},
        "session": {"require_exact_calendar": True},
        "strategy": {"selection_mode": "all_proved",
                     "execution_mode": "shares"},
        "research": {"enabled": True, "require_validated_variant": True,
                     "strategy_llm": {"enabled": llm_enabled,
                                      "provider": "openai", "model": "gpt-5"}},
    }), encoding="utf-8")


def _session(recorded: Path, acceptance: Path, day: str, *, accepted: bool) -> None:
    sessions = recorded / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    acceptance.mkdir(parents=True, exist_ok=True)
    name = f"market-{day}.csv"
    opened = f"{day}T14:30:00+00:00"
    closed = f"{day}T21:00:00+00:00"
    (sessions / name).write_text("event_key,timestamp\nrow,1\n", encoding="utf-8")
    (sessions / f"{name}.calendar.json").write_text(json.dumps({
        "schema": "recorder-partition-calendar.v1", "partition": name,
        "status": "open", "source": "alpaca_calendar",
        "open": opened, "close": closed,
    }), encoding="utf-8")
    close_ts = datetime.fromisoformat(closed).timestamp()
    (acceptance / f"session-{day}.report.json").write_text(json.dumps({
        "schema": "session-acceptance-report.v1",
        "session": {"date": day, "open": opened, "close": closed,
                    "source": "alpaca_calendar"},
        "accepted": accepted, "status": "accepted" if accepted else "rejected",
        "operational_only": True, "authorizing": False,
        "promotion_eligible": False,
        "reasons": [] if accepted else ["sample_gap_detected"],
        "reason_counts": {} if accepted else {"sample_gap_detected": 1},
        "sample_counts": ({"total": 390, "healthy": 390, "failed": 0,
                           "valid_timestamps": 390}
                          if accepted else
                          {"total": 389, "healthy": 388, "failed": 1,
                           "valid_timestamps": 389}),
        "expected_symbols": ["AAPL", "MSFT"],
        "identities": {"deployment": "deploy-1", "code": "code-1",
                       "cohort": "cohort-1", "activation": "activation-1"},
        "coverage": {
            "first_sample_ts": datetime.fromisoformat(opened).timestamp(),
            "last_sample_ts": close_ts,
            "open_ts": datetime.fromisoformat(opened).timestamp(),
            "close_ts": close_ts,
            "start_tolerance_seconds": 60.0,
            "end_tolerance_seconds": 60.0,
            "max_sample_gap_seconds": 65.0,
            "max_observed_gap_seconds": 60.0,
            "closed_at_report": True,
        },
        "post_activation_progress": {
            "arms": 24, "snapshots": 390,
            "all_arms_progressed": True,
            "minimum_processed_events": 390,
            "minimum_session_delta": 389,
            "activation_watermark": {
                "last_inserted_at": datetime.fromisoformat(opened).timestamp() - 3600,
                "last_event_key": "activation-event", "count": 0,
                "decision_event_count": 0,
            },
        },
        "freshness": {
            "strict_threshold_cap_seconds": 30.0,
            "quote_event_age_seconds": {"count": 780, "max": 1.0},
            "bar_event_age_seconds": {"count": 780, "max": 61.0},
            "bar_publication_deadline_lag_seconds": {
                "count": 780, "max": 30.0},
            "shadow_source_lag_seconds": {"count": 390, "max": 1.0},
        },
        "warmup_sessions": [(date.fromisoformat(day) - timedelta(days=1)).isoformat()],
        "finalized_ts": close_ts + 1,
    }), encoding="utf-8")


def _run(root: Path, *, llm_enabled: bool, max_source_bytes: int,
         dataset: Path | None = None) -> subprocess.CompletedProcess:
    recorded = root / "recorded"
    acceptance = root / "acceptance"
    config = root / "config.json"
    _config(config, llm_enabled=llm_enabled)
    env = dict(
        os.environ,
        PYTHON=sys.executable,
        ALPACA_AGENT_CONFIG=str(config),
        ALPACA_RECORDED_DATASET_ROOT=str(recorded),
        ALPACA_RESEARCH_ACCEPTANCE_ROOT=str(acceptance),
        ALPACA_RESEARCH_DIRECT_STATUS_FILE=str(root / "research-direct.json"),
        ALPACA_RESEARCH_DIRECT_HEARTBEAT_SECONDS="1",
        ALPACA_RESEARCH_LLM_SECRETS_FILE="/dev/null",
        ALPACA_RESEARCH_MAX_SOURCE_BYTES=str(max_source_bytes),
        ALPACA_FACTORY_ENABLED="0",
    )
    if dataset is None:
        env.pop("ALPACA_RESEARCH_DATASET", None)
    else:
        env["ALPACA_RESEARCH_DATASET"] = str(dataset)
    env.pop("OPENAI_API_KEY", None)
    return subprocess.run(
        ["deploy/research-cycle.sh"], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False)


class ResearchReadinessPreflightTests(unittest.TestCase):
    def test_explicit_dataset_not_run_census_survives_scheduler_capture(self):
        readiness = {
            "schema": "research-readiness.v1", "state": "not_run",
            "reason": "partition census was not reached", "authorizing": False,
        }
        cycle = {
            "schema": "research-cycle.v1", "status": "completed_no_edge",
            "reason": "external dataset evaluated without an edge", "exit_code": 0,
            "outcomes": [], "proofs": False, "no_edge": True,
            "readiness": readiness,
        }
        captured = scheduler_output._BoundedCapture(4096)
        captured.feed(json.dumps(cycle))
        self.assertEqual(captured.research_cycles[0]["readiness"], readiness)
        self.assertEqual(captured.research_readiness, readiness)
        projected = scheduler_output.derive_research_readiness(
            readiness=captured.research_readiness, now=100)
        self.assertEqual(projected["state"], "not_run")
        self.assertFalse(projected["authorizing"])

    def test_terminal_census_does_not_erase_full_source_session_readiness(self):
        detailed = {
            "schema": "research-readiness.v1", "state": "pending",
            "reason": "candidate-specific proof is still required",
            "recorded_sessions": 30, "required_sessions": 210,
            "sessions_remaining": 180, "updated_ts": 100,
        }
        for census_state in ("not_run", "input_ready"):
            with self.subTest(census_state=census_state):
                captured = scheduler_output._BoundedCapture(4096)
                captured.feed(json.dumps(detailed))
                captured.feed(json.dumps({
                    "schema": "research-cycle.v1", "status": "completed_no_edge",
                    "reason": "cycle completed", "exit_code": 0,
                    "outcomes": [], "proofs": False, "no_edge": True,
                    "readiness": {
                        "schema": "research-readiness.v1", "state": census_state,
                        "reason": "metadata census only", "authorizing": False,
                    },
                }))
                self.assertEqual(captured.research_readiness["state"], "pending")
                self.assertEqual(captured.research_readiness["required_sessions"], 210)
                self.assertEqual(captured.research_readiness["sessions_remaining"], 180)
                self.assertEqual(captured.research_cycles[0]["readiness"]["state"],
                                 census_state)

    def test_scheduler_sanitizer_preserves_bounded_acceptance_readiness(self):
        readiness = {
            "schema": "research-readiness.v1",
            "state": "waiting_for_forward_sessions",
            "reason": "1 accepted full session; 30 are required",
            "complete_forward_partition_count": 2,
            "accepted_forward_partition_count": 1,
            "missing_acceptance_report_count": 1,
            "rejected_acceptance_report_count": 0,
            "invalid_acceptance_report_count": 0,
            "required_forward_sessions": 30,
            "readiness_session_count": 1,
            "recorded_sessions": 1,
            "required_sessions": 30,
            "sessions_remaining": 29,
            "readiness_basis": "accepted_full_sessions",
            "acceptance_required": True,
            "authorizing": False,
        }
        cycle = scheduler_output.structured_research_cycle({
            "schema": "research-cycle.v1",
            "status": "waiting_for_forward_sessions",
            "reason": readiness["reason"],
            "exit_code": 0,
            "outcomes": [],
            "proofs": False,
            "no_edge": False,
            "evidence_available": False,
            "readiness": readiness,
        })

        self.assertIsNotNone(cycle)
        self.assertEqual(cycle["readiness"]["state"],
                         "waiting_for_forward_sessions")
        self.assertEqual(
            cycle["readiness"]["accepted_forward_partition_count"], 1)
        derived = scheduler_output.derive_research_readiness(
            readiness=cycle["readiness"], now=100)
        self.assertEqual(derived["state"], "waiting_for_forward_sessions")
        self.assertEqual(derived["readiness_basis"], "accepted_full_sessions")
        self.assertFalse(derived["authorizing"])

    def test_scheduler_preserves_waiting_cycle_as_top_level_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text("mode: paper\nbroker:\n  paper: true\n",
                              encoding="utf-8")
            script = root / "cycle.sh"
            script.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$CYCLE_RESULT\"\n",
                encoding="utf-8")
            script.chmod(0o755)
            status_file = root / "research.json"
            readiness = {
                "schema": "research-readiness.v1",
                "state": "waiting_for_forward_sessions",
                "reason": "0 accepted full sessions; 30 are required",
                "complete_forward_partition_count": 0,
                "accepted_forward_partition_count": 0,
                "missing_acceptance_report_count": 0,
                "rejected_acceptance_report_count": 0,
                "invalid_acceptance_report_count": 0,
                "required_forward_sessions": 30,
                "readiness_session_count": 0,
                "recorded_sessions": 0,
                "required_sessions": 30,
                "sessions_remaining": 30,
                "readiness_basis": "accepted_full_sessions",
                "acceptance_required": True,
                "authorizing": False,
            }
            cycle = {
                "schema": "research-cycle.v1",
                "status": "waiting_for_forward_sessions",
                "reason": readiness["reason"],
                "exit_code": 0,
                "outcomes": [],
                "proofs": False,
                "no_edge": False,
                "evidence_available": False,
                "readiness": readiness,
            }
            scheduler._running = True
            with patch.dict(os.environ, {
                    **os.environ, "CYCLE_RESULT": json.dumps(cycle)}, clear=True):
                result = scheduler.run_scheduler(SimpleNamespace(
                    status_file=str(status_file), config=str(config),
                    script=str(script), root=str(root), hour=3, minute=0,
                    once=True, timeout_seconds=10,
                    output_limit_chars=4096))
            payload = json.loads(status_file.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "waiting_for_forward_sessions")
        self.assertEqual(payload["cycle_status"],
                         "waiting_for_forward_sessions")
        self.assertEqual(payload["research_cycle"]["status"],
                         "waiting_for_forward_sessions")

    def test_immature_recorder_waits_before_llm_and_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _session(root / "recorded", root / "acceptance",
                     "2020-01-02", accepted=True)
            result = _run(root, llm_enabled=True, max_source_bytes=1)
            direct = json.loads((root / "research-direct.json").read_text())

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        terminal = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(terminal["status"], "waiting_for_forward_sessions")
        self.assertFalse(terminal["evidence_available"])
        self.assertEqual(terminal["preflight"]["status"], "not_run")
        self.assertEqual(terminal["readiness"]["accepted_forward_partition_count"], 1)
        self.assertEqual(terminal["readiness"]["required_forward_sessions"], 30)
        self.assertEqual(direct["status"], "waiting_for_forward_sessions")
        self.assertEqual(direct["terminal"]["readiness"], terminal["readiness"])
        self.assertNotIn("research input exceeds capacity budget", terminal["reason"])

    def test_absent_recorder_history_waits_before_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _run(root, llm_enabled=True, max_source_bytes=1)
            direct = json.loads((root / "research-direct.json").read_text())

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        terminal = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(terminal["status"], "waiting_for_forward_sessions")
        self.assertFalse(terminal["evidence_available"])
        self.assertEqual(terminal["preflight"]["status"], "not_run")
        self.assertEqual(
            terminal["readiness"]["complete_forward_partition_count"], 0)
        self.assertEqual(
            terminal["readiness"]["accepted_forward_partition_count"], 0)
        self.assertEqual(direct["status"], "waiting_for_forward_sessions")

    def test_explicit_dataset_bypasses_recorder_acceptance_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "explicit.jsonl"
            dataset.write_text("\n", encoding="utf-8")
            result = _run(root, llm_enabled=True, max_source_bytes=1,
                          dataset=dataset)

        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        terminal = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(terminal["status"], "failed")
        self.assertIn("OPENAI_API_KEY", terminal["reason"])
        self.assertEqual(terminal["readiness"]["state"], "not_run")

    def test_mature_recorder_still_enforces_capacity_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = date(2020, 1, 1)
            for offset in range(30):
                day = (start + timedelta(days=offset)).isoformat()
                _session(root / "recorded", root / "acceptance", day,
                         accepted=True)
            result = _run(root, llm_enabled=False, max_source_bytes=1)

        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        terminal = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(terminal["status"], "failed")
        self.assertIn("exceeds capacity budget", terminal["reason"])
        self.assertEqual(terminal["readiness"]["accepted_forward_partition_count"], 30)
        self.assertEqual(terminal["readiness"]["state"], "input_ready")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
