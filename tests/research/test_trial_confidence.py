import tempfile
import unittest

from research.trial import _verdict, _mean_r_pct, _capital_return_pct, review_trials
from .test_trial import POLICY, _ledgers, _candidate, _outcomes


class TrialConfidenceTests(unittest.TestCase):
    def test_positive_point_estimate_without_positive_bound_is_inconclusive(self):
        policy = {"min_sessions": 30, "min_trades": 100, "min_mean_r": 0, "min_total_r": 0}
        performance = {"sessions": 30, "outcomes": 100, "total_r": 1, "mean_r": .01}
        self.assertEqual(_verdict(performance, policy)["state"], "inconclusive")
        confidence = {"available": True, "confidence": .95, "lower_bound": -.1,
                      "observations": 100, "clusters": 30}
        performance["session_cluster_confidence"] = confidence
        self.assertEqual(_verdict(performance, policy)["state"], "inconclusive")
        confidence["lower_bound"] = .001
        self.assertEqual(_verdict(performance, policy)["state"], "passed")
        confidence["clusters"] = 2
        self.assertEqual(_verdict(performance, policy)["state"], "inconclusive")

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
