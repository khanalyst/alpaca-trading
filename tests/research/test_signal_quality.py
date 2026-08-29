"""Conditional forward-return diagnostics stay causal and non-authorizing."""

import unittest

from agent.contracts.rule import (
    evaluate_rule_signal, evaluate_rule_signal_trace,
)
from research.costs import diagnostic_backfill_policy
from research.edge_lab import _read_discovery_rows
from research.signal_quality import _forward_return, measure_signal_quality
from research.strategy_factory import _sanitize_fit_selection
from tests.research.test_factory_end_to_end import ROOT_SPEC, edge_corpus


class SignalTraceTests(unittest.TestCase):
    def setUp(self):
        _raw, self.bars, _snapshots, _quotes = _read_discovery_rows(edge_corpus(2))

    def test_insufficient_prefix_has_one_explicit_terminal_stage(self):
        trace = evaluate_rule_signal_trace(self.bars[:2], ROOT_SPEC)
        self.assertFalse(trace["authorizing"])
        self.assertTrue(trace["diagnostic_only"])
        self.assertIsNone(trace["signal"])
        self.assertEqual(trace["terminal_stage"], "minimum_prefix")
        self.assertEqual(trace["stages"][-1]["reason"], "insufficient_prefix")

    def test_trace_and_executable_evaluator_share_the_same_signal(self):
        grouped = [row for row in self.bars
                   if row.symbol == self.bars[0].symbol and
                   row.session_date == self.bars[0].session_date]
        for index in range(1, len(grouped) + 1):
            direct = evaluate_rule_signal(grouped[:index], ROOT_SPEC)
            trace = evaluate_rule_signal_trace(grouped[:index], ROOT_SPEC)
            self.assertEqual(trace["signal"], direct)
            if direct is not None:
                self.assertEqual(trace["terminal_stage"], "signal")
                self.assertTrue(all(stage["tested"] for stage in trace["stages"]))
                break
        else:
            self.fail("fixture did not emit its expected root signal")


class ConditionalForwardReturnTests(unittest.TestCase):
    def setUp(self):
        _raw, self.bars, _snapshots, _quotes = _read_discovery_rows(edge_corpus(2))
        self.policy = diagnostic_backfill_policy()

    def test_screen_is_compact_non_authorizing_and_deterministic(self):
        first = measure_signal_quality(
            self.bars, ROOT_SPEC, policy=self.policy, cost_hurdle_bps=17.0)
        reordered = measure_signal_quality(
            list(reversed(self.bars)), ROOT_SPEC, policy=self.policy,
            cost_hurdle_bps=17.0)
        self.assertEqual(first, reordered)
        self.assertEqual(first["schema"], "signal-quality.v1")
        self.assertFalse(first["authorizing"])
        self.assertTrue(first["diagnostic_only"])
        self.assertFalse(first["canonical_cross_sectional_ic"])
        self.assertEqual(first["horizons"], [5, 15, 30, 60, 120, 390])
        self.assertGreater(first["event_count"], 0)
        self.assertNotIn("events", first)
        five = first["horizon_metrics"]["5m"]
        self.assertEqual(five["cost_hurdle_bps"], 17.0)
        self.assertAlmostEqual(
            five["mean_after_hurdle_bps"],
            five["mean_forward_return_bps"] - 17.0)
        self.assertEqual(five["candidate_count"], five["matched_count"])

    def test_direction_sign_is_applied_before_aggregation(self):
        rows = [row for row in self.bars
                if row.symbol == self.bars[0].symbol and
                row.session_date == self.bars[0].session_date]
        long_value, long_reason = _forward_return(
            rows, entry_index=1, horizon=5, entry_price=rows[0].close,
            direction="long", allow_backfill=False)
        short_value, short_reason = _forward_return(
            rows, entry_index=1, horizon=5, entry_price=rows[0].close,
            direction="short", allow_backfill=False)
        self.assertIsNone(long_reason)
        self.assertIsNone(short_reason)
        self.assertAlmostEqual(long_value, -short_value)

    def test_a_future_gap_is_censored_instead_of_rolled_forward(self):
        baseline = measure_signal_quality(
            self.bars, ROOT_SPEC, policy=self.policy, cost_hurdle_bps=17.0)
        first_symbol = self.bars[0].symbol
        first_day = self.bars[0].session_date
        session = [row for row in self.bars
                   if row.symbol == first_symbol and row.session_date == first_day]
        signal_index = next(
            index for index in range(1, len(session) - 1)
            if evaluate_rule_signal(session[:index + 1], ROOT_SPEC) is not None)
        removed_at = session[signal_index + 2].timestamp
        gapped = [row for row in self.bars
                  if not (row.symbol == first_symbol and
                          row.session_date == first_day and
                          row.timestamp == removed_at)]
        result = measure_signal_quality(
            gapped, ROOT_SPEC, policy=self.policy, cost_hurdle_bps=17.0)
        self.assertLess(result["horizon_metrics"]["5m"]["candidate_count"],
                        baseline["horizon_metrics"]["5m"]["candidate_count"])
        self.assertGreater(
            result["horizon_metrics"]["5m"]["unavailable_reason_counts"]
                  .get("future_gap", 0), 0)

    def test_empty_screen_has_no_false_edge_claim(self):
        result = measure_signal_quality([], ROOT_SPEC, cost_hurdle_bps=17.0)
        self.assertEqual(result["event_count"], 0)
        self.assertIsNone(
            result["horizon_metrics"]["5m"]["mean_forward_return_bps"])
        self.assertNotIn("passes", result)

    def test_model_selection_sees_only_compact_quality_aggregates(self):
        quality = measure_signal_quality(
            self.bars, ROOT_SPEC, policy=self.policy, cost_hurdle_bps=17.0)
        quality["raw_rows"] = [{"close": 999.0}]
        projected = _sanitize_fit_selection(
            {"fit_diagnostics": {"schema": "fit-diagnostics.v1",
                                 "signal_quality": quality}},
            context="diagnostic")
        retained = projected["fit_diagnostics"]["signal_quality"]
        self.assertEqual(retained["event_count"], quality["event_count"])
        self.assertIn("5m", retained["horizon_metrics"])
        self.assertNotIn("raw_rows", retained)


if __name__ == "__main__":
    unittest.main()
