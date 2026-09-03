"""Fit-only diagnostic invariants and exact execution-floor measurements."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.rule import evaluate_rule_signal_metadata
from research.edge_lab import _read_discovery_rows
from research.costs import CostModel, diagnostic_backfill_policy
from research.fit_diagnostics import (
    BAR_COVERAGE_SCHEMA, FIT_BEHAVIOR_ALIAS_DECIMALS, FIT_BEHAVIOR_ALIAS_SCHEMA,
    _fit_prefixes, _planned_vector, bar_coverage_telemetry,
    collapse_behavior_aliases,
    measure_fit_diagnostics,
)
from research.strategy_factory import initial_hypotheses
from tests.research.test_factory_end_to_end import ROOT_SPEC, edge_corpus


class FitDiagnosticsTests(unittest.TestCase):
    def _fit(self):
        _raw, bars, snapshots, quotes = _read_discovery_rows(edge_corpus(2))
        return bars, snapshots, quotes

    def test_atr_and_30_bps_floor_are_measured_from_the_signal_prefix(self):
        bars, _snapshots, _quotes = self._fit()
        diagnostic = measure_fit_diagnostics(bars, ROOT_SPEC)
        self.assertGreater(diagnostic["first_signal"]["signals"], 0)
        self.assertEqual(diagnostic["floor_30bps"]["bps"], 30.0)
        # The fixture's ATR is below 30 bps, so every executable planned stop
        # is floor-bound and the floor rate is exact rather than inferred from
        # account outcomes.
        self.assertEqual(diagnostic["floor_30bps"]["binding"],
                         diagnostic["first_signal"]["signals"])
        self.assertEqual(diagnostic["floor_30bps"]["rate"], 1.0)
        self.assertGreater(diagnostic["atr_bps"]["median"], 0.0)
        self.assertLess(diagnostic["atr_bps"]["median"], 30.0)

        session = next(row for row in bars if row.symbol == "AAA")
        # Use the first complete fixture session to pin the metadata helper's
        # non-authorizing floor decision as well.
        session_rows = [row for row in bars if row.symbol == session.symbol and
                        row.session_date == session.session_date]
        metadata = None
        for index in range(1, len(session_rows) + 1):
            metadata = evaluate_rule_signal_metadata(session_rows[:index], ROOT_SPEC)
            if metadata is not None:
                break
        self.assertIsNotNone(metadata)
        self.assertTrue(metadata["floor_binding"])
        self.assertAlmostEqual(metadata["planned_stop_distance"],
                               metadata["floor_distance"])

    def test_heldout_changes_do_not_enter_a_fit_measurement(self):
        bars, snapshots, quotes = self._fit()
        fit_bars = [row for row in bars if row.session_date == bars[0].session_date]
        baseline = measure_fit_diagnostics(fit_bars, ROOT_SPEC)
        altered = [
            replace(row, open=row.open * 4.0, close=row.close * 4.0,
                    high=row.high * 4.0, low=row.low * 4.0)
            if row.session_date != fit_bars[0].session_date else row
            for row in bars]
        self.assertNotEqual(altered, bars)
        # The caller passes only the sealed fit slice.  Altering later rows in
        # the corpus therefore cannot change a fit-only fingerprint/summary.
        self.assertEqual(
            baseline["behavior_fingerprint"],
            measure_fit_diagnostics(
                [row for row in altered
                 if row.session_date == fit_bars[0].session_date],
                ROOT_SPEC)["behavior_fingerprint"])

    def test_unavailable_feature_prefix_is_not_counted_as_executable(self):
        bars, _snapshots, _quotes = self._fit()
        altered = [
            replace(row, identity=replace(
                row.identity, observed_at=row.end + timedelta(days=1)))
            for row in bars]
        baseline = measure_fit_diagnostics(bars, ROOT_SPEC)
        diagnostic = measure_fit_diagnostics(altered, ROOT_SPEC)
        self.assertGreater(baseline["first_signal"]["signals"], 0)
        self.assertEqual(diagnostic["first_signal"]["signals"], 0)
        self.assertEqual(diagnostic["eligible_prefix"]["eligible"], 0)

    def test_explicit_backfill_diagnostics_recover_prefix_without_authorizing(self):
        bars, _snapshots, _quotes = self._fit()
        historical = [replace(
            row,
            identity=replace(
                row.identity,
                as_of=row.timestamp,
                observed_at=row.timestamp + timedelta(days=30),
                source_mode="historical_backfill",
            ),
        ) for row in bars]
        refused = measure_fit_diagnostics(historical, ROOT_SPEC)
        self.assertEqual(refused["first_signal"]["signals"], 0)
        policy = diagnostic_backfill_policy()
        diagnostic = measure_fit_diagnostics(
            historical, ROOT_SPEC, policy=policy)
        self.assertGreater(diagnostic["first_signal"]["signals"], 0)
        self.assertFalse(diagnostic["authorizing"])
        self.assertTrue(diagnostic["diagnostic_only"])
        self.assertTrue(diagnostic["historical_backfill"]["included"])
        self.assertFalse(diagnostic["historical_backfill"]["authorizing"])
        self.assertEqual(
            historical[0].identity.observed_at,
            bars[0].timestamp + timedelta(days=30),
        )

    def test_missing_immediate_entry_bar_cannot_become_a_signal(self):
        bars, _snapshots, _quotes = self._fit()
        baseline = measure_fit_diagnostics(bars, ROOT_SPEC)
        # The public diagnostic intentionally exposes only aggregates. Locate
        # the earliest candidate via the same metadata helper without adding
        # raw rows to the diagnostic contract.
        from research.fit_diagnostics import _fit_prefixes
        prefix = _fit_prefixes(bars, ROOT_SPEC)
        self.assertTrue(prefix["first_signals"])
        signal_at = datetime.fromisoformat(prefix["first_signals"][0]["signal_timestamp"])
        altered = [row for row in bars
                    if row.timestamp != signal_at]
        diagnostic = measure_fit_diagnostics(altered, ROOT_SPEC)
        self.assertNotIn(prefix["first_signals"][0]["signal_timestamp"],
                         [item["signal_timestamp"]
                          for item in _fit_prefixes(altered, ROOT_SPEC)["first_signals"]])
        self.assertLessEqual(diagnostic["first_signal"]["signals"],
                             baseline["first_signal"]["signals"])

    def test_entry_unavailable_prefixes_fail_open_as_incomplete_data(self):
        bars, _snapshots, _quotes = self._fit()
        # Keep exactly one mature signal prefix per cell, then delay the
        # signal bar's observation beyond the only following bar.  The
        # decision timestamp therefore has no available entry row; it must
        # not be reclassified as a predicate-level no-actionable result.
        grouped = {}
        for row in bars:
            grouped.setdefault((row.symbol, row.session_date), []).append(row)
        altered = []
        for rows in grouped.values():
            rows = sorted(rows, key=lambda item: item.timestamp)[:17]
            delayed = replace(
                rows[15],
                identity=replace(
                    rows[15].identity,
                    observed_at=rows[15].end + timedelta(days=1),
                ),
            )
            altered.extend(rows[:15] + [delayed, rows[16]])
        prefix = _fit_prefixes(altered, ROOT_SPEC)
        self.assertEqual(prefix["eligible_prefixes"], 0)
        self.assertGreater(prefix["prefix_status_counts"].get(
            "entry_bar_unavailable", 0), 0)
        self.assertEqual(prefix["eligibility_provenance"]["status"],
                         "data_incomplete")
        diagnostic = measure_fit_diagnostics(altered, ROOT_SPEC)
        self.assertEqual(diagnostic["first_signal"]["signals"], 0)
        self.assertNotEqual(
            diagnostic["signal_quality"]["event_rejection_counts"],
            {"no_actionable_signal": len(grouped)})
        self.assertEqual(
            diagnostic["signal_quality"]["eligibility_provenance"]["status"],
            "data_incomplete")

    def test_alias_canonicalization_is_review_only_and_zero_signal_is_kept(self):
        bars, _snapshots, _quotes = self._fit()
        left = ROOT_SPEC
        right = dict(ROOT_SPEC, threshold_bps=4.0)
        left_diag = measure_fit_diagnostics(bars, left)
        right_diag = measure_fit_diagnostics(bars, right)
        result = collapse_behavior_aliases(
            [{"rule_spec": left, "source": "deterministic"},
             {"rule_spec": right, "source": "deterministic"}],
            diagnostics={left_diag["variant_id"]: left_diag,
                         right_diag["variant_id"]: right_diag})
        self.assertEqual(len(result["kept"]), 2)
        self.assertEqual(result["excluded"], [])
        self.assertEqual(result["dedup_status"], "diagnostic_only")
        self.assertTrue(result["requires_operator_review"])
        self.assertEqual(result["intended_variant_count"],
                         result["kept_variant_count"])

    def test_fit_only_freeze_collapses_cross_family_alias_but_not_zero_signal(self):
        left, right, zero_left, zero_right = [
            item.rule_spec for item in initial_hypotheses(4)]

        def diagnostic(key, *, signals):
            return {
                "scope": "fit_only",
                "behavior_fingerprint": {
                    "fit_evidence_key": "sealed-fit-corpus",
                    "entry_alias_key": f"entry-{key}",
                    "full_alias_key": f"full-{key}",
                    "alias_schema": FIT_BEHAVIOR_ALIAS_SCHEMA,
                    "alias_numeric_decimals": FIT_BEHAVIOR_ALIAS_DECIMALS,
                    "signal_count": signals,
                    "planned_vector_count": signals,
                },
            }

        records = [
            {"candidate_key": "left", "rule_spec": left, "source": "llm",
             "fit_diagnostics": diagnostic("shared", signals=3)},
            {"candidate_key": "right", "rule_spec": right,
             "source": "deterministic",
             "fit_diagnostics": diagnostic("shared", signals=3)},
            {"candidate_key": "zero-left", "rule_spec": zero_left,
             "source": "llm",
             "fit_diagnostics": diagnostic("zero", signals=0)},
            {"candidate_key": "zero-right", "rule_spec": zero_right,
             "source": "deterministic",
             "fit_diagnostics": diagnostic("zero", signals=0)},
        ]
        frozen = collapse_behavior_aliases(records, freeze=True)
        self.assertEqual(frozen["dedup_status"], "fit_preregistered_frozen")
        self.assertFalse(frozen["requires_operator_review"])
        self.assertEqual(frozen["intended_variant_count"], 4)
        self.assertEqual(frozen["kept_variant_count"], 3)
        self.assertEqual(
            {item["candidate_key"] for item in frozen["kept"]},
            {"right", "zero-left", "zero-right"})
        self.assertEqual(frozen["excluded"][0]["candidate_key"], "left")
        self.assertEqual(
            frozen["excluded"][0]["canonical_candidate_key"], "right")
        self.assertEqual(
            set(frozen["full_aliases"][0]["families"]),
            {left["family"], right["family"]})

        # Freeze mode fails open if the diagnostic is not explicitly fit-only.
        unscoped = [{**record,
                     "fit_diagnostics": {
                         "behavior_fingerprint": record["fit_diagnostics"]
                         ["behavior_fingerprint"]}}
                    for record in records[:2]]
        retained = collapse_behavior_aliases(unscoped, freeze=True)
        self.assertEqual(retained["kept_variant_count"], 2)
        self.assertEqual(retained["excluded"], [])

    def test_near_exact_alias_quantization_only_removes_numeric_noise(self):
        metadata = {
            "session_date": "2026-01-05", "symbol": "SPY",
            "direction": "long", "signal_timestamp": "2026-01-05T14:45:00Z",
            "entry_price": 100.0, "planned_stop_distance": 1.0,
            "planned_target_distance": 2.0, "target_r": 2.0,
            "planned_hold_bars": 30,
        }
        noisy = {**metadata, "planned_stop_distance": 1.000000001}
        material = {**metadata, "planned_stop_distance": 1.000001}
        self.assertNotEqual(_planned_vector(metadata, full=True),
                            _planned_vector(noisy, full=True))
        self.assertEqual(
            _planned_vector(metadata, full=True,
                            decimals=FIT_BEHAVIOR_ALIAS_DECIMALS),
            _planned_vector(noisy, full=True,
                            decimals=FIT_BEHAVIOR_ALIAS_DECIMALS))
        self.assertNotEqual(
            _planned_vector(metadata, full=True,
                            decimals=FIT_BEHAVIOR_ALIAS_DECIMALS),
            _planned_vector(material, full=True,
                            decimals=FIT_BEHAVIOR_ALIAS_DECIMALS))

    def test_stress_costs_are_bps_not_multipliers_and_include_option_fees(self):
        equity = measure_fit_diagnostics(
            [], ROOT_SPEC, vehicle="equity", costs=CostModel(), account_rows=[{
                "vehicle": "equity", "no_trade": False, "plan_entry": 100.0,
                "quantity": 10, "contract_multiplier": 1, "risk_usd": 10.0,
                "costs": 1.0,
            }])
        self.assertAlmostEqual(equity["cost_to_risk"]["stressed"]["9"]["total_cost"],
                               .9)
        self.assertAlmostEqual(equity["cost_to_risk"]["stressed"]["9"]["cost_to_risk_ratio"],
                               .09)

        option_model = CostModel(option_fee_per_contract_side=.65)
        option = measure_fit_diagnostics(
            [], ROOT_SPEC, vehicle="option", costs=option_model, account_rows=[{
                "vehicle": "option", "no_trade": False, "plan_entry": 2.0,
                "quantity": 2, "contract_multiplier": 100, "risk_usd": 100.0,
                "costs": 1.0,
            }])
        # $400 entry notional * 9 bps + two contracts x two sides x $0.65.
        self.assertAlmostEqual(option["cost_to_risk"]["stressed"]["9"]["total_cost"],
                               2.96)
        self.assertAlmostEqual(option["cost_to_risk"]["stressed"]["9"]["cost_to_risk_ratio"],
                               .0296)

    def test_fit_output_exposes_provenance_pricing_and_risk_controls(self):
        bars, _snapshots, _quotes = self._fit()
        diagnostic = measure_fit_diagnostics(
            bars, ROOT_SPEC,
            account_rows=[{
                "vehicle": "equity", "no_trade": False,
                "plan_entry": 100.0, "quantity": 1,
                "contract_multiplier": 1, "risk_usd": 0.5,
                "entry_fill_source": "quote", "exit_fill_source": "quote",
                "entry_feed": "sip", "exit_feed": "sip",
                "entry_provider": "alpaca", "exit_provider": "alpaca",
            }, {"vehicle": "equity", "no_trade": False}],
            risk_config={"risk": {
                "stressed_cost_scenario_bps": 25.0,
                "max_stressed_cost_to_risk_ratio": 0.30,
            }},
        )
        self.assertFalse(diagnostic["authorizing"])
        self.assertTrue(diagnostic["diagnostic_only"])
        provenance = diagnostic["provenance"]
        self.assertEqual(
            set(provenance["feeds"]) | set(provenance["fills"]["feeds"]),
            {"iex", "sip"})
        self.assertEqual(set(provenance["providers"]), {"test"})
        self.assertGreater(provenance["observations"], 0)
        pricing = diagnostic["entry_pricing"]
        self.assertEqual(pricing["signals"], pricing["quote_required"])
        self.assertEqual(pricing["bar_available"], 0)
        controls = diagnostic["risk_controls"]
        self.assertEqual(controls["stressed_cost_scenario_bps"], 25.0)
        self.assertEqual(controls["max_stressed_cost_to_risk_ratio"], 0.30)
        self.assertEqual(controls["basis"]["notional"], "entry_notional")
        statuses = diagnostic["cost_to_risk"]["stressed"]["25"]["row_status"]
        self.assertEqual(statuses, {"pass": 0, "fail": 1, "unknown": 1})

    def test_fit_risk_names_configured_budget_and_capped_delivery(self):
        diagnostic = measure_fit_diagnostics(
            [], ROOT_SPEC,
            account_rows=[{
                "vehicle": "equity", "no_trade": False,
                "risk_budget": 500.0, "risk_usd": 117.5,
            }])
        risk = diagnostic["risk"]
        self.assertEqual(risk["configured"]["median"], 500.0)
        self.assertEqual(risk["capped_delivered"]["median"], 117.5)
        self.assertEqual(
            risk["delivered_to_configured"]["median"], 0.235)
        # Existing machine consumers retain their original aliases.
        self.assertEqual(risk["intended"], risk["configured"])
        self.assertEqual(risk["delivered"], risk["capped_delivered"])

    def test_fit_output_keeps_no_trade_rejection_counts(self):
        diagnostic = measure_fit_diagnostics(
            [], ROOT_SPEC,
            account_rows=[
                {"vehicle": "equity", "no_trade": True,
                 "reject_reason": "stressed_cost_risk_limit"},
                {"vehicle": "equity", "no_trade": True,
                 "reject_reason": "entry_slippage_exceeds_limit"},
                {"vehicle": "equity", "no_trade": False},
            ])
        summary = diagnostic["execution_rejections"]
        self.assertEqual(summary["no_trade_rows"], 2)
        self.assertEqual(summary["executed_rows"], 1)
        self.assertEqual(summary["reject_reason_counts"], {
            "entry_slippage_exceeds_limit": 1,
            "stressed_cost_risk_limit": 1,
        })
        self.assertFalse(summary["execution_blocked"])

    def test_fit_output_includes_predicate_and_forward_return_diagnostics(self):
        bars, _snapshots, _quotes = self._fit()
        diagnostic = measure_fit_diagnostics(
            bars, ROOT_SPEC, costs=CostModel(), vehicle="equity")
        funnel = diagnostic["predicate_funnel"]
        self.assertEqual(funnel["schema"], "rule-predicate-funnel.v1")
        self.assertFalse(funnel["authorizing"])
        self.assertIn("family_predicate", funnel["stages"])
        self.assertGreater(
            funnel["stages"]["family_predicate"]["tested"], 0)
        quality = diagnostic["signal_quality"]
        self.assertEqual(quality["schema"], "signal-quality.v2")
        self.assertFalse(quality["authorizing"])
        self.assertGreater(quality["event_count"], 0)
        self.assertEqual(
            quality["horizon_metrics"]["5m"]["cost_hurdle_bps"], 17.0)
        self.assertEqual(
            diagnostic["expected_cost"]["bar_reference_round_trip_bps"], 17.0)
        self.assertEqual(
            diagnostic["expected_cost"]["executable_quote_round_trip_bps"], 13.0)
        controls = diagnostic["risk_controls"]
        self.assertAlmostEqual(
            controls["required_static_stop_distance_bps"], 25.0 / 0.30)
        self.assertFalse(controls["grammar_stop_floor_admissible"])

    def test_realized_fill_pricing_separates_mixed_and_bar_exit_paths(self):
        diagnostic = measure_fit_diagnostics(
            [], ROOT_SPEC, costs=CostModel(), vehicle="equity",
            account_rows=[
                {"vehicle": "equity", "no_trade": False,
                 "entry_fill_source": "quote", "exit_fill_source": "bar",
                 "entry_quote_age_seconds": 1.5, "entry_feed": "iex",
                 "entry_provider": "alpaca", "net_pnl": 10.0},
                {"vehicle": "equity", "no_trade": False,
                 "entry_fill_source": "bar", "exit_fill_source": "bar",
                 "net_pnl": -4.0},
            ])
        pricing = diagnostic["realized_fill_pricing"]
        self.assertEqual(pricing["source_pair_counts"], {
            "bar->bar": 1, "quote->bar": 1})
        self.assertEqual(pricing["mixed"], 1)
        self.assertEqual(pricing["both_bar"], 1)
        self.assertEqual(pricing["entry_quote_age_seconds"]["median"], 1.5)
        self.assertIn("intrabar", pricing["intrabar_bar_exit_caveat"])

    def test_non_opportunity_data_refusal_is_not_execution_blocked(self):
        diagnostic = measure_fit_diagnostics(
            [], ROOT_SPEC,
            account_rows=[{
                "vehicle": "equity", "no_trade": True,
                "execution_disposition": "refused",
                "signal_opportunity": False,
                "reject_reason": "invalid_session_bars",
            }])
        summary = diagnostic["execution_rejections"]
        self.assertEqual(summary["explicit_rejections"], 1)
        self.assertFalse(summary["execution_blocked"])

    def test_bar_coverage_reports_sparse_sessions_without_false_calendar_precision(self):
        bars, _snapshots, _quotes = self._fit()
        sparse = [row for row in bars if row.timestamp.minute % 5 != 0]
        coverage = bar_coverage_telemetry(sparse)
        self.assertEqual(coverage["schema"], BAR_COVERAGE_SCHEMA)
        expected_sessions = len({(row.symbol, row.session_date) for row in bars})
        self.assertEqual(coverage["session_count"], expected_sessions)
        self.assertEqual(coverage["unknown_expected_sessions"],
                         coverage["session_count"])
        self.assertIsNone(coverage["expected_minutes"])
        self.assertIsNone(coverage["coverage_ratio"])
        self.assertIn("early_close_unknown", coverage["caveats"])
        self.assertIn("exact_session_calendar_missing", coverage["caveats"])
        for symbol_sessions in coverage["by_symbol_session"].values():
            for record in symbol_sessions.values():
                self.assertEqual(record["status"], "unknown_expected")
                self.assertIsNone(record["expected_minutes"])

        # Keep behavior fingerprints independent of this corpus-level
        # reporting aggregate.
        diagnostic = measure_fit_diagnostics(sparse, ROOT_SPEC)
        # Coverage belongs to the input corpus.  The factory persists one
        # detailed record instead of repeating it in every fit result.
        self.assertNotIn("bar_coverage", diagnostic)

    def test_bar_coverage_uses_exact_regular_and_early_close_bounds(self):
        bars, _snapshots, _quotes = self._fit()
        expected_sessions = len({(row.symbol, row.session_date) for row in bars})
        regular = []
        for row in bars:
            opened = row.timestamp.astimezone(ZoneInfo("America/New_York")).replace(
                hour=9, minute=30, second=0, microsecond=0)
            regular.append(replace(
                row, session_open=opened,
                session_close=opened + timedelta(minutes=390)))
        exact = bar_coverage_telemetry(regular)
        self.assertEqual(exact["expected_minutes"], expected_sessions * 390)
        self.assertTrue(exact["expected_minutes_complete"])
        self.assertAlmostEqual(exact["coverage_ratio"],
                               exact["observed_minutes"] /
                               (expected_sessions * 390))
        self.assertFalse(exact["by_symbol_session"]["AAA"]
                         ["2026-01-05"]["early_close"])

        early = []
        for row in bars:
            opened = row.timestamp.astimezone(ZoneInfo("America/New_York")).replace(
                hour=9, minute=30, second=0, microsecond=0)
            early.append(replace(
                row, session_open=opened,
                session_close=opened + timedelta(minutes=210)))
        early_coverage = bar_coverage_telemetry(early)
        self.assertEqual(early_coverage["expected_minutes"], expected_sessions * 210)
        self.assertTrue(early_coverage["by_symbol_session"]["AAA"]
                        ["2026-01-05"]["early_close"])
        self.assertIn("early_close_exact_calendar",
                      early_coverage["caveats"])

        delayed = []
        for row in bars:
            opened = row.timestamp.astimezone(ZoneInfo("America/New_York")).replace(
                hour=10, minute=0, second=0, microsecond=0)
            delayed.append(replace(
                row, session_open=opened,
                session_close=opened.replace(hour=16)))
        delayed_coverage = bar_coverage_telemetry(delayed)
        delayed_record = delayed_coverage["by_symbol_session"]["AAA"]["2026-01-05"]
        self.assertEqual(delayed_record["expected_minutes"], 360)
        self.assertFalse(delayed_record["early_close"])
        self.assertIn("non_standard_session_open", delayed_record["caveats"])


if __name__ == "__main__":
    unittest.main()
