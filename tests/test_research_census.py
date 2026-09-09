"""Metadata-only forward partition and acceptance report census tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from deploy.research_census import census


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _calendar(day: str) -> dict:
    return {
        "status": "open",
        "source": "alpaca_calendar",
        "open": f"{day}T13:30:00+00:00",
        "close": f"{day}T20:00:00+00:00",
    }


def _partition(recorded: Path, day: str, *, historical: bool = False) -> Path:
    sessions = recorded / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    name = f"market-{day}.csv"
    path = sessions / name
    path.write_text("event_key,timestamp\nrow,1\n", encoding="utf-8")
    (sessions / f"{name}.calendar.json").write_text(json.dumps({
        "schema": "recorder-partition-calendar.v1",
        "partition": name,
        **_calendar(day),
    }), encoding="utf-8")
    if historical:
        (sessions / f"{name}.source.json").write_text(json.dumps({
            "schema": "recorder-partition-source.v1",
            "partition": name,
            "source_mode": "historical_backfill",
        }), encoding="utf-8")
    return path


def _report(root: Path, day: str, *, accepted: bool) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    calendar = _calendar(day)
    close_ts = datetime.fromisoformat(calendar["close"]).timestamp()
    path = root / f"session-{day}.report.json"
    path.write_text(json.dumps({
        "schema": "session-acceptance-report.v1",
        "session": {"date": day, "open": calendar["open"],
                    "close": calendar["close"], "source": "alpaca_calendar"},
        "accepted": accepted,
        "status": "accepted" if accepted else "rejected",
        "operational_only": True,
        "authorizing": False,
        "promotion_eligible": False,
        "reasons": [] if accepted else ["coverage_end_missing"],
        "reason_counts": {} if accepted else {"coverage_end_missing": 1},
        "sample_counts": ({"total": 390, "healthy": 390, "failed": 0,
                           "valid_timestamps": 390}
                          if accepted else
                          {"total": 389, "healthy": 388, "failed": 1,
                           "valid_timestamps": 389}),
        "expected_symbols": ["AAPL", "MSFT"],
        "identities": {"deployment": "deploy-1", "code": "code-1",
                       "cohort": "cohort-1", "activation": "activation-1"},
        "coverage": {
            "first_sample_ts": datetime.fromisoformat(calendar["open"]).timestamp(),
            "last_sample_ts": close_ts,
            "open_ts": datetime.fromisoformat(calendar["open"]).timestamp(),
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
                "last_inserted_at": (
                    datetime.fromisoformat(calendar["open"]).timestamp() - 3600),
                "last_event_key": "activation-event",
                "count": 0, "decision_event_count": 0,
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
    return path


class ResearchCensusTests(unittest.TestCase):
    def test_absent_partition_root_is_zero_forward_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = census(
                partition_root=root / "recorded" / "sessions",
                recorded_root=root / "recorded", trusted_recorder=True,
                acceptance_root=root / "acceptance", now=NOW,
                backtest_minimum_sessions=30)

        self.assertEqual(result["complete_forward_partition_count"], 0)
        self.assertEqual(result["accepted_forward_partition_count"], 0)
        self.assertTrue(result["structurally_underpowered"])
        self.assertEqual(result["readiness"]["state"],
                         "waiting_for_forward_sessions")

    def test_historical_partition_never_counts_as_forward_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = root / "recorded"
            acceptance = root / "acceptance"
            _partition(recorded, "2026-09-08", historical=True)
            _report(acceptance, "2026-09-08", accepted=True)

            result = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1)

        self.assertEqual(result["complete_forward_partition_count"], 0)
        self.assertEqual(result["accepted_forward_partition_count"], 0)
        self.assertEqual(result["eligible_historical_partition_count"], 1)
        self.assertTrue(result["structurally_underpowered"])

    def test_missing_and_rejected_reports_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = root / "recorded"
            acceptance = root / "acceptance"
            _partition(recorded, "2026-09-07")
            _partition(recorded, "2026-09-08")
            _report(acceptance, "2026-09-08", accepted=False)

            result = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1)

        self.assertEqual(result["complete_forward_partition_count"], 2)
        self.assertEqual(result["accepted_forward_partition_count"], 0)
        self.assertEqual(result["missing_acceptance_report_count"], 1)
        self.assertEqual(result["rejected_acceptance_report_count"], 1)
        self.assertEqual(result["readiness"]["state"],
                         "waiting_for_forward_sessions")

    def test_only_exact_matching_report_is_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = root / "recorded"
            acceptance = root / "acceptance"
            _partition(recorded, "2026-09-08")
            arbitrary = _report(acceptance, "2026-09-08", accepted=True)
            arbitrary.rename(acceptance / "accepted.json")
            missing = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1)
            _report(acceptance, "2026-09-08", accepted=True)
            accepted = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1)

        self.assertEqual(missing["accepted_forward_partition_count"], 0)
        self.assertEqual(missing["missing_acceptance_report_count"], 1)
        self.assertEqual(accepted["accepted_forward_partition_count"], 1)
        self.assertFalse(accepted["structurally_underpowered"])

    def test_incomplete_accepted_report_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorded = root / "recorded"
            acceptance = root / "acceptance"
            _partition(recorded, "2026-09-08")
            report = _report(acceptance, "2026-09-08", accepted=True)
            payload = json.loads(report.read_text())
            payload.pop("coverage")
            report.write_text(json.dumps(payload), encoding="utf-8")

            result = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, acceptance_root=acceptance, now=NOW,
                backtest_minimum_sessions=1)

        self.assertEqual(result["accepted_forward_partition_count"], 0)
        self.assertEqual(result["invalid_acceptance_report_count"], 1)

    def test_accepted_label_cannot_mask_missing_progress_or_stale_market_data(self):
        mutations = (
            lambda payload: payload.pop("post_activation_progress"),
            lambda payload: payload["freshness"][
                "bar_publication_deadline_lag_seconds"].update(max=31.0),
            lambda payload: payload.update(warmup_sessions=[payload["session"]["date"]]),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate.__code__.co_firstlineno), \
                 tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                recorded = root / "recorded"
                acceptance = root / "acceptance"
                _partition(recorded, "2026-09-08")
                report = _report(acceptance, "2026-09-08", accepted=True)
                payload = json.loads(report.read_text())
                mutate(payload)
                report.write_text(json.dumps(payload), encoding="utf-8")

                result = census(
                    partition_root=recorded / "sessions",
                    recorded_root=recorded, trusted_recorder=True,
                    acceptance_root=acceptance, now=NOW,
                    backtest_minimum_sessions=1)

                self.assertEqual(result["accepted_forward_partition_count"], 0)
                self.assertEqual(result["invalid_acceptance_report_count"], 1)

    def test_omitted_acceptance_root_preserves_partition_upper_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            recorded = Path(directory) / "recorded"
            _partition(recorded, "2026-09-08")
            result = census(
                partition_root=recorded / "sessions", recorded_root=recorded,
                trusted_recorder=True, now=NOW, backtest_minimum_sessions=1)

        self.assertFalse(result["acceptance_required"])
        self.assertFalse(result["structurally_underpowered"])
        self.assertEqual(result["accepted_forward_partition_count"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
