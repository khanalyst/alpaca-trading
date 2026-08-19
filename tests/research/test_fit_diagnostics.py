"""Fit-only diagnostic invariants and exact execution-floor measurements."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest

from agent.contracts.rule import evaluate_rule_signal_metadata
from research.edge_lab import _read_discovery_rows
from research.costs import CostModel
from research.fit_diagnostics import (
    collapse_behavior_aliases, measure_fit_diagnostics,
)
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
        self.assertEqual(set(provenance["feeds"]), {"sip"})
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


if __name__ == "__main__":
    unittest.main()
