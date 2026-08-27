"""Live paper results per edge, readable rather than only actionable.

The ledger consumes paper outcomes for held-out drift safety and exposes the
overlapping fixed-window rolling-R signal as advisory telemetry.  These tests
pin the read surface to the same numbers the surveillance uses.
"""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research.edge_ledger import (
    PAPER_DEMOTION_MIN_OUTCOMES, PAPER_DEMOTION_R_FLOOR,
    PAPER_DRIFT_MIN_OUTCOMES,
)
from research.edge_lab import EdgeLedger
from research.trial import review_trials
from . import test_strategy_factory as strategy_factory_tests
from .test_strategy_factory import persist_rule_gate


def _ledger(directory):
    return EdgeLedger(Path(directory) / "edge_lab.sqlite3")


def _candidate(ledger, variant="rule.mean-reversion.aaaa", status=None):
    record = ledger.register_candidate(
        variant, strategy_id="rule", vehicle="equity",
        hypothesis="paper visibility",
        config={"strategy": {"rule_spec": {"family": "mean_reversion"}}})
    if status:
        with closing(sqlite3.connect(ledger.path)) as db, db:
            db.execute("UPDATE candidate_state SET status=? WHERE candidate_id=?",
                       (status, record["candidate_id"]))
    return record["candidate_id"]


def _outcomes(ledger, candidate_id, r_values, *, risk=100.0, prefix="op"):
    for index, value in enumerate(r_values):
        ledger.ingest_paper_outcome(candidate_id, {
            "opportunity_id": f"{prefix}{index}",
            "session_date": f"2026-05-{index % 28 + 1:02d}",
            "net_pnl": value * risk, "risk_usd": risk})


def _persist_shadow_proof(ledger, candidate_id):
    """Seed a strict shadow proof while retaining the compact fixture rows.

    The shared fixture helper uses an optional empty fit floor for shadow runs,
    but the immutable shadow envelope still has to carry the full 150-trade
    protocol minimum.  Recompute that report locally; no ledger rows or
    lifecycle checks are bypassed.
    """
    original_floor = strategy_factory_tests.structural_floor

    def strict_shadow_floor(rows, **kwargs):
        rows = list(rows)
        if not rows and kwargs.get("min_trades", 0) < 150:
            kwargs = {**kwargs, "min_trades": 150}
        return original_floor(rows, **kwargs)

    with patch.object(strategy_factory_tests, "structural_floor",
                      side_effect=strict_shadow_floor):
        persist_rule_gate(ledger, candidate_id, "shadow")


class PaperPerformanceTests(unittest.TestCase):
    def test_an_edge_with_no_outcomes_reports_empty_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            report = ledger.paper_performance(_candidate(ledger))
            self.assertEqual(report["outcomes"], 0)
            self.assertIsNone(report["total_r"])
            self.assertIsNone(report["mean_r"])
            self.assertIsNone(report["win_rate"])
            self.assertFalse(report["rolling"]["armed"])
            self.assertFalse(report["rolling"]["authoritative"])
            self.assertEqual(report["rolling"]["action"], "warning_only")
            self.assertFalse(report["drift"]["applicable"])

    def test_unknown_candidate_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KeyError):
                _ledger(directory).paper_performance("nope")

    def test_summary_matches_the_recorded_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            candidate = _candidate(ledger)
            _outcomes(ledger, candidate, [1.0, -0.5, 2.0, -1.0])
            report = ledger.paper_performance(candidate)
            self.assertEqual(report["outcomes"], 4)
            self.assertAlmostEqual(report["total_r"], 1.5)
            self.assertAlmostEqual(report["mean_r"], 0.375)
            self.assertAlmostEqual(report["win_rate"], 0.5)
            self.assertAlmostEqual(report["net_pnl"], 150.0)
            self.assertEqual(report["first_session"], "2026-05-01")
            self.assertEqual(report["last_session"], "2026-05-04")

    def test_rolling_guard_mirrors_the_demotion_thresholds(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            candidate = _candidate(ledger)
            _outcomes(ledger, candidate,
                      [-0.5] * (PAPER_DEMOTION_MIN_OUTCOMES - 1))
            report = ledger.paper_performance(candidate)
            self.assertFalse(report["rolling"]["armed"])
            self.assertEqual(report["rolling"]["required"],
                             PAPER_DEMOTION_MIN_OUTCOMES)
            self.assertEqual(report["rolling"]["floor"], PAPER_DEMOTION_R_FLOOR)

            _outcomes(ledger, candidate, [-0.5], prefix="tail")
            report = ledger.paper_performance(candidate)
            self.assertTrue(report["rolling"]["armed"])
            self.assertTrue(report["rolling"]["breached"])
            self.assertFalse(report["rolling"]["authoritative"])
            self.assertEqual(report["rolling"]["action"], "warning_only")

    def test_a_deployed_edge_that_breaches_is_reported_as_warning_only(self):
        """The report preserves the threshold without claiming authority."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            candidate = _candidate(ledger, status="validated")
            _outcomes(ledger, candidate, [-0.5] * PAPER_DEMOTION_MIN_OUTCOMES)
            report = ledger.paper_performance(candidate)
            self.assertTrue(report["rolling"]["breached"])
            self.assertFalse(report["rolling"]["authoritative"])
            self.assertEqual(report["rolling"]["action"], "warning_only")
            self.assertEqual(report["status"], "validated")
            self.assertTrue(report["deployed"])

    def test_drift_reports_its_own_prerequisites(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            candidate = _candidate(ledger)
            _outcomes(ledger, candidate, [0.4] * 5)
            drift = ledger.paper_performance(candidate)["drift"]
            # No re-verified shadow proof exists, so surveillance is simply not
            # applicable; it must say so rather than imply a healthy edge.
            self.assertFalse(drift["applicable"])
            self.assertFalse(drift["degraded"])
            self.assertEqual(drift["required"], PAPER_DRIFT_MIN_OUTCOMES)
            self.assertIsNone(drift["reference_mean_r"])

    def test_duplicate_outcomes_are_not_counted_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            candidate = _candidate(ledger)
            for _ in range(3):
                ledger.ingest_paper_outcome(candidate, {
                    "opportunity_id": "same", "session_date": "2026-05-01",
                    "net_pnl": 100.0, "risk_usd": 100.0})
            self.assertEqual(ledger.paper_performance(candidate)["outcomes"], 1)

    def test_retrial_uses_only_the_new_shadow_proof_epoch(self):
        policy = {"research": {"trial": {"enabled": True,
                                            "min_sessions": 5,
                                            "min_trades": 10,
                                            "min_mean_r": 0.0,
                                            "min_total_r": 0.0}}}
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            candidate = _candidate(ledger, status=None)
            persist_rule_gate(ledger, candidate, "backtest")
            ledger.transition(candidate, "backtest_passed", reason="backtest proof")
            _persist_shadow_proof(ledger, candidate)
            ledger.transition(candidate, "shadow", reason="shadow proof started")
            ledger.transition(candidate, "validated", reason="shadow proof passed")
            _outcomes(ledger, candidate, [-1.0] * 12, prefix="old")
            review = review_trials(ledger.path, config=policy)
            self.assertEqual(review["parked"][0]["candidate_id"], candidate)

            # Re-proof the same immutable candidate identity.  The new trial
            # must not inherit the old losses from the demoted epoch.
            _persist_shadow_proof(ledger, candidate)
            ledger.transition(candidate, "shadow", reason="new shadow proof")
            ledger.transition(candidate, "validated", reason="new shadow proof passed")
            report = ledger.paper_performance(candidate)
            self.assertEqual(report["outcomes"], 0)
            # Reuse the same opportunity id to exercise the legacy unique-key
            # migration path; storage keys are epoch-qualified internally.
            _outcomes(ledger, candidate, [1.0], prefix="old")
            self.assertEqual(ledger.paper_performance(candidate)["outcomes"], 1)


class PaperReportTests(unittest.TestCase):
    def test_report_ranks_by_realized_r_and_sinks_unobserved_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            strong = _candidate(ledger, "rule.mean-reversion.strong")
            weak = _candidate(ledger, "rule.trend-pullback.weak")
            _candidate(ledger, "rule.volume-breakout.silent")
            _outcomes(ledger, strong, [1.0, 1.0, 1.0])
            _outcomes(ledger, weak, [-1.0, 0.2])
            report = ledger.paper_report(vehicle="equity")
            self.assertEqual([item["variant_id"] for item in report],
                             ["rule.mean-reversion.strong",
                              "rule.trend-pullback.weak",
                              "rule.volume-breakout.silent"])

    def test_deployed_only_filters_to_the_edges_actually_trading(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            _candidate(ledger, "rule.mean-reversion.live", status="validated")
            _candidate(ledger, "rule.trend-pullback.bench")
            report = ledger.paper_report(vehicle="equity", deployed_only=True)
            self.assertEqual([item["variant_id"] for item in report],
                             ["rule.mean-reversion.live"])

    def test_report_is_vehicle_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            _candidate(ledger, "rule.mean-reversion.eq")
            ledger.register_candidate(
                "rule.mean-reversion.opt", strategy_id="rule", vehicle="option",
                hypothesis="h", config={"strategy": {"rule_spec": {
                    "family": "mean_reversion"}}})
            self.assertEqual(len(ledger.paper_report(vehicle="equity")), 1)
            self.assertEqual(len(ledger.paper_report(vehicle="option")), 1)
            self.assertEqual(len(ledger.paper_report()), 2)


class PaperCommandTests(unittest.TestCase):
    """``research.py edge paper`` is the operator's read path."""

    def _run(self, *arguments, db):
        import subprocess
        import sys
        return subprocess.run(
            [sys.executable, "research.py", "edge", "paper", *arguments,
             "--db", str(db)],
            cwd=Path(__file__).resolve().parents[2], check=False,
            capture_output=True, text=True)

    def test_command_prints_every_edge_and_can_filter_to_the_deployed_ones(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(directory)
            live = _candidate(ledger, "rule.mean-reversion.live", status="validated")
            _candidate(ledger, "rule.trend-pullback.bench")
            _outcomes(ledger, live, [1.0, -0.25])

            result = self._run(db=ledger.path)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(len(report), 2)
            self.assertAlmostEqual(report[0]["total_r"], 0.75)

            deployed = json.loads(
                self._run("--deployed", db=ledger.path).stdout)
            self.assertEqual([row["variant_id"] for row in deployed],
                             ["rule.mean-reversion.live"])

            single = json.loads(self._run(live, db=ledger.path).stdout)
            self.assertEqual(len(single), 1)
            self.assertEqual(single[0]["candidate_id"], live)


if __name__ == "__main__":
    unittest.main()
