"""Integration regressions for the real diagnostic cohort producer/consumers."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from agent.config import load_config
from deploy import health
from deploy.session_acceptance import summarize_session
from research.diagnostic_cohort_contract import validate_cohort_layout
from research.diagnostic_shadow import build_diagnostic_cohort
from tests.test_session_acceptance import CLOSE, OPEN, SESSION, SYMBOLS, _sample


ROOT = Path(__file__).resolve().parents[1]


def _raw_diagnostic(cohort: dict, captured_ts: float) -> dict:
    """Build operational telemetry around a real producer catalog."""
    arm_count = len(cohort["arms"])
    processed = int(captured_ts - OPEN) + 1
    cursors = {
        str(candidate): {
            "last_inserted_at": captured_ts - 1.0,
            "last_event_key": f"forward-{processed}",
            "processed_events": processed,
        }
        for candidate in cohort["candidate_identities"]
    }
    return {
        "schema": "diagnostic-shadow-coverage.v1",
        "enabled": True,
        "diagnostic": True,
        "authorizing": False,
        "gate_eligible": False,
        "promotion_eligible": False,
        "online_fdr": False,
        "actual_fills": 0,
        "actual_fill_claims": False,
        "realized_pnl_authorizing": False,
        "families_total": cohort["families_total"],
        "families_covered": cohort["families_total"],
        "families_observed": cohort["families_total"],
        "families_missing": [],
        "baseline_count": cohort["baseline_count"],
        "variant_count": cohort["variant_count"],
        "candidate_identities": list(cohort["candidate_identities"]),
        # Live coverage emits identity metadata only; the producer's full
        # configs remain in the preregistered cohort record, not every poll.
        "arms": [{key: arm[key] for key in (
            "candidate_id", "family", "role", "variant_id",
            "code_identity", "cohort_identity")}
                 for arm in cohort["arms"]],
        "cohort_contract": deepcopy(cohort["cohort_contract"]),
        "cohort_identity": cohort["cohort_identity"],
        "code_identity": cohort["code_identity"],
        "activation_identity": "activation-1",
        "activation_status": "active",
        "activation_event_watermark": {
            "count": 10,
            "decision_event_count": 5,
            "last_inserted_at": OPEN - 20.0,
            "last_event_key": "activation-event",
        },
        "warmup_session": "2026-09-07",
        "observation_status": "observed_no_quoteable_virtual_opens",
        "poll_duration_seconds": 0.5,
        "source_lag_seconds": 1.0,
        "processed_events": processed * arm_count,
        "processed_event_cursors": cursors,
        "health_cursor_matches": True,
    }


def _health_projection(diagnostic: dict, captured_ts: float,
                       candidate_errors: object = None) -> dict:
    heartbeat = {
        "status": "running",
        "updated_ts": captured_ts,
        "diagnostic_shadow": diagnostic,
    }
    if candidate_errors is not None:
        heartbeat["candidate_errors"] = candidate_errors
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shadow-health.json"
        path.write_text(json.dumps(heartbeat), encoding="utf-8")
        return health.shadow(path, 60.0, now=captured_ts)


class DiagnosticCohortIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = load_config(ROOT / "config.yaml")

    def _cohort(self, include_ibr: bool = False) -> dict:
        return build_diagnostic_cohort(
            self.runtime, code_identity="code-1", include_ibr=include_ibr)

    def _sample_set(self, cohort: dict, *, mutate_index: int | None = None,
                    mutate=None) -> list[dict]:
        samples = []
        timestamps = (OPEN, min(OPEN + 60.0, CLOSE), CLOSE)
        for index, timestamp in enumerate(timestamps):
            sample = _sample(float(timestamp))
            diagnostic = _raw_diagnostic(cohort, float(timestamp))
            if mutate_index == index and mutate is not None:
                mutate(diagnostic)
            sample["shadow"]["diagnostic_shadow"] = diagnostic
            sample["shadow"]["cohort_contract"] = deepcopy(
                cohort["cohort_contract"])
            sample["shadow"]["candidate_errors"] = {}
            sample["shadow"]["candidate_errors_present"] = True
            sample["identities"].update(
                code=cohort["code_identity"],
                cohort=cohort["cohort_identity"],
                activation="activation-1")
            samples.append(sample)
        return samples

    def _report(self, samples: list[dict]) -> dict:
        return summarize_session(
            samples, session=SESSION, expected=SYMBOLS, now=CLOSE + 1.0,
            start_tolerance=5.0, end_tolerance=5.0, max_sample_gap=65.0)

    def test_real_producer_publishes_exact_supported_layouts(self):
        for include_ibr, expected in ((False, (24, 12, 12, 12)),
                                      (True, (31, 13, 13, 18))):
            with self.subTest(include_ibr=include_ibr):
                cohort = self._cohort(include_ibr)
                contract = validate_cohort_layout(cohort)
                self.assertIsNotNone(contract)
                self.assertEqual(
                    tuple(contract[key] for key in (
                        "arm_count", "family_count", "baseline_count",
                        "variant_count")), expected)
                self.assertEqual(len(cohort["arms"]), expected[0])
                self.assertEqual(len(cohort["candidate_identities"]), expected[0])
                self.assertEqual(
                    {arm["candidate_id"] for arm in cohort["arms"]},
                    set(cohort["candidate_identities"]))
                self.assertEqual(cohort["cohort_contract"], contract)

    def test_real_24_and31_catalogs_project_ready_and_accept_full_session(self):
        for include_ibr in (False, True):
            with self.subTest(include_ibr=include_ibr):
                cohort = self._cohort(include_ibr)
                samples = self._sample_set(cohort)
                for sample in samples:
                    projected = _health_projection(
                        sample["shadow"]["diagnostic_shadow"],
                        sample["captured_ts"], {})
                    self.assertTrue(projected["coverage_ready"],
                                    projected["coverage_status"])
                    diagnostic = projected["diagnostic_shadow"]
                    self.assertEqual(diagnostic["candidate_count"], len(cohort["arms"]))
                    self.assertEqual(diagnostic["arms_total"], len(cohort["arms"]))
                    self.assertEqual(diagnostic["cursor_count"], len(cohort["arms"]))
                report = self._report(samples)
                self.assertTrue(report["accepted"], report["reasons"])
                self.assertEqual(
                    report["cohort_contract"], cohort["cohort_contract"])
                self.assertEqual(
                    report["post_activation_progress"]["arms"],
                    len(cohort["arms"]))

    def test_catalog_conflicts_fail_health_and_session_acceptance(self):
        cohort = self._cohort()

        def duplicate(diagnostic):
            diagnostic["arms"][1]["candidate_id"] = diagnostic["arms"][0]["candidate_id"]
            diagnostic["candidate_identities"][1] = diagnostic["candidate_identities"][0]

        def missing(diagnostic):
            diagnostic["arms"].pop()
            diagnostic["candidate_identities"].pop()
            diagnostic["processed_event_cursors"].popitem()
            diagnostic["processed_events"] -= 1

        def extra(diagnostic):
            extra_id = "shadow:diagnostic:unknown:variant"
            arm = deepcopy(diagnostic["arms"][0])
            arm.update(candidate_id=extra_id, family="unknown")
            diagnostic["arms"].append(arm)
            diagnostic["candidate_identities"].append(extra_id)
            diagnostic["processed_event_cursors"][extra_id] = {
                "last_inserted_at": diagnostic["source_lag_seconds"],
                "last_event_key": "extra",
                "processed_events": 1,
            }
            diagnostic["processed_events"] += 1

        def unknown_family(diagnostic):
            diagnostic["arms"][0]["family"] = "unknown"

        def string_count(diagnostic):
            diagnostic["families_total"] = "12"

        def malformed_identity(diagnostic):
            diagnostic["code_identity"] = 123

        def nested_disagreement(diagnostic):
            diagnostic["cohort_contract"]["variant_count"] = 13

        def nested_alias_conflict(diagnostic):
            diagnostic["cohort_contract"]["family_count"] = 12
            diagnostic["cohort_contract"]["families_total"] = 99

        mutations = {
            "duplicate": duplicate,
            "missing": missing,
            "extra": extra,
            "unknown_family": unknown_family,
            "string_count": string_count,
            "malformed_identity": malformed_identity,
            "nested_disagreement": nested_disagreement,
            "nested_alias_conflict": nested_alias_conflict,
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                diagnostic = _raw_diagnostic(cohort, OPEN + 50.0)
                mutate(diagnostic)
                projected = _health_projection(diagnostic, OPEN + 50.0, {})
                self.assertFalse(projected["coverage_ready"])
                samples = self._sample_set(
                    cohort, mutate_index=1, mutate=mutate)
                report = self._report(samples)
                self.assertFalse(report["accepted"], report)

    def test_shared_contract_rejects_contradictory_family_count_aliases(self):
        cohort = self._cohort()
        contradictory = deepcopy(cohort)
        contradictory["family_count"] = 12
        contradictory["families_total"] = 99
        self.assertIsNone(validate_cohort_layout(contradictory))

    def test_error_presence_unknown_ids_and_identity_drift_fail_closed(self):
        cohort = self._cohort(include_ibr=True)
        diagnostic = _raw_diagnostic(cohort, OPEN + 50.0)
        errors = {candidate: "evaluation failed"
                  for candidate in cohort["candidate_identities"]}
        projected = _health_projection(diagnostic, OPEN + 50.0, errors)
        self.assertFalse(projected["coverage_ready"])
        self.assertEqual(projected["coverage_status"], "candidate_errors_present")
        self.assertEqual(projected["candidate_error_count"], 31)

        unknown_error = _health_projection(
            diagnostic, OPEN + 50.0, {"shadow:diagnostic:unknown": "failed"})
        self.assertFalse(unknown_error["coverage_ready"])
        self.assertEqual(unknown_error["coverage_status"],
                         "candidate_errors_present")

        missing_error_map = _health_projection(diagnostic, OPEN + 50.0)
        self.assertFalse(missing_error_map["coverage_ready"])
        self.assertEqual(missing_error_map["coverage_status"],
                         "candidate_errors_unknown")

        def drift(diagnostic_value):
            diagnostic_value["cohort_identity"] = "different-cohort"

        samples = self._sample_set(cohort, mutate_index=1, mutate=drift)
        report = self._report(samples)
        self.assertFalse(report["accepted"])
        self.assertIn("cohort_contract_invalid", report["reasons"])

    def test_cursor_catalog_is_exact_and_extra_cursor_is_not_silently_dropped(self):
        cohort = self._cohort(include_ibr=True)
        diagnostic = _raw_diagnostic(cohort, OPEN + 50.0)
        extra_id = "shadow:diagnostic:extra:cursor"
        diagnostic["processed_event_cursors"][extra_id] = {
            "last_inserted_at": OPEN + 49.0,
            "last_event_key": "extra-cursor",
            "processed_events": 1,
        }
        diagnostic["processed_events"] += 1
        projected = _health_projection(diagnostic, OPEN + 50.0, {})
        self.assertFalse(projected["coverage_ready"])
        self.assertEqual(projected["coverage_status"], "arm_cursors_missing")
        self.assertIn(extra_id,
                      projected["diagnostic_shadow"]["processed_event_cursors"])
        self.assertEqual(projected["diagnostic_shadow"]["cursor_count"], 32)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
