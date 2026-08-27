"""The paper-account trial lane, and the boundary it must not cross.

Backtests say what would have worked; only a live paper book says what does.
These tests pin three things: that a trial window is judged on a real sample
and not on noise, that a failed trial teaches the search something rather than
only changing a status, and that trial review remains independent from the
rolling paper warning.
"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from research.edge_lab import EdgeLedger
from research.factory_core import initial_hypotheses
from research.factory_ledger import FactoryLedger
from research.trial import (
    DEFAULT_MIN_SESSIONS, TRIAL_SCHEMA, promotable_report, review_trials,
    trial_policy,
)

POLICY = {"research": {"trial": {"enabled": True, "min_sessions": 5,
                                 "min_trades": 10, "min_mean_r": 0.0,
                                 "min_total_r": 0.0}},
          "strategy": {"execution_mode": "shares"}}


def _ledgers(directory):
    path = Path(directory) / "edge_lab.sqlite3"
    factory = FactoryLedger(path)
    hypothesis = initial_hypotheses(1)[0]
    factory.register(hypothesis)
    return EdgeLedger(path), factory, hypothesis


def _candidate(ledger, variant, *, status="validated", hypothesis_id=None,
               family="opening_range_breakout"):
    record = ledger.register_candidate(
        variant, strategy_id="rule", vehicle="equity", hypothesis="trial lane",
        config={"strategy": {"rule_spec": {"family": family}}},
        axes={"hypothesis_id": hypothesis_id} if hypothesis_id else {})
    with closing(sqlite3.connect(ledger.path)) as db, db:
        db.execute("UPDATE candidate_state SET status=? WHERE candidate_id=?",
                   (status, record["candidate_id"]))
    return record["candidate_id"]


def _outcomes(ledger, candidate_id, values, *, risk=100.0, prefix="op"):
    for index, value in enumerate(values):
        ledger.ingest_paper_outcome(candidate_id, {
            "opportunity_id": f"{prefix}{index}",
            "session_date": f"2026-05-{index % 28 + 1:02d}",
            "net_pnl": value * risk, "risk_usd": risk})


class TrialPolicyTests(unittest.TestCase):
    def test_defaults_require_a_real_sample(self):
        policy = trial_policy(None)
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["min_sessions"], DEFAULT_MIN_SESSIONS)
        self.assertGreaterEqual(policy["min_trades"], 20)

    def test_configuration_overrides_the_window_and_the_floor(self):
        policy = trial_policy(POLICY)
        self.assertEqual(policy["min_sessions"], 5)
        self.assertEqual(policy["min_trades"], 10)

    def test_a_disabled_lane_judges_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.a.1")
            # Losing, but gently enough that the separate rolling-R monitor
            # does not warn: this test is about the trial lane, not telemetry.
            _outcomes(ledger, candidate, [-0.05] * 30)
            result = review_trials(
                ledger.path,
                config={"research": {"trial": {"enabled": False}}})
            self.assertFalse(result["enabled"])
            self.assertEqual(result["reviews"], [])
            self.assertEqual(ledger.candidate(candidate)["status"], "validated")


class TrialVerdictTests(unittest.TestCase):
    def test_a_short_window_is_still_running_and_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.thin.1")
            _outcomes(ledger, candidate, [-1.0, -1.0, -1.0])
            result = review_trials(ledger.path, config=POLICY)
            verdict = result["reviews"][0]["verdict"]
            self.assertEqual(verdict["state"], "running")
            self.assertEqual(result["parked"], [])
            self.assertEqual(ledger.candidate(candidate)["status"], "validated")

    def test_a_positive_trial_becomes_promotable_and_keeps_trading(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.win.1")
            _outcomes(ledger, candidate, [1.0] * 6 + [-0.3] * 6)
            result = review_trials(ledger.path, config=POLICY)
            review = result["reviews"][0]
            self.assertEqual(review["verdict"]["state"], "passed")
            self.assertEqual(review["action"], "promotable")
            self.assertEqual(ledger.candidate(candidate)["status"], "validated")
            self.assertEqual(result["promotable"][0]["variant_id"], "rule.win.1")

    def test_a_negative_trial_is_parked(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.lose.1")
            _outcomes(ledger, candidate, [-0.4] * 12)
            result = review_trials(ledger.path, config=POLICY)
            self.assertEqual(result["schema"], TRIAL_SCHEMA)
            self.assertEqual(len(result["parked"]), 1)
            self.assertEqual(ledger.candidate(candidate)["status"], "demoted")

    def test_a_dry_run_reports_the_same_verdict_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.lose.1")
            _outcomes(ledger, candidate, [-0.4] * 12)
            result = review_trials(ledger.path, config=POLICY, apply=False)
            self.assertEqual(result["reviews"][0]["action"], "would_park")
            self.assertEqual(result["parked"], [])
            self.assertEqual(ledger.candidate(candidate)["status"], "validated")

    def test_outcomes_without_usable_r_are_inconclusive_not_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.blank.1")
            with closing(sqlite3.connect(ledger.path)) as db, db:
                for index in range(12):
                    db.execute(
                        """INSERT INTO paper_outcomes
                           (outcome_id,candidate_id,vehicle,opportunity_id,
                            session_date,net_pnl,outcome_json,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (f"o{index}", candidate, "equity", f"op{index}",
                         f"2026-05-{index + 1:02d}", 0.0, json.dumps({}), 0.0))
            verdict = review_trials(ledger.path, config=POLICY)["reviews"][0]["verdict"]
            self.assertEqual(verdict["state"], "inconclusive")


class PinnedEdgeTests(unittest.TestCase):
    """A pinned edge belongs to the operator, in this lane too."""

    def test_a_pinned_loser_is_reported_and_parked(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.pinned.1")
            _outcomes(ledger, candidate, [-0.4] * 12)
            result = review_trials(ledger.path, config=POLICY,
                                   pinned=[("rule.pinned.1", "equity")])
            review = result["reviews"][0]
            self.assertTrue(review["pinned"])
            self.assertEqual(review["action"], "parked")
            self.assertEqual(review["verdict"]["state"], "failed")
            self.assertEqual(len(result["parked"]), 1)
            self.assertEqual(ledger.candidate(candidate)["status"], "demoted")
            event = next(item for item in ledger.history(candidate)
                         if item["event_type"] == "safety_demotion")
            self.assertTrue(json.loads(event["payload_json"])["pinned"])

    def test_a_rolling_guard_breach_on_a_pinned_edge_is_advisory(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.pinned.2")
            for index in range(25):
                summary = ledger.ingest_paper_outcome(candidate, {
                    "opportunity_id": f"op{index}",
                    "session_date": f"2026-05-{index % 28 + 1:02d}",
                    "net_pnl": -100.0, "risk_usd": 100.0}, frozen=True)
            self.assertTrue(summary["frozen"])
            self.assertTrue(summary["rolling_guard_breached"])
            self.assertIsNone(summary["guard_breach"])
            self.assertEqual(ledger.candidate(candidate)["status"], "validated")
            demotions = [json.loads(event["payload_json"])
                         for event in ledger.history(candidate)
                         if event["event_type"] == "safety_demotion"]
            self.assertEqual(demotions, [])

    def test_the_same_rolling_breach_is_advisory_for_an_unpinned_edge(self):
        """Pinning does not change the rolling signal's authority."""
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.auto.1")
            for index in range(25):
                summary = ledger.ingest_paper_outcome(candidate, {
                    "opportunity_id": f"op{index}",
                    "session_date": f"2026-05-{index % 28 + 1:02d}",
                    "net_pnl": -100.0, "risk_usd": 100.0})
            self.assertTrue(summary["rolling_guard_breached"])
            self.assertIsNone(summary["guard_breach"])
            self.assertEqual(ledger.candidate(candidate)["status"], "validated")


class LiveLessonTests(unittest.TestCase):
    """A parked trial has to teach the search something."""

    def test_a_parked_trial_records_a_graded_live_lesson(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, factory, hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.lose.1",
                                   hypothesis_id=hypothesis.hypothesis_id)
            _outcomes(ledger, candidate, [-0.4] * 12)
            review_trials(ledger.path, config=POLICY)
            lessons = [item for item in factory.lessons(vehicle="equity")
                       if item["kind"] == "trial"]
            self.assertEqual(len(lessons), 1)
            lesson = lessons[0]
            self.assertEqual(lesson["source"], "live_paper")
            self.assertIn("Live paper trial", lesson["reason"])
            self.assertEqual(lesson["diagnosis"]["primary_failure"],
                             "live_paper_underperformance")
            # Graded immediately: the live book already gave its verdict.
            self.assertIsNotNone(lesson["outcome"])
            self.assertFalse(lesson["outcome"]["passed"])
            self.assertFalse(lesson["outcome"]["underpowered"])

    def test_the_live_lesson_reaches_the_next_tuning_brief(self):
        """The whole point of the lane: the book informs the next proposal."""
        from research.strategy_factory import _lesson_brief

        with tempfile.TemporaryDirectory() as directory:
            ledger, factory, hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.lose.1",
                                   hypothesis_id=hypothesis.hypothesis_id)
            _outcomes(ledger, candidate, [-0.4] * 12)
            review_trials(ledger.path, config=POLICY)
            brief = _lesson_brief(factory, vehicle="equity",
                                  family="opening_range_breakout")
            self.assertTrue(brief)
            self.assertTrue(any("Live paper trial" in item["reason"]
                                for item in brief))
            self.assertTrue(any(item["proposed_by"] == "live_paper"
                                for item in brief))

    def test_a_candidate_without_lineage_parks_without_a_lesson(self):
        """No hypothesis to attach to is not a reason to fail the review."""
        with tempfile.TemporaryDirectory() as directory:
            ledger, factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.orphan.1")
            _outcomes(ledger, candidate, [-0.4] * 12)
            result = review_trials(ledger.path, config=POLICY)
            self.assertEqual(len(result["parked"]), 1)
            self.assertEqual(ledger.candidate(candidate)["status"], "demoted")
            self.assertEqual(
                [item for item in factory.lessons(vehicle="equity")
                 if item["kind"] == "trial"], [])


class PromotableTests(unittest.TestCase):
    def test_it_hands_over_a_pasteable_config_block(self):
        from agent.config import DEFAULT_CONFIG, validate_config

        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            _outcomes(ledger, _candidate(ledger, "rule.win.1"),
                      [1.0] * 6 + [-0.3] * 6)
            rows = promotable_report(ledger.path, config=POLICY)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["variant_id"], "rule.win.1")
            self.assertEqual(row["family"], "opening_range_breakout")
            self.assertGreater(row["total_r"], 0)
            self.assertIsNotNone(row["return_pct"])
            # The snippet must be config the validator accepts, for this
            # exact variant. Retyping a content hash is how promotions go
            # to the wrong edge.
            snippet = json.loads(row["config_snippet"])
            merged = {**DEFAULT_CONFIG,
                      "strategy": {**DEFAULT_CONFIG["strategy"],
                                   **snippet["strategy"]}}
            config = validate_config(merged)
            self.assertEqual(config["strategy"]["pinned"][0]["variant_id"],
                             "rule.win.1")

    def test_an_already_pinned_edge_is_marked_and_offers_no_snippet(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            _outcomes(ledger, _candidate(ledger, "rule.win.1"),
                      [1.0] * 6 + [-0.3] * 6)
            rows = promotable_report(ledger.path, config=POLICY,
                                     pinned=[("rule.win.1", "equity")])
            self.assertTrue(rows[0]["already_pinned"])
            self.assertIsNone(rows[0]["config_snippet"])

    def test_a_losing_edge_is_never_promotable(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            _outcomes(ledger, _candidate(ledger, "rule.lose.1"), [-0.4] * 12)
            self.assertEqual(promotable_report(ledger.path, config=POLICY), [])

    def test_promotion_stays_a_human_action(self):
        """Nothing in this module may change a lifecycle toward deployment."""
        with tempfile.TemporaryDirectory() as directory:
            ledger, _factory, _hypothesis = _ledgers(directory)
            candidate = _candidate(ledger, "rule.win.1", status="backtest_passed")
            _outcomes(ledger, candidate, [1.0] * 12)
            review_trials(ledger.path, config=POLICY)
            promotable_report(ledger.path, config=POLICY)
            # Not deployed, so not a trial, so untouched in either direction.
            self.assertEqual(ledger.candidate(candidate)["status"],
                             "backtest_passed")


if __name__ == "__main__":
    unittest.main()
