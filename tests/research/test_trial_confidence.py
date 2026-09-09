import tempfile
import unittest
from unittest.mock import patch

from research.trial import _verdict, _mean_r_pct, _capital_return_pct, review_trials
from .test_trial import POLICY, _ledgers, _candidate, _outcomes


class TrialConfidenceTests(unittest.TestCase):
    POLICY = {"min_sessions": 20, "min_trades": 20,
              "min_mean_r": 0, "min_total_r": 0}

    @staticmethod
    def _performance(*, total_r, mean_r, lower_bound, upper_bound,
                     sessions=20, trades=20):
        return {
            "sessions": sessions, "outcomes": trades,
            "total_r": total_r, "mean_r": mean_r,
            "session_cluster_confidence": {
                "available": True, "confidence": .95,
                "lower_bound": lower_bound, "upper_bound": upper_bound,
                "observations": trades, "clusters": sessions,
            },
        }

    def test_before_sample_floor_remains_running(self):
        performance = self._performance(
            total_r=-20, mean_r=-1, lower_bound=-1.1, upper_bound=-.9,
            sessions=19)
        self.assertEqual(_verdict(performance, self.POLICY)["state"], "running")

    def test_negative_point_estimate_with_spanning_interval_is_inconclusive(self):
        performance = self._performance(
            total_r=-.02, mean_r=-.001, lower_bound=-.2, upper_bound=.2)
        self.assertEqual(
            _verdict(performance, self.POLICY)["state"], "inconclusive")

    def test_uncertain_negative_trial_is_not_parked(self):
        confidence = {
            "available": True, "confidence": .95,
            "lower_bound": -.2, "upper_bound": .2,
            "observations": 20, "clusters": 20,
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger, _, _ = _ledgers(directory)
            candidate = _candidate(ledger, "test-uncertain-negative")
            _outcomes(ledger, candidate, [-.001] * 20)
            with patch("research.trial._session_cluster_confidence",
                       return_value=confidence):
                result = review_trials(ledger.path, config=POLICY, apply=True)
            self.assertEqual(ledger.candidate(candidate)["status"], "validated")
        self.assertEqual(result["reviews"][0]["verdict"]["state"],
                         "inconclusive")
        self.assertEqual(result["parked"], [])

    def test_negative_trial_requires_an_upper_bound_below_the_floor(self):
        performance = self._performance(
            total_r=-2, mean_r=-.1, lower_bound=-.2, upper_bound=-.01)
        self.assertEqual(_verdict(performance, self.POLICY)["state"], "failed")
        performance["total_r"] = .01
        self.assertEqual(
            _verdict(performance, self.POLICY)["state"], "inconclusive")

    def test_positive_point_estimate_without_positive_bound_is_inconclusive(self):
        performance = self._performance(
            total_r=1, mean_r=.01, lower_bound=-.1, upper_bound=.1)
        confidence = performance["session_cluster_confidence"]
        performance_without_confidence = dict(performance)
        performance_without_confidence.pop("session_cluster_confidence")
        self.assertEqual(
            _verdict(performance_without_confidence, self.POLICY)["state"],
            "inconclusive")
        self.assertEqual(
            _verdict(performance, self.POLICY)["state"], "inconclusive")
        confidence["lower_bound"] = .001
        self.assertEqual(_verdict(performance, self.POLICY)["state"], "passed")
        confidence["clusters"] = 2
        self.assertEqual(
            _verdict(performance, self.POLICY)["state"], "inconclusive")

    def test_malformed_confidence_or_nonfinite_r_is_inconclusive(self):
        malformed = (
            {"upper_bound": None},
            {"lower_bound": float("nan")},
            {"upper_bound": float("inf")},
            {"lower_bound": .2, "upper_bound": -.2},
            {"confidence": .94},
            {"available": False},
        )
        for update in malformed:
            with self.subTest(update=update):
                performance = self._performance(
                    total_r=-2, mean_r=-.1, lower_bound=-.2,
                    upper_bound=-.01)
                performance["session_cluster_confidence"].update(update)
                self.assertEqual(
                    _verdict(performance, self.POLICY)["state"],
                    "inconclusive")
        for field, value in (("mean_r", float("nan")),
                             ("total_r", float("inf"))):
            with self.subTest(field=field):
                performance = self._performance(
                    total_r=-2, mean_r=-.1, lower_bound=-.2,
                    upper_bound=-.01)
                performance[field] = value
                self.assertEqual(
                    _verdict(performance, self.POLICY)["state"],
                    "inconclusive")

    def test_real_ledger_trial_derives_deterministic_session_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _, _ = _ledgers(directory)
            candidate = _candidate(ledger, "test-confidence")
            _outcomes(ledger, candidate, [1] * 10)
            first = review_trials(ledger.path, config=POLICY, apply=False)
            second = review_trials(ledger.path, config=POLICY, apply=False)
        verdict = first["reviews"][0]["verdict"]
        self.assertEqual(verdict["state"], "passed")
        self.assertEqual(verdict["session_cluster_confidence"]["lower_bound"], 1)
        self.assertEqual(verdict, second["reviews"][0]["verdict"])

    def test_invalid_counts_neither_crash_nor_conclude(self):
        for field in ("sessions", "outcomes"):
            for value in (float("nan"), float("inf"), -1, 20.5, "20", True, None):
                with self.subTest(field=field, value=value):
                    performance = self._performance(
                        total_r=2, mean_r=.1, lower_bound=.05, upper_bound=.2)
                    performance[field] = value
                    self.assertEqual(_verdict(performance, self.POLICY)["state"], "inconclusive")

    def test_string_or_fractional_confidence_metadata_cannot_conclude(self):
        updates = (
            {"confidence": "0.95"}, {"lower_bound": "0.05"},
            {"upper_bound": "0.2"}, {"observations": "20"},
            {"clusters": "20"}, {"observations": 20.5},
            {"clusters": 20.5}, {"observations": True},
            {"session_clusters": 20.5},
        )
        for update in updates:
            with self.subTest(update=update):
                performance = self._performance(
                    total_r=2, mean_r=.1, lower_bound=.05, upper_bound=.2)
                performance["session_cluster_confidence"].update(update)
                self.assertEqual(_verdict(performance, self.POLICY)["state"], "inconclusive")

    def test_mean_r_percentage_is_not_capital_return(self):
        self.assertEqual(_mean_r_pct({"mean_r": .1}), 10)
        self.assertIsNone(_capital_return_pct({"net_pnl": 100, "mean_r": .1}))
        self.assertEqual(_capital_return_pct({"net_pnl": 100, "starting_equity": 10000}), 1)

    def test_positive_but_regime_clustered_returns_do_not_earn_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _, _ = _ledgers(directory)
            candidate = _candidate(ledger, "test-positive-point-only")
            _outcomes(ledger, candidate, [1.0] * 6 + [-.3] * 6)
            result = review_trials(ledger.path, config=POLICY, apply=True)
            self.assertEqual(ledger.candidate(candidate)["status"], "validated")
        self.assertEqual(result["reviews"][0]["verdict"]["state"], "inconclusive")
        self.assertEqual(result["promotable"], [])
        self.assertEqual(result["parked"], [])
