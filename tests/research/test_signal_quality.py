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
from research.maturity import causal_maturity_bars
from research.signal_quality import (_ControlPolicy, _first_event,
                                     _forward_return, measure_signal_quality)
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

    def test_measurement_scope_is_explicit_and_validated(self):
        result = measure_signal_quality(
            self.bars, ROOT_SPEC, horizons=(5,),
            measurement_scope="previously_examined_retrospective_diagnostic")
        self.assertEqual(result["scope"],
                         "previously_examined_retrospective_diagnostic")
        self.assertEqual(result["timing_policy"]["measurement_scope"],
                         result["scope"])
        with self.assertRaises(ValueError):
            measure_signal_quality(self.bars, ROOT_SPEC,
                                   measurement_scope="validation")

    def test_session_clustered_inference_accompanies_every_control_delta(self):
        result = measure_signal_quality(
            self.bars, ROOT_SPEC, policy=self.policy, cost_hurdle_bps=17.0)
        for label, horizon in result["horizon_metrics"].items():
            with self.subTest(horizon=label):
                self.assertEqual(horizon["inference_cluster_unit"], "session")
                clusters = horizon["session_clusters"]
                if horizon["matched_count"] and clusters >= 2:
                    self.assertEqual(
                        horizon["candidate_minus_control_cluster_df"],
                        clusters - 1)
                    p_value = horizon[
                        "candidate_minus_control_cluster_sign_flip_p_value"]
                    self.assertGreater(p_value, 0.0)
                    self.assertLessEqual(p_value, 1.0)
                    self.assertIn("after_hurdle_cluster_t_stat", horizon)
                else:
                    self.assertIsNone(
                        horizon["candidate_minus_control_cluster_t_stat"])

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

    def test_normal_one_second_recorder_delay_keeps_causal_signal_outcome_and_control(self):
        delayed = [replace(
            row,
            identity=replace(row.identity,
                             observed_at=row.end + timedelta(seconds=1)),
        ) for row in self.bars]
        result = measure_signal_quality(
            delayed, ROOT_SPEC, policy=self.policy, horizons=(5,))
        metrics = result["horizon_metrics"]["5m"]
        self.assertGreater(result["event_count"], 0)
        self.assertGreater(metrics["candidate_count"], 0)
        self.assertGreater(metrics["matched_count"], 0)
        self.assertEqual(
            metrics["candidate_decision_delay_seconds"]["mean_seconds"], 1.0)
        self.assertEqual(
            metrics["candidate_outcome_observation_delay_seconds"]["mean_seconds"],
            1.0)
        self.assertEqual(
            metrics["control_decision_delay_seconds"]["mean_seconds"], 1.0)
        self.assertEqual(
            metrics["control_outcome_observation_delay_seconds"]["mean_seconds"],
            1.0)
        self.assertEqual(result["timing_policy"]["entry_semantics"],
                         "conditional_signal_close_anchor")

    def test_delayed_entry_selection_does_not_shift_wall_clock_horizon(self):
        rows = [row for row in self.bars
                if row.symbol == self.bars[0].symbol and
                row.session_date == self.bars[0].session_date]
        delayed = [replace(
            row,
            identity=replace(row.identity,
                             observed_at=row.end + timedelta(seconds=1)),
        ) for row in rows]
        baseline = measure_signal_quality(rows, ROOT_SPEC, horizons=(5,))
        result = measure_signal_quality(delayed, ROOT_SPEC, horizons=(5,))
        self.assertEqual(
            baseline["horizon_metrics"]["5m"]["mean_forward_return_bps"],
            result["horizon_metrics"]["5m"]["mean_forward_return_bps"])
        event, reason = _first_event(
            delayed, ROOT_SPEC, allow_backfill=False)
        self.assertIsNone(reason)
        self.assertGreater(event["entry_index"], event["signal_index"] + 1)

    def test_late_signal_cannot_reuse_outcome_that_closed_before_decision(self):
        rows = [row for row in self.bars
                if row.symbol == self.bars[0].symbol and
                row.session_date == self.bars[0].session_date]
        delayed = list(rows)
        signal_index = 15
        delayed[signal_index] = replace(
            delayed[signal_index],
            identity=replace(
                delayed[signal_index].identity,
                observed_at=delayed[signal_index].end + timedelta(minutes=10),
            ),
        )
        result = measure_signal_quality(delayed, ROOT_SPEC, horizons=(5,))
        metrics = result["horizon_metrics"]["5m"]
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(metrics["candidate_count"], 0)
        self.assertEqual(
            metrics["unavailable_reason_counts"],
            {"future_bar_unavailable": 1})

    def test_unresolved_revision_marker_is_refused_closed(self):
        class VersionedRow:
            def __init__(self, base):
                self._base = base
                self.version = "v2"

            def __getattr__(self, name):
                return getattr(self._base, name)

        revised = [VersionedRow(row) for row in self.bars]
        result = measure_signal_quality(revised, ROOT_SPEC, horizons=(5,))
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(
            result["event_rejection_counts"], {"revision_unresolved": 16})
        self.assertEqual(
            result["eligibility_provenance"]["reason_counts"].get(
                "revision_unresolved"), 16)


class ControlPrefixTests(unittest.TestCase):
    def test_short_corpus_is_insufficient_history_not_predicate_no_signal(self):
        rows = [{
            "timestamp": (self._opening() + timedelta(minutes=index)).isoformat(),
            "interval_seconds": 60,
            "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0,
            "symbol": "SPY",
        } for index in range(5)]
        event, reason = _first_event(rows, ROOT_SPEC, allow_backfill=False)
        self.assertIsNone(event)
        self.assertEqual(reason, "data_incomplete")
        prefix = _fit_prefixes(rows, ROOT_SPEC)
        self.assertEqual(prefix["prefix_status_counts"].get(
            "insufficient_history"), 3)
        self.assertEqual(prefix["eligibility_provenance"]["status"],
                         "data_incomplete")

    def test_opening_range_anchor_adds_signal_bar_to_maturity(self):
        spec = validate_rule_spec({**ROOT_SPEC,
                                   "family": "opening_range_breakout",
                                   "range_minutes": 30,
                                   "lookback": 3,
                                   "atr_period": 3,
                                   "slow_lookback": 5,
                                   "confirmation": "none",
                                   "confirmations": []})
        self.assertEqual(causal_maturity_bars(spec), 31)

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
        self.assertEqual(causal_maturity_bars(spec), 40)

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
