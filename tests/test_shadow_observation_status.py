"""Unknown or warm-up telemetry cannot claim observed shadow readiness."""

from copy import deepcopy
import unittest

from deploy import health
from deploy.session_acceptance import summarize_session
from tests.test_parallel_runtime_status import _diagnostic_coverage
from tests.test_session_acceptance import CLOSE, OPEN, SESSION, SYMBOLS, _sample


OBSERVED_STATUSES = (
    "quoteable_virtual_observations",
    "signals_unpriced_no_virtual_fill_claim",
    "observed_no_quoteable_virtual_opens",
)
UNOBSERVED_STATUSES = (
    None, "", "failed", "unknown", "awaiting_forward_activation",
    "no_post_activation_events", "warmup_signals_not_evaluated",
    True, 1, [], {}, "quoteable_virtual_observations ",
)


class ShadowObservationStatusTests(unittest.TestCase):
    def _health(self, status):
        diagnostic = _diagnostic_coverage(now=100.0)
        diagnostic["observation_status"] = status
        return health._shadow_diagnostic_summary(
            diagnostic, now=100.0, candidate_errors_present=True,
            candidate_errors_clear=True, candidate_error_count=0)

    def _report(self, status):
        samples = [_sample(float(stamp))
                   for stamp in range(int(OPEN), int(CLOSE) + 1, 5)]
        for sample in samples:
            sample["shadow"]["diagnostic_shadow"]["observation_status"] = (
                deepcopy(status))
        return summarize_session(
            samples, session=SESSION, expected=SYMBOLS, now=CLOSE + 1.0,
            start_tolerance=5.0, end_tolerance=5.0, max_sample_gap=10.0)

    def test_only_known_postactivation_states_are_ready(self):
        for status in OBSERVED_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(self._health(status)["readiness_status"], "ready")

    def test_unknown_malformed_and_warmup_states_are_not_ready(self):
        for status in UNOBSERVED_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(
                    self._health(status)["readiness_status"],
                    "post_activation_observation_missing")

    def test_known_states_can_pass_operational_acceptance_without_trades(self):
        for status in OBSERVED_STATUSES:
            with self.subTest(status=status):
                report = self._report(status)
                self.assertTrue(report["accepted"], report["reasons"])
                self.assertFalse(report["authorizing"])

    def test_unknown_malformed_and_warmup_states_fail_session_acceptance(self):
        for status in UNOBSERVED_STATUSES:
            with self.subTest(status=status):
                report = self._report(status)
                self.assertFalse(report["accepted"])
                self.assertIn(
                    "post_activation_observation_missing", report["reasons"])


if __name__ == "__main__":
    unittest.main()
