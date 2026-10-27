"""The v2 paper-trial verdict: decided once, by a session test, and persisted.

Review finding D1-07: the v1 verdict re-tests a small-sample bootstrap bound
at every review and dashboard render, so a zero-edge candidate can read
"passed" on a lucky day and lose it later.  v2 is selected explicitly in the
configuration; v1 remains the default so existing verdicts reproduce.
"""

from datetime import date, timedelta
import random
import tempfile
import unittest

from research.trial import (TRIAL_VERDICT_EVENT, _verdict_v2, promotable_report,
                            review_trials, trial_policy)
from tests.research.test_trial import _candidate, _ledgers

V2 = {"research": {"trial": {"enabled": True, "verdict_version": "v2",
                             "min_sessions": 5, "min_trades": 10,
                             "min_mean_r": 0.0, "min_total_r": 0.0}},
      "strategy": {"execution_mode": "shares"}}
V1 = {"research": {"trial": {key: value for key, value in V2["research"]["trial"].items()
                             if key != "verdict_version"}},
      "strategy": V2["strategy"]}


def _ingest(ledger, candidate_id, by_session, *, prefix="op"):
    """Ingest R values per session, two or more trades per session."""
    counter = 0
    for session, values in by_session:
        for value in values:
            ledger.ingest_paper_outcome(candidate_id, {
                "opportunity_id": f"{prefix}{counter}", "session_date": session,
                "net_pnl": value * 100.0, "risk_usd": 100.0})
            counter += 1


def _days(count, first=date(2026, 5, 1)):
    return [(first + timedelta(days=offset)).isoformat() for offset in range(count)]


class DecideOnceTests(unittest.TestCase):
    def test_v2_is_explicit_and_v1_stays_the_default(self):
        self.assertNotIn("verdict_version", trial_policy(V1))
        self.assertEqual(trial_policy(V2)["verdict_version"], "v2")
        self.assertEqual(trial_policy(V2)["alpha_one_sided"], 0.05)
        with self.assertRaises(ValueError):
            trial_policy({"research": {"trial": {"verdict_version": "v3"}}})

    def test_the_verdict_is_decided_on_the_first_qualifying_window_only(self):
        days = _days(10)
        winners = [(day, [1.0 + 0.1 * index, 0.8])
                   for index, day in enumerate(days[:5])]
        losers = [(day, [-1.0, -1.0]) for day in days[5:]]
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.v2.window")
            _ingest(ledger, candidate, winners)
            first = review_trials(ledger.path, config=V2, apply=False)
            _ingest(ledger, candidate, losers, prefix="later")
            later = review_trials(ledger.path, config=V2, apply=False)
            v1_later = review_trials(ledger.path, config=V1, apply=False)
        verdict = first["reviews"][0]["verdict"]
        self.assertEqual(verdict["state"], "passed")
        self.assertEqual(verdict["decision_window_last"], days[4])
        self.assertTrue(verdict["session_cluster_inference"]["sign_flip_exact"])
        # Later sessions cannot change a decided window...
        self.assertEqual(later["reviews"][0]["verdict"]["state"], "passed")
        self.assertEqual(later["reviews"][0]["verdict"]["sessions"], 5)
        # ...whereas the v1 verdict is re-decided on everything at each review.
        self.assertNotEqual(v1_later["reviews"][0]["verdict"]["state"], "passed")

    def test_before_the_window_fills_the_trial_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.v2.running")
            _ingest(ledger, candidate, [(day, [1.0]) for day in _days(9)])
            result = review_trials(ledger.path, config=V2, apply=True)
        verdict = result["reviews"][0]["verdict"]
        self.assertEqual(verdict["state"], "running")
        self.assertEqual(result["promotable"], [])

    def test_the_first_terminal_verdict_is_persisted_and_returned(self):
        days = _days(5)
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.v2.persist")
            _ingest(ledger, candidate, [(day, [1.0 + 0.1 * index, 0.9])
                                         for index, day in enumerate(days)])
            first = review_trials(ledger.path, config=V2, apply=True)
            events = [event for event in ledger.history(candidate)
                      if event["event_type"] == TRIAL_VERDICT_EVENT]
            # A late-arriving outcome dated inside the decided window would
            # change a recomputation; the persisted verdict stands.
            _ingest(ledger, candidate, [(days[0], [-5.0, -5.0, -5.0])],
                    prefix="late")
            again = review_trials(ledger.path, config=V2, apply=True)
            promotable = promotable_report(ledger.path, config=V2)
            events_after = [event for event in ledger.history(candidate)
                            if event["event_type"] == TRIAL_VERDICT_EVENT]
        self.assertEqual(first["reviews"][0]["verdict"]["state"], "passed")
        self.assertEqual(len(events), 1)
        verdict = again["reviews"][0]["verdict"]
        self.assertEqual(verdict["state"], "passed")
        self.assertTrue(verdict["persisted"])
        self.assertEqual(len(events_after), 1)
        self.assertEqual([row["candidate_id"] for row in promotable], [candidate])

    def test_a_failed_v2_trial_is_parked(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.v2.fail")
            _ingest(ledger, candidate, [(day, [-1.0 - 0.1 * index, -0.9])
                                         for index, day in enumerate(_days(5))])
            result = review_trials(ledger.path, config=V2, apply=True)
            status = ledger.candidate(candidate)["status"]
        self.assertEqual(result["reviews"][0]["verdict"]["state"], "failed")
        self.assertEqual(status, "demoted")
        self.assertEqual(len(result["parked"]), 1)


class ZeroEdgeErrorRateTests(unittest.TestCase):
    def test_a_zero_edge_candidate_rarely_passes_its_single_decision(self):
        # The reviewer's null: R = +2 with probability 1/3, -1 otherwise
        # (mean zero, skewed), one to three trades per session.  Twelve
        # sessions keep the sign flip exact so the check stays fast.
        policy = trial_policy({"research": {"trial": {
            "verdict_version": "v2", "min_sessions": 12, "min_trades": 12}}})
        rng = random.Random(20261027)
        passed = 0
        repetitions = 300
        for _ in range(repetitions):
            values, clusters = [], []
            for session in _days(12):
                for _trade in range(rng.randint(1, 3)):
                    values.append(2.0 if rng.random() < 1 / 3 else -1.0)
                    clusters.append(session)
            passed += _verdict_v2(values, clusters, policy)["state"] == "passed"
        self.assertLessEqual(passed / repetitions, 0.08)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
