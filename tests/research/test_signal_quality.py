"""Conditional forward-return diagnostics stay causal and non-authorizing."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from agent.contracts.rule import (
    evaluate_rule_signal, evaluate_rule_signal_trace, validate_rule_spec,
)
from research.costs import diagnostic_backfill_policy
from research.edge_lab import _read_discovery_rows
from research.fit_diagnostics import _fit_prefixes
from research.signal_quality import (_ControlPolicy, _forward_return,
                                     measure_signal_quality)
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
        self.assertEqual(first["schema"], "signal-quality.v2")
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

    def test_fit_prefix_events_reuse_exactly_without_rescanning(self):
        prefix = _fit_prefixes(self.bars, ROOT_SPEC, policy=self.policy)
        scanned = measure_signal_quality(
            self.bars, ROOT_SPEC, policy=self.policy, cost_hurdle_bps=17.0)
        with patch("research.signal_quality._first_event",
                   side_effect=AssertionError("unexpected prefix scan")):
            reused = measure_signal_quality(
                self.bars, ROOT_SPEC, policy=self.policy,
                cost_hurdle_bps=17.0,
                precomputed_first_signals=prefix["first_signals"])
        self.assertEqual(scanned, reused)
        self.assertEqual(scanned["event_digest"], reused["event_digest"])

    def test_precomputed_no_signal_sessions_keep_rejection_counts(self):
        no_signal_spec = dict(ROOT_SPEC, threshold_bps=500.0)
        prefix = _fit_prefixes(
            self.bars, no_signal_spec, policy=self.policy)
        scanned = measure_signal_quality(
            self.bars, no_signal_spec, policy=self.policy)
        with patch("research.signal_quality._first_event",
                   side_effect=AssertionError("unexpected prefix scan")):
            reused = measure_signal_quality(
                self.bars, no_signal_spec, policy=self.policy,
                precomputed_first_signals=prefix["first_signals"])
        self.assertEqual(scanned, reused)
        self.assertEqual(scanned["event_rejection_counts"],
                         {"no_actionable_signal": 16})

    def test_malformed_precomputed_event_is_rejected_closed(self):
        prefix = _fit_prefixes(self.bars, ROOT_SPEC, policy=self.policy)
        malformed = dict(prefix["first_signals"][0])
        malformed["entry_index"] = len(self.bars) + 1
        result = measure_signal_quality(
            self.bars, ROOT_SPEC, policy=self.policy,
            precomputed_first_signals=[malformed])
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(
            result["event_rejection_counts"],
            {"precomputed_event_invalid": 1,
             "no_actionable_signal": len(prefix["first_signals"]) - 1},
        )

    def test_empty_precomputed_backfill_prefix_is_data_ineligible(self):
        historical = [replace(
            row,
            identity=replace(
                row.identity,
                as_of=row.timestamp,
                observed_at=row.timestamp + timedelta(days=30),
                source_mode="historical_backfill",
            ),
        ) for row in self.bars]
        prefix = _fit_prefixes(historical, ROOT_SPEC)
        self.assertFalse(prefix["first_signals"])
        self.assertEqual(prefix["eligibility_provenance"]["status"],
                         "data_ineligible")
        quality = measure_signal_quality(
            historical, ROOT_SPEC,
            precomputed_first_signals=prefix["first_signals"],
            eligibility_provenance=prefix["eligibility_provenance"])
        self.assertEqual(quality["event_count"], 0)
        self.assertEqual(quality["event_rejection_counts"],
                         {"data_ineligible": 16})
        self.assertEqual(quality["eligibility_provenance"]["status"],
                         "data_ineligible")
        self.assertNotEqual(
            quality["event_rejection_counts"], {"no_actionable_signal": 16})


class ControlPrefixTests(unittest.TestCase):
    def test_inactive_slow_lookback_does_not_delay_a_control(self):
        spec = validate_rule_spec({**ROOT_SPEC,
                                   "family": "momentum_continuation",
                                   "confirmation": "none",
                                   "confirmations": [],
                                   "lookback": 5,
                                   "slow_lookback": 40,
                                   "atr_period": 14})
        policy = _ControlPolicy(spec)
        # Momentum reads lookback/ATR (and its own trailing feature window),
        # but not the normalized slow_lookback field.
        self.assertEqual(policy.minimum_prefix, 15)

    def test_active_trend_confirmation_requires_slow_lookback(self):
        spec = validate_rule_spec({**ROOT_SPEC,
                                   "family": "opening_range_breakout",
                                   "confirmation": "trend",
                                   "confirmations": [],
                                   "slow_lookback": 40,
                                   "atr_period": 14})
        policy = _ControlPolicy(spec)
        self.assertEqual(policy.minimum_prefix, 40)

    def test_underpowered_corpus_does_not_fall_back_to_shorter_prefix(self):
        spec = validate_rule_spec({**ROOT_SPEC,
                                   "family": "opening_range_breakout",
                                   "confirmation": "trend",
                                   "confirmations": [],
                                   "slow_lookback": 40})
        policy = _ControlPolicy(spec)
        rows = [{"timestamp": (self._opening() + timedelta(minutes=index)).isoformat()}
                for index in range(20)]
        self.assertIsNone(policy.admissible(rows, len(rows) - 1))

    @staticmethod
    def _opening():
        return datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main()
