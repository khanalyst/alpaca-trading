"""Session evidence cannot bypass the stricter current-poll cohort contract."""

from copy import deepcopy
import unittest

from deploy import health
from deploy.session_acceptance import summarize_session
from tests.test_session_acceptance import CLOSE, OPEN, SESSION, SYMBOLS, _sample


class SessionAcceptanceReadinessParityTests(unittest.TestCase):
    def _samples(self):
        return [_sample(float(stamp))
                for stamp in range(int(OPEN), int(CLOSE) + 1, 5)]

    def _report(self, samples):
        return summarize_session(
            samples, session=SESSION, expected=SYMBOLS, now=CLOSE + 1.0,
            start_tolerance=5.0, end_tolerance=5.0, max_sample_gap=10.0)

    def _readiness(self, sample):
        shadow = sample["shadow"]
        errors = shadow.get("candidate_errors")
        return health._shadow_diagnostic_summary(
            shadow["diagnostic_shadow"], now=sample["captured_ts"],
            candidate_errors_present=(
                shadow.get("candidate_errors_present") is True and
                "candidate_errors" in shadow),
            candidate_errors_clear=isinstance(errors, dict) and not errors,
            candidate_error_count=len(errors) if isinstance(errors, dict) else None,
        )["readiness_status"]

    def test_normalized_producer_contract_passes_both_checks(self):
        samples = self._samples()
        self.assertTrue(all(self._readiness(row) == "ready" for row in samples))
        report = self._report(samples)
        self.assertTrue(report["accepted"], report["reasons"])
        self.assertFalse(report["authorizing"])

    def test_invalid_raw_diagnostic_cannot_pass_session_when_health_rejects(self):
        mutations = {
            "variant_id_missing": lambda d: d["arms"][0].pop("variant_id"),
            "arm_code_missing": lambda d: d["arms"][0].pop("code_identity"),
            "online_fdr_true": lambda d: d.update(online_fdr=True),
            "actual_fill_claims_true": lambda d: d.update(actual_fill_claims=True),
            "realized_authorizing_true": lambda d: d.update(realized_pnl_authorizing=True),
            "boolean_actual_fills": lambda d: d.update(actual_fills=False),
            "families_covered_zero": lambda d: d.update(families_covered=0),
            "families_missing_nonempty": lambda d: d.update(families_missing=["missing"]),
            "duration_missing": lambda d: d.pop("poll_duration_seconds"),
            "boolean_source_lag": lambda d: d.update(source_lag_seconds=True),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                samples = self._samples()
                mutate(samples[10]["shadow"]["diagnostic_shadow"])
                readiness = self._readiness(samples[10])
                self.assertNotEqual(readiness, "ready")
                report = self._report(samples)
                self.assertFalse(report["accepted"])
                self.assertIn(readiness, report["reasons"])

    def test_malformed_error_map_cannot_be_reinterpreted_as_no_errors(self):
        for errors in (False, 0, [], "malformed", None):
            with self.subTest(errors=errors):
                samples = self._samples()
                samples[10]["shadow"]["candidate_errors"] = deepcopy(errors)
                readiness = self._readiness(samples[10])
                self.assertNotEqual(readiness, "ready")
                report = self._report(samples)
                self.assertFalse(report["accepted"])
                self.assertIn(readiness, report["reasons"])

    def test_missing_error_map_is_unknown_despite_claimed_presence(self):
        samples = self._samples()
        samples[10]["shadow"].pop("candidate_errors")
        report = self._report(samples)
        self.assertFalse(report["accepted"])
        self.assertIn("candidate_errors_unknown", report["reasons"])


if __name__ == "__main__":
    unittest.main()
