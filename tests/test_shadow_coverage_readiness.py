"""Fail-closed current-poll readiness for the 24-arm diagnostic shadow."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from deploy import dashboard, health
from tests.test_parallel_runtime_status import _diagnostic_coverage


NOW = 100.0


class ShadowCoverageReadinessTests(unittest.TestCase):
    def _project(self, diagnostic: dict, *, updated_ts: float = NOW,
                 max_age: float = 180.0, candidate_errors=()) -> dict:
        heartbeat = {
            "status": "running",
            "updated_ts": updated_ts,
            "diagnostic_shadow": diagnostic,
        }
        if candidate_errors != ():
            heartbeat["candidate_errors"] = candidate_errors
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow-health.json"
            path.write_text(json.dumps(heartbeat), encoding="utf-8")
            return health.shadow(path, max_age, now=NOW)

    def test_all_24_matching_postactivation_cursors_are_ready(self):
        result = self._project(
            _diagnostic_coverage(now=NOW), candidate_errors={})

        self.assertTrue(result["ok"])
        self.assertTrue(result["coverage_ready"])
        self.assertEqual(result["coverage_status"], "ready")
        self.assertEqual(result["coverage_scope"], "current_poll_only")
        self.assertTrue(result["candidate_errors_clear"])
        self.assertEqual(result["candidate_error_count"], 0)
        diagnostic = result["diagnostic_shadow"]
        self.assertEqual(diagnostic["cursor_status"], "ready")
        self.assertEqual(diagnostic["cursor_count"], 24)
        self.assertEqual(len(diagnostic["processed_event_cursors"]), 24)
        self.assertFalse(diagnostic["authorizing"])
        self.assertFalse(diagnostic["proof_authority"])
        self.assertNotIn("accepted", result)
        self.assertNotIn("accepted", diagnostic)

    def test_fresh_heartbeat_cannot_hide_invalid_arm_progress(self):
        cases = []

        stale = _diagnostic_coverage(now=NOW)
        stale["activation_event_watermark"]["last_inserted_at"] = 50.0
        first = stale["candidate_identities"][0]
        stale["processed_event_cursors"][first]["last_inserted_at"] = 69.0
        cases.append(("stale", "arm_cursor_stale", stale))

        missing = _diagnostic_coverage(now=NOW)
        missing["processed_event_cursors"].pop(
            missing["candidate_identities"][0])
        cases.append(("missing", "arm_cursors_missing", missing))

        mismatched = _diagnostic_coverage(now=NOW)
        mismatched["processed_event_cursors"][
            mismatched["candidate_identities"][0]
        ]["last_event_key"] = "different-forward-event"
        cases.append(("mismatched", "arm_cursors_mismatch", mismatched))

        future = _diagnostic_coverage(now=NOW)
        future["processed_event_cursors"][
            future["candidate_identities"][0]
        ]["last_inserted_at"] = NOW + 6.0
        cases.append(("future", "arm_cursor_future", future))

        preactivation = _diagnostic_coverage(now=NOW)
        marker = preactivation["activation_event_watermark"]
        preactivation["processed_event_cursors"][
            preactivation["candidate_identities"][0]
        ].update({
            "last_inserted_at": marker["last_inserted_at"],
            "last_event_key": marker["last_event_key"],
        })
        cases.append((
            "preactivation", "arm_post_activation_progress_missing",
            preactivation))

        zero_progress = _diagnostic_coverage(now=NOW)
        zero_progress["processed_event_cursors"][
            zero_progress["candidate_identities"][0]
        ]["processed_events"] = 0
        cases.append((
            "zero-progress", "arm_post_activation_progress_missing",
            zero_progress))

        for case, expected_status, diagnostic in cases:
            with self.subTest(case=case):
                result = self._project(diagnostic, candidate_errors={})
                self.assertTrue(result["ok"])
                self.assertFalse(result["coverage_ready"])
                self.assertEqual(result["coverage_status"], expected_status)

    def test_catalog_code_and_cohort_must_match_exactly(self):
        mutations = []
        wrong_catalog = _diagnostic_coverage(now=NOW)
        wrong_catalog["candidate_identities"][0] = "shadow:not-an-arm"
        mutations.append(wrong_catalog)

        wrong_code = _diagnostic_coverage(now=NOW)
        wrong_code["arms"][0]["code_identity"] = "different-code"
        mutations.append(wrong_code)

        wrong_cohort = _diagnostic_coverage(now=NOW)
        wrong_cohort["arms"][0]["cohort_identity"] = "different-cohort"
        mutations.append(wrong_cohort)

        for diagnostic in mutations:
            with self.subTest(status=diagnostic["arms"][0]["candidate_id"]):
                result = self._project(diagnostic, candidate_errors={})
                self.assertTrue(result["ok"])
                self.assertFalse(result["coverage_ready"])

    def test_errors_or_missing_observations_never_become_ready(self):
        failed = self._project(
            _diagnostic_coverage(now=NOW),
            candidate_errors={"shadow:family_0:baseline": "failed"})
        self.assertTrue(failed["ok"])
        self.assertFalse(failed["coverage_ready"])
        self.assertEqual(failed["coverage_status"], "candidate_errors_present")

        malformed = self._project(
            _diagnostic_coverage(now=NOW), candidate_errors=False)
        self.assertTrue(malformed["ok"])
        self.assertFalse(malformed["coverage_ready"])
        self.assertEqual(
            malformed["coverage_status"], "candidate_errors_present")

        unknown = self._project(_diagnostic_coverage(now=NOW))
        self.assertTrue(unknown["ok"])
        self.assertFalse(unknown["coverage_ready"])
        self.assertEqual(unknown["coverage_status"], "candidate_errors_unknown")

        unobserved = _diagnostic_coverage(now=NOW)
        unobserved["observation_status"] = "no_post_activation_events"
        result = self._project(unobserved, candidate_errors={})
        self.assertTrue(result["ok"])
        self.assertFalse(result["coverage_ready"])
        self.assertEqual(
            result["coverage_status"], "post_activation_observation_missing")
        self.assertNotIn("accepted", result)

    def test_heartbeat_age_is_independent_from_strict_data_age(self):
        ready = self._project(
            _diagnostic_coverage(now=NOW), updated_ts=0.0,
            max_age=180.0, candidate_errors={})
        self.assertTrue(ready["ok"])
        self.assertEqual(ready["heartbeat_age_seconds"], 100.0)
        self.assertTrue(ready["coverage_ready"])

        stale_data = self._project(
            _diagnostic_coverage(source_lag=31.0, now=NOW), updated_ts=0.0,
            max_age=180.0, candidate_errors={})
        self.assertTrue(stale_data["ok"])
        self.assertFalse(stale_data["coverage_ready"])
        self.assertEqual(stale_data["coverage_status"], "source_data_stale")
        self.assertEqual(
            stale_data["diagnostic_shadow"]["data_max_age_seconds"], 30.0)

    def test_malformed_projection_is_bounded_to_the_fixed_cohort(self):
        diagnostic = deepcopy(_diagnostic_coverage(now=NOW))
        extra_id = "shadow:extra:variant"
        diagnostic["candidate_identities"].append(extra_id)
        diagnostic["arms"].append({
            "candidate_id": extra_id,
            "family": "extra",
            "role": "variant",
            "variant_id": "rule.extra.variant",
            "code_identity": diagnostic["code_identity"],
            "cohort_identity": diagnostic["cohort_identity"],
            "secret": "must-not-escape",
        })
        diagnostic["processed_event_cursors"][extra_id] = {
            "last_inserted_at": NOW - 5.0,
            "last_event_key": "forward-event",
            "processed_events": 1,
            "secret": "must-not-escape",
        }
        diagnostic["processed_events"] += 1

        result = self._project(diagnostic, candidate_errors={})
        projected = result["diagnostic_shadow"]
        self.assertFalse(result["coverage_ready"])
        self.assertLessEqual(projected["candidate_count"], 24)
        self.assertLessEqual(projected["arms_total"], 24)
        self.assertLessEqual(len(projected["processed_event_cursors"]), 24)
        self.assertNotIn("arms", projected)
        self.assertNotIn("candidate_identities", projected)
        self.assertNotIn("must-not-escape", json.dumps(projected))

    def test_dashboard_names_the_compacted_counter_as_no_trade_decisions(self):
        self.assertIn("evaluations / no-trade decisions", dashboard.HTML)
        self.assertNotIn("evaluations / no signal", dashboard.HTML)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
