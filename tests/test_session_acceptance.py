"""Bounded operational session acceptance evidence tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy import shadow as shadow_service
from deploy.session_acceptance import (
    DIAGNOSTIC_COVERAGE_SCHEMA,
    REPORT_SCHEMA,
    SAMPLE_SCHEMA,
    SessionAcceptanceError,
    capture_sample,
    finalize_session,
    read_session_samples,
    record_poll,
    report_file,
    session_file,
    summarize_session,
)


DATE = "2026-09-08"
WARMUP_DATE = "2026-09-07"
OPEN = datetime.fromisoformat(f"{DATE}T13:30:00+00:00").timestamp()
CLOSE = OPEN + 100.0
SYMBOLS = ["AAPL", "MSFT"]
SESSION = {
    "date": DATE,
    "open": datetime.fromtimestamp(OPEN, timezone.utc).isoformat(),
    "close": datetime.fromtimestamp(CLOSE, timezone.utc).isoformat(),
    "open_ts": OPEN,
    "close_ts": CLOSE,
    "source": "alpaca_calendar",
}


def _arms(processed_events: int, cursor_at: float) -> tuple[list[dict], dict]:
    arms = []
    cursors = {}
    for family_number in range(12):
        family = f"family_{family_number:02d}"
        for role in ("baseline", "variant"):
            candidate = f"shadow:diagnostic:{family}:{role}"
            arms.append({
                "candidate_id": candidate,
                "family": family,
                "role": role,
                "variant_id": f"rule.{family}.{role}",
                "code_identity": "code-1",
                "cohort_identity": "cohort-1",
            })
            cursors[candidate] = {
                "last_inserted_at": cursor_at,
                "last_event_key": f"event-{processed_events}",
                "processed_events": processed_events,
            }
    return arms, cursors


def _sample(ts: float, *, code: str = "code-1", warmup: str = WARMUP_DATE,
            stale: bool = False, source_stale: bool = False,
            arm_count: int = 24, processed_events: int | None = None) -> dict:
    processed = (max(1, int(ts - OPEN) + 1)
                 if processed_events is None else processed_events)
    cursor_at = min(float(ts), OPEN - 10.0 + max(0, processed))
    arms, cursors = _arms(processed, cursor_at)
    arms = arms[:arm_count]
    candidates = [arm["candidate_id"] for arm in arms]
    cursors = {key: value for key, value in cursors.items() if key in candidates}
    diag = {
        "schema": DIAGNOSTIC_COVERAGE_SCHEMA,
        "enabled": True,
        "diagnostic": True,
        "authorizing": False,
        "gate_eligible": False,
        "promotion_eligible": False,
        "online_fdr": False,
        "actual_fills": 0,
        "actual_fill_claims": False,
        "realized_pnl_authorizing": False,
        "families_total": 12,
        "families_covered": 12,
        "families_missing": [],
        "baseline_count": 12,
        "variant_count": 12,
        "candidate_identities": candidates,
        "arms": [{**arm, "code_identity": code} for arm in arms],
        "cohort_identity": "cohort-1",
        "code_identity": code,
        "activation_identity": "activation-1",
        "activation_status": "active",
        "activation_event_watermark": {
            "count": 10, "decision_event_count": 5,
            "last_inserted_at": OPEN - 20.0,
            "last_event_key": "activation-event",
        },
        "warmup_session": warmup,
        "observation_status": "observed_no_quoteable_virtual_opens",
        "poll_duration_seconds": 0.5,
        "source_lag_seconds": 31.0 if source_stale else 1.0,
        "processed_events": processed * len(cursors),
        "processed_event_cursors": cursors,
        "health_cursor_matches": True,
    }
    quote_age = 31.0 if stale else 1.0
    completed_bar = (
        None if ts < OPEN + 60.0 else
        min(CLOSE, OPEN + int((ts - OPEN) // 60.0) * 60.0))
    publication_lag = (
        0.0 if ts <= OPEN + 90.0 or completed_bar is None else
        0.0 if completed_bar >= CLOSE - 5.0 else
        max(0.0, ts - min(CLOSE, completed_bar + 60.0)))
    observations = {
        symbol: {
            "quote_age_seconds": quote_age,
            "bar_watermark": (datetime.fromtimestamp(
                completed_bar, timezone.utc).isoformat()
                if completed_bar is not None else None),
            "bar_age_seconds": (ts - completed_bar
                                if completed_bar is not None else None),
            "bar_publication_deadline_lag_seconds": publication_lag,
        }
        for symbol in SYMBOLS
    }
    return {
        "schema": SAMPLE_SCHEMA,
        "captured_ts": ts,
        "session": dict(SESSION, open_ts=None, close_ts=None),
        "expected_symbols": list(SYMBOLS),
        "recorder": {
            "status": "recording",
            "ok": True,
            "fresh": True,
            "market_session_status": "open" if ts < CLOSE else "closed",
            "observation_ages": observations,
            "cadence": {
                "configured_interval_seconds": 5.0,
                "realized_interval_seconds": 5.0,
            },
            "partition_bytes": int(ts - OPEN + 100),
        },
        "shadow": {
            "status": "running",
            "ok": True,
            "fresh": True,
            "last_error": None,
            "candidate_errors": {},
            "candidate_errors_present": True,
            "candidate_error_count": 0,
            "poll_duration_seconds": 0.5,
            "diagnostic_shadow": diag,
        },
        "identities": {
            "deployment_recorder": "deploy-1",
            "deployment_shadow": "deploy-1",
            "deployment": "deploy-1",
            "code": code,
            "cohort": "cohort-1",
            "activation": "activation-1",
        },
        "warmup_session": warmup,
        "operational_only": True,
        "authorizing": False,
        "promotion_eligible": False,
    }


def _write_index(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(exist_ok=True)
    (root / "sessions" / f"market-{DATE}.csv").write_text(
        "event_key,timestamp\nrow,1\n", encoding="utf-8")
    (root / ".recorder-index.json").write_text(json.dumps({
        "schema": "recorder-index.v1",
        "configured_symbols": SYMBOLS,
        "bar_coverage": {
            symbol: {"observed_at": datetime.fromtimestamp(
                OPEN - 1.0, timezone.utc).isoformat()}
            for symbol in SYMBOLS
        },
        "session_calendar": {DATE: {
            "status": "open", "source": "alpaca_calendar",
            "open": SESSION["open"], "close": SESSION["close"],
        }},
    }), encoding="utf-8")


class SessionAcceptanceTests(unittest.TestCase):
    def test_session_paths_require_exact_iso_date(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(SessionAcceptanceError):
                report_file(root, "../outside")
            self.assertFalse((root.parent / "outside.report.json").exists())

    def _report(self, samples, **kwargs):
        return summarize_session(samples, session=SESSION, expected=SYMBOLS,
                                 now=CLOSE + 1.0, start_tolerance=5.0,
                                 end_tolerance=5.0, max_sample_gap=10.0,
                                 **kwargs)

    def test_full_open_to_close_session_passes(self):
        samples = [_sample(float(ts)) for ts in range(
            int(OPEN), int(CLOSE) + 1, 5)]
        report = self._report(samples)
        self.assertEqual(report["schema"], REPORT_SCHEMA)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["sample_counts"], {
            "total": 21, "healthy": 21, "failed": 0, "valid_timestamps": 21})
        self.assertEqual(report["storage_growth"]["growth_bytes"], 100)
        self.assertEqual(report["duration_seconds"]["p95"], 0.5)
        self.assertFalse(report["authorizing"])

    def test_warmup_session_is_excluded(self):
        report = self._report([
            _sample(OPEN, warmup=DATE), _sample(CLOSE, warmup=DATE)])
        self.assertFalse(report["accepted"])
        self.assertIn("warmup_session_excluded", report["reasons"])

    def test_partial_coverage_and_gap_reject(self):
        report = self._report([
            _sample(OPEN + 10), _sample(OPEN + 70), _sample(CLOSE)])
        self.assertFalse(report["accepted"])
        self.assertIn("coverage_start_missing", report["reasons"])
        self.assertIn("sample_gap_detected", report["reasons"])

    def test_missing_end_rejects(self):
        report = self._report([_sample(OPEN), _sample(OPEN + 50)])
        self.assertFalse(report["accepted"])
        self.assertIn("coverage_end_missing", report["reasons"])

    def test_stale_symbol_observation_rejects_and_counts_symbol(self):
        report = self._report([_sample(OPEN, stale=True), _sample(CLOSE)])
        self.assertFalse(report["accepted"])
        self.assertEqual(report["symbol_failure_counts"]["AAPL"], 1)
        self.assertIn("symbol:AAPL:quote_stale", report["reasons"])

    def test_missing_24_arm_cohort_rejects(self):
        report = self._report([
            _sample(OPEN, arm_count=23), _sample(CLOSE, arm_count=23)])
        self.assertFalse(report["accepted"])
        self.assertIn("arms_count_invalid", report["reasons"])

    def test_identity_drift_rejects(self):
        report = self._report([_sample(OPEN), _sample(CLOSE, code="code-2")])
        self.assertFalse(report["accepted"])
        self.assertIn("identity_drift", report["reasons"])

    def test_activation_mismatch_rejects(self):
        sample = _sample(OPEN)
        sample["identities"]["activation"] = "activation-other"
        report = self._report([sample, _sample(CLOSE)])
        self.assertFalse(report["accepted"])
        self.assertIn("activation_identity_mismatch", report["reasons"])

    def test_completed_bars_remain_fresh_across_thirty_second_poll_cadence(self):
        samples = [_sample(OPEN + offset) for offset in (0, 30, 60, 90, 100)]
        report = summarize_session(
            samples, session=SESSION, expected=SYMBOLS, now=CLOSE + 1.0,
            start_tolerance=5.0, end_tolerance=5.0, max_sample_gap=35.0)
        self.assertTrue(report["accepted"], report["reasons"])
        self.assertEqual(
            report["freshness"]["bar_publication_deadline_lag_seconds"]["max"],
            0.0)

    def test_refetching_stale_bar_does_not_refresh_publication_deadline(self):
        samples = [_sample(OPEN), _sample(CLOSE)]
        for observation in samples[-1]["recorder"]["observation_ages"].values():
            observation["bar_watermark"] = datetime.fromtimestamp(
                OPEN - 60.0, timezone.utc).isoformat()
            observation["bar_age_seconds"] = 160.0
            observation["bar_publication_deadline_lag_seconds"] = 40.0
            observation["bar_ingestion_age_seconds"] = 0.0
        report = self._report(samples)
        self.assertFalse(report["accepted"])
        self.assertIn("symbol:AAPL:bar_publication_stale", report["reasons"])

    def test_stale_source_lag_rejects_without_requiring_signals(self):
        report = self._report([
            _sample(OPEN), _sample(CLOSE, source_stale=True)])
        self.assertFalse(report["accepted"])
        self.assertIn("shadow_source_lag_stale", report["reasons"])

    def test_all_arms_must_advance_after_activation_across_session(self):
        report = self._report([
            _sample(OPEN, processed_events=0),
            _sample(CLOSE, processed_events=0)])
        self.assertFalse(report["accepted"])
        self.assertIn("arm_post_activation_progress_missing", report["reasons"])
        self.assertIn("session_arm_progress_missing", report["reasons"])

    def test_positive_but_unchanged_arm_cursors_do_not_count_as_session_progress(self):
        report = self._report([
            _sample(OPEN, processed_events=1),
            _sample(CLOSE, processed_events=1)])
        self.assertFalse(report["accepted"])
        self.assertIn("arm_cursor_stale", report["reasons"])
        self.assertIn("session_arm_progress_missing", report["reasons"])

    def test_activation_must_precede_the_accepted_session(self):
        samples = [_sample(OPEN), _sample(CLOSE)]
        for sample in samples:
            marker = sample["shadow"]["diagnostic_shadow"][
                "activation_event_watermark"]
            marker["last_inserted_at"] = OPEN
            marker["last_event_key"] = "activation-at-open"
        report = self._report(samples)
        self.assertFalse(report["accepted"])
        self.assertIn("activation_not_pre_session", report["reasons"])

    def test_future_arm_cursor_rejects(self):
        samples = [_sample(OPEN), _sample(CLOSE)]
        final_diag = samples[-1]["shadow"]["diagnostic_shadow"]
        for cursor in final_diag["processed_event_cursors"].values():
            cursor["last_inserted_at"] = CLOSE + 10.0
        report = self._report(samples)
        self.assertFalse(report["accepted"])
        self.assertIn("arm_cursor_future", report["reasons"])

    def test_future_warmup_session_rejects(self):
        report = self._report([
            _sample(OPEN, warmup="2026-09-09"),
            _sample(CLOSE, warmup="2026-09-09")])
        self.assertFalse(report["accepted"])
        self.assertIn("warmup_chronology_invalid", report["reasons"])

    def test_session_bounds_must_be_timezone_aware_and_same_market_day(self):
        malformed = dict(SESSION, open=f"{DATE}T13:30:00")
        report = summarize_session(
            [_sample(OPEN), _sample(CLOSE)], session=malformed,
            expected=SYMBOLS, now=CLOSE + 1.0, start_tolerance=5.0,
            end_tolerance=5.0, max_sample_gap=110.0)
        self.assertFalse(report["accepted"])
        self.assertIn("session_calendar_invalid", report["reasons"])

    def test_future_sample_rejects(self):
        report = self._report([_sample(OPEN), _sample(CLOSE + 5)])
        self.assertFalse(report["accepted"])
        self.assertIn("future_evidence", report["reasons"])

    def test_corrupt_jsonl_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = session_file(directory, DATE)
            path.write_text("{broken\n", encoding="utf-8")
            with self.assertRaises(SessionAcceptanceError) as context:
                read_session_samples(directory, DATE)
            self.assertIn("session_samples_corrupt", str(context.exception))

    def test_oversized_sample_journal_finalizes_as_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recorded"
            output = Path(directory) / "acceptance"
            _write_index(root)
            output.mkdir()
            session_file(output, DATE).write_text("{}\n", encoding="utf-8")

            with patch("deploy.session_acceptance.MAX_SAMPLE_FILE_BYTES", 1):
                report = finalize_session(root, output, DATE, now=CLOSE)

        self.assertFalse(report["accepted"])
        self.assertEqual(report["session"]["date"], DATE)
        self.assertIn("session_samples_size_bound_exceeded", report["reasons"])

    def test_declared_freshness_is_bounded_at_thirty_seconds(self):
        sample = _sample(OPEN)
        sample["freshness_threshold_seconds"] = 120.0
        report = self._report([sample, _sample(CLOSE)])
        self.assertFalse(report["accepted"])
        self.assertIn("freshness_threshold_invalid", report["reasons"])

    def test_malformed_shadow_health_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recorded"
            _write_index(root)
            shadow = Path(directory) / "shadow-health.json"
            shadow.write_text("{broken\n", encoding="utf-8")
            with self.assertRaises(SessionAcceptanceError) as context:
                capture_sample(root, shadow, DATE, now=OPEN)
            self.assertIn("shadow_health_corrupt", str(context.exception))

    def test_empty_shadow_health_object_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recorded"
            _write_index(root)
            shadow = Path(directory) / "shadow-health.json"
            shadow.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(SessionAcceptanceError) as context:
                capture_sample(root, shadow, DATE, now=OPEN)
            self.assertIn("shadow_health_schema_invalid", str(context.exception))

    def test_capture_uses_strict_freshness_even_for_slow_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recorded"
            _write_index(root)
            shadow = Path(directory) / "shadow-health.json"
            shadow.write_text(json.dumps({
                "schema": "shadow-health.v1", "status": "running",
                "updated_ts": OPEN, "candidate_errors": {},
                "diagnostic_shadow": _sample(OPEN)["shadow"]["diagnostic_shadow"],
            }), encoding="utf-8")
            recorder_health = {
                "status": "recording", "ok": True, "fresh": True,
                "market_session_status": "open", "data_feed": "iex",
                "observation_ages": {
                    symbol: {"quote_age_seconds": 1.0,
                             "bar_watermark": None,
                             "bar_age_seconds": None}
                    for symbol in SYMBOLS
                },
                "cadence": {"configured_interval_seconds": 60.0,
                            "realized_interval_seconds": 60.0},
                "provenance": {"identity": "deploy-1"},
            }
            shadow_health = {
                "status": "running", "ok": True, "fresh": True,
                "last_error": None, "provenance": {"identity": "deploy-1"},
            }
            with patch("deploy.session_acceptance.health.recorder",
                       return_value=recorder_health) as recorder_probe, \
                 patch("deploy.session_acceptance.health.shadow",
                       return_value=shadow_health) as shadow_probe:
                sample = capture_sample(root, shadow, DATE, now=OPEN, max_age=120)

        self.assertEqual(sample["freshness_threshold_seconds"], 30.0)
        self.assertEqual(sample["sample_reasons"], [])
        self.assertEqual(recorder_probe.call_args.args[1], 30.0)
        self.assertEqual(shadow_probe.call_args.args[1], 30.0)

    def test_record_poll_restarts_idempotently_and_finalizes_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recorded"
            output = Path(directory) / "acceptance"
            shadow = Path(directory) / "shadow-health.json"
            _write_index(root)

            def captured(_root, _shadow, _date, *, now, **_kwargs):
                sample = _sample(float(now))
                sample["sample_id"] = f"poll:{now:.6f}"
                return sample

            with patch("deploy.session_acceptance.capture_sample",
                       side_effect=captured):
                first = record_poll(
                    root, shadow, output, session_date=DATE, now=OPEN,
                    interval_seconds=50, gap_tolerance_seconds=0)
                duplicate = record_poll(
                    root, shadow, output, session_date=DATE, now=OPEN,
                    interval_seconds=50, gap_tolerance_seconds=0)
                middle = record_poll(
                    root, shadow, output, session_date=DATE, now=OPEN + 50,
                    interval_seconds=50, gap_tolerance_seconds=0)
                final = record_poll(
                    root, shadow, output, session_date=DATE, now=CLOSE,
                    interval_seconds=50, gap_tolerance_seconds=0)

            self.assertEqual(first["status"], "recorded")
            self.assertEqual(duplicate["status"], "duplicate")
            self.assertEqual(middle["status"], "recorded")
            self.assertEqual(len(read_session_samples(output, DATE)), 3)
            self.assertEqual(final["status"], "finalized")
            self.assertTrue(final["accepted"])
            persisted = json.loads(report_file(output, DATE).read_text())
            self.assertTrue(persisted["accepted"])
            self.assertEqual(persisted["coverage"]["max_sample_gap_seconds"], 50.0)
            self.assertEqual(list(output.glob("*.tmp")), [])

    def test_failed_close_capture_is_durable_and_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recorded"
            output = Path(directory) / "acceptance"
            shadow = Path(directory) / "shadow-health.json"
            _write_index(root)

            def captured(_root, _shadow, _date, *, now, **_kwargs):
                if float(now) == CLOSE:
                    raise SessionAcceptanceError("shadow_health_corrupt")
                sample = _sample(float(now))
                sample["sample_id"] = f"poll:{now:.6f}"
                return sample

            with patch("deploy.session_acceptance.capture_sample",
                       side_effect=captured):
                record_poll(root, shadow, output, session_date=DATE, now=OPEN,
                            interval_seconds=100, gap_tolerance_seconds=0)
                final = record_poll(
                    root, shadow, output, session_date=DATE, now=CLOSE,
                    interval_seconds=100, gap_tolerance_seconds=0)

        self.assertFalse(final["accepted"])
        self.assertEqual(final["capture_error"], "shadow_health_corrupt")
        self.assertIn("capture_failed:shadow_health_corrupt",
                      final["report"]["reasons"])

    def test_missing_calendar_boundary_persists_rejected_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recorded"
            output = Path(directory) / "acceptance"
            _write_index(root)
            index_path = root / ".recorder-index.json"
            index = json.loads(index_path.read_text())
            index["session_calendar"][DATE]["close"] = None
            index_path.write_text(json.dumps(index), encoding="utf-8")

            result = record_poll(
                root, Path(directory) / "shadow-health.json", output,
                session_date=DATE, now=CLOSE)

        self.assertEqual(result["status"], "finalized")
        self.assertFalse(result["accepted"])
        self.assertIn("session_calendar_invalid", result["report"]["reasons"][0])

    def test_shadow_once_records_acceptance_after_health_publish(self):
        order: list[str] = []

        class Runner:
            def __init__(self, _config):
                pass

            def run_once(self):
                return {"candidate_errors": {}}

        def write_health(*_args, **_kwargs):
            order.append("health")
            return {}

        def poll(*_args, **_kwargs):
            order.append("acceptance")
            return {"schema": "session-acceptance.v1", "status": "recorded"}

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(shadow_service, "ShadowRunner", Runner), \
             patch.object(shadow_service, "_write_health", side_effect=write_health), \
             patch.object(shadow_service, "record_poll", side_effect=poll) as recorder, \
             patch("builtins.print"):
            root = Path(directory)
            result = shadow_service.main([
                "--no-diagnostic", "--once",
                "--corpus", str(root / "recorded" / "data.csv"),
                "--shadow-db", str(root / "shadow.sqlite3"),
            ])

        self.assertEqual(result, 0)
        self.assertEqual(order, ["health", "acceptance"])
        self.assertEqual(recorder.call_args.args[0], root / "recorded")
        self.assertEqual(recorder.call_args.args[2], root / "session-acceptance")
        self.assertEqual(recorder.call_args.kwargs["freshness_seconds"], 30.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
