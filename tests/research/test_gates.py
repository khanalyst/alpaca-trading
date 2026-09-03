import copy
import pickle
import unittest

from research.gates import (
    AcceptanceFloor, SealedWindowError, chronological_split,
    deterministic_placebo_deltas, falsification_gate, matched_cluster_test,
    expectancy_rejection_report, gate_dependence_report, paired_delta, performance_floor,
    placebo_null_distribution,
    fdr_batch_evidence, qualification_report, recompute_gate_statistics,
    seal_final_window, structural_floor,
    risk_unit_report, verified_gate_envelope, verify_gate_envelope,
    walk_forward_report, max_drawdown_of, paired_control_adequacy,
)
from research.costs import (RESTING_BRACKET, RESTING_BRACKET_FILL_SCHEMA,
                             resting_bracket_fill_claim)


def _rows(count, *, net, start=2, symbol="SPY", prefix="candidate"):
    return [{"vehicle": "equity", "symbol": symbol,
             "session_date": f"2024-01-{day:02d}",
             "opportunity_id": f"{prefix}-{day}",
             "net_pnl": float(net(day) if callable(net) else net)}
            for day in range(start, start + count)]


class EvidenceGateTests(unittest.TestCase):
    @staticmethod
    def _equity_quote_row(**changes):
        row = {
            "vehicle": "equity", "symbol": "SPY", "session_date": "2026-01-02",
            "opportunity_id": "equity-1", "net_pnl": 20.0,
            "entry_fill_source": "quote", "exit_fill_source": "quote",
            "entry_feed": "iex", "exit_feed": "iex",
            "entry_provider": "alpaca", "exit_provider": "alpaca",
            "entry_quote_age_seconds": 0.0,
            "exit_quote_age_seconds": 0.0,
        }
        row.update(changes)
        return row

    def test_equity_fill_quality_binds_to_configured_feed_on_both_legs(self):
        from research.gates import fill_source_summary
        self.assertTrue(fill_source_summary(
            [self._equity_quote_row()], vehicle="equity")["adequate"])
        sip = self._equity_quote_row(entry_feed="sip", exit_feed="sip")
        self.assertTrue(fill_source_summary(
            [sip], vehicle="equity", equity_feed="sip")["adequate"])
        self.assertFalse(fill_source_summary(
            [sip], vehicle="equity", equity_feed="iex")["adequate"])
        for changes in (
                {"entry_feed": "sip"}, {"exit_feed": "sip"},
                {"entry_feed": "delayed_sip"}, {"exit_feed": "delayed_sip"},
                {"entry_feed": None}, {"exit_feed": None},
                {"entry_feed": "unknown"}, {"exit_feed": "unknown"},
                {"entry_fill_source": "bar"}, {"exit_fill_source": "bar"},
                {"entry_provider": None}, {"exit_provider": ""}):
            with self.subTest(changes=changes):
                self.assertFalse(fill_source_summary(
                    [self._equity_quote_row(**changes)],
                    vehicle="equity")["adequate"])

    def test_authorization_projection_is_symmetric_across_candidate_and_baseline(self):
        from research.gates import authorization_projection
        good = self._equity_quote_row()
        bar = {**good, "opportunity_id": "bar", "entry_fill_source": "bar",
               "exit_fill_source": "bar"}
        refused = {**good, "opportunity_id": "refused", "no_trade": True}
        candidate = authorization_projection([good, bar, refused], vehicle="equity")
        baseline = authorization_projection([good, bar, refused], vehicle="equity")
        self.assertEqual(candidate["counts"], {"raw": 3, "eligible": 1, "excluded": 2})
        self.assertEqual(candidate["counts"], baseline["counts"])
        self.assertEqual(candidate["reasons"], {
            "no_trade": 1, "non_authorizing_fill_source": 1})

    def test_resting_bracket_source_is_symmetric_for_candidate_and_null(self):
        from research.gates import authorization_projection
        row = self._equity_quote_row(
            exit_fill_source=RESTING_BRACKET,
            exit_fill_schema=RESTING_BRACKET_FILL_SCHEMA,
            exit_reason="target", exit_reference=103.0,
            stop_price=99.0, target_price=103.0, active_stop_price=99.0,
            tie_broken=False, gap_fill=False, entry_gap_fill=False,
            exit_gap_fill=False,
            signal_bar_feed="iex", signal_bar_provider="alpaca",
            entry_bar_feed="iex", entry_bar_provider="alpaca",
            exit_bar_feed="iex", exit_bar_provider="alpaca",
            exit_fill_bar_timestamp="2026-01-02T14:35:00+00:00",
        )
        row["exit_fill_claim"] = resting_bracket_fill_claim(
            exit_reason="target", exit_reference=103.0,
            stop_price=99.0, target_price=103.0,
            bar_timestamp=row["exit_fill_bar_timestamp"],
            bar_feed="iex", bar_provider="alpaca")
        candidate = authorization_projection([row], vehicle="equity")
        null = authorization_projection([dict(row)], vehicle="equity")
        self.assertEqual(candidate["counts"], {"raw": 1, "eligible": 1, "excluded": 0})
        self.assertEqual(candidate["counts"], null["counts"])
        malformed = {**row, "exit_fill_claim": {**row["exit_fill_claim"],
                                                 "planned_level": 104.0}}
        self.assertEqual(authorization_projection(
            [malformed], vehicle="equity")["counts"],
            {"raw": 1, "eligible": 0, "excluded": 1})

    def test_cost_stress_report_names_its_entry_notional_basis(self):
        from research.costs import STRESSED_COST_BASIS, STRESSED_COST_SCHEMA
        from research.gates import cost_stress_report

        row = self._equity_quote_row(
            entry_price=100.0, exit_price=101.0, quantity=1.0,
            gross_pnl=1.0, costs=.1, net_pnl=.9)
        report = cost_stress_report([row], vehicle="equity", risk_report={})
        self.assertEqual(report["stress_basis_schema"], STRESSED_COST_SCHEMA)
        self.assertEqual(report["stress_basis"], STRESSED_COST_BASIS)
        self.assertEqual(report["required_entry_notional_bps"], 25.0)
        required = next(item for item in report["scenarios"]
                        if item["entry_notional_bps"] == 25.0)
        self.assertEqual(required["stress_basis_schema"], STRESSED_COST_SCHEMA)
        self.assertEqual(required["stress_basis"], STRESSED_COST_BASIS)
        # The old field remains an explicit compatibility alias, not the
        # authoritative description of how the stress is charged.
        self.assertEqual(required["round_trip_bps"], 25.0)

    def test_qualification_excludes_no_trade_and_bar_fallback_rows(self):
        from research.gates import qualification_report
        good = self._equity_quote_row()
        rows = [good,
                {**good, "opportunity_id": "bar", "entry_fill_source": "bar",
                 "exit_fill_source": "bar", "session_date": "2026-01-03"},
                {**good, "opportunity_id": "none", "no_trade": True,
                 "session_date": "2026-01-04"}]
        baseline = [{**row, "net_pnl": 0.0} for row in rows]
        report = qualification_report(
            rows, baseline, vehicle="equity",
            sessions=["2026-01-02", "2026-01-03", "2026-01-04"],
            min_trades=1, min_sessions=1, min_clusters=1)
        self.assertEqual(report["trades"], 1)
        self.assertEqual(report["sessions"], ["2026-01-02"])
        self.assertEqual(report["authorization_projection"]["candidate"]["counts"],
                         {"raw": 3, "eligible": 1, "excluded": 2})

    def test_qualification_derives_a_risk_scaled_drawdown_limit(self):
        from research.gates import qualification_report
        candidate = self._equity_quote_row(risk_usd=12.5, net_pnl=2.0)
        baseline = {**candidate, "net_pnl": 0.0}
        report = qualification_report(
            [candidate], [baseline], vehicle="equity",
            sessions=["2026-01-02"], min_trades=1, min_sessions=1,
            min_clusters=1, max_drawdown_r=10.0)
        self.assertEqual(report["max_drawdown_limit"], 125.0)
        self.assertEqual(report["max_drawdown_limit_source"],
                         "median_risk_usd_times_r")
        self.assertEqual(report["max_drawdown_limit_r"], 10.0)
        self.assertTrue(report["drawdown_within_limit"])

        no_risk = qualification_report(
            [{key: value for key, value in candidate.items()
              if key != "risk_usd"}],
            [{key: value for key, value in baseline.items()
              if key != "risk_usd"}],
            vehicle="equity", sessions=["2026-01-02"], min_trades=1,
            min_sessions=1, min_clusters=1)
        self.assertIsNone(no_risk["max_drawdown_limit"])
        self.assertFalse(no_risk["drawdown_within_limit"])
        self.assertFalse(no_risk["adequate"])

    def test_opra_option_rows_satisfy_fill_and_risk_evidence(self):
        rows = [{
            "vehicle": "option", "symbol": "SPY",
            "session_date": "2026-01-02", "opportunity_id": "opra-1",
            "net_pnl": 20.0, "entry_price": 2.0, "exit_price": 2.2,
            "quantity": 1.0, "contract_multiplier": 100,
            "risk_usd": 200.0, "entry_fill_source": "quote",
            "exit_fill_source": "quote", "entry_quote_age_seconds": 0.0,
            "exit_quote_age_seconds": 0.0, "entry_feed": "opra",
            "exit_feed": "opra", "entry_provider": "alpaca",
            "exit_provider": "alpaca",
        }]
        from research.gates import fill_source_summary
        self.assertTrue(fill_source_summary(rows, vehicle="option")["adequate"])
        report = risk_unit_report(rows, vehicle="option")
        self.assertTrue(report["adequate"])
        envelope = verified_gate_envelope(
            lane="shadow", vehicle="option", fit=[], heldout=rows,
            fit_floor={}, heldout_floor={}, control={}, p_value=1.0,
            q_value=1.0, alpha=.05, falsification={}, separation={},
            checks={}, passes=False, risk_unit_report=report)
        self.assertTrue(envelope["checks"]["fill_quality_adequate"])
        self.assertTrue(envelope["checks"]["risk_unit_adequate"])

    def test_indicative_option_rows_fail_fill_quality_even_with_quote_ages(self):
        row = {
            "vehicle": "option", "opportunity_id": "indicative-1",
            "entry_quote_age_seconds": 0.0, "exit_quote_age_seconds": 0.0,
            "entry_feed": "indicative", "exit_feed": "indicative",
            "entry_price": 2.0, "exit_price": 2.2, "quantity": 1,
            "contract_multiplier": 100, "risk_usd": 200.0,
        }
        from research.gates import fill_source_summary
        self.assertFalse(fill_source_summary([row], vehicle="option")["adequate"])

    def test_option_quote_age_authorization_caps_both_legs_at_thirty_seconds(self):
        row = {
            "vehicle": "option", "opportunity_id": "fresh-option-1",
            "entry_quote_age_seconds": 30.0,
            "exit_quote_age_seconds": 30.0,
            "entry_feed": "opra", "exit_feed": "opra",
            "entry_fill_source": "quote", "exit_fill_source": "quote",
        }
        from research.gates import fill_source_summary
        self.assertTrue(fill_source_summary([row], vehicle="option")["adequate"])
        self.assertFalse(fill_source_summary(
            [{**row, "entry_quote_age_seconds": 31.0}],
            vehicle="option")["adequate"])
        self.assertFalse(fill_source_summary(
            [{**row, "exit_quote_age_seconds": 31.0}],
            vehicle="option")["adequate"])

    def test_equity_quote_age_authorization_caps_both_legs_at_thirty_seconds(self):
        row = {
            "vehicle": "equity", "opportunity_id": "fresh-equity-1",
            "entry_quote_age_seconds": 30.0,
            "exit_quote_age_seconds": 30.0,
            "entry_feed": "iex", "exit_feed": "iex",
            "entry_provider": "alpaca", "exit_provider": "alpaca",
            "entry_fill_source": "quote", "exit_fill_source": "quote",
        }
        from research.gates import fill_source_summary
        self.assertTrue(fill_source_summary([row], vehicle="equity")["adequate"])
        self.assertFalse(fill_source_summary(
            [{**row, "entry_quote_age_seconds": 31.0}],
            vehicle="equity")["adequate"])
        self.assertFalse(fill_source_summary(
            [{**row, "exit_quote_age_seconds": 31.0}],
            vehicle="equity")["adequate"])

    def test_floor_is_vehicle_local_and_no_trade_rows_do_not_count_as_trades(self):
        rows = [
            {"vehicle": "equity", "session_date": "2024-01-02",
             "net_pnl": 0, "no_trade": True},
            {"vehicle": "equity", "session_date": "2024-01-03",
             "net_pnl": 1, "no_trade": False},
        ]
        floor = AcceptanceFloor(min_trades=1, min_sessions=2)
        self.assertTrue(floor.check(rows, vehicle="equity")["passes"])
        self.assertFalse(floor.check(rows, vehicle="option")["passes"])
        self.assertFalse(
            AcceptanceFloor(min_trades=2, min_sessions=2)
            .check(rows, vehicle="equity")["passes"])

    def test_duplicate_opportunities_are_excluded_from_either_arm(self):
        baseline = [{"vehicle": "equity", "opportunity_id": "a", "net_pnl": .5}]
        candidate = [
            {"vehicle": "equity", "opportunity_id": "a", "net_pnl": 1},
            {"vehicle": "equity", "opportunity_id": "a", "net_pnl": 2},
        ]
        self.assertEqual(
            paired_delta(candidate, baseline, vehicle="equity")["matched"], 0)
        self.assertEqual(
            paired_delta(baseline, candidate, vehicle="equity")["matched"], 0)

    def test_matched_cluster_test_materializes_one_shot_inputs(self):
        candidate = _rows(3, net=2.0)
        baseline = _rows(3, net=0.0, prefix="baseline")
        result = matched_cluster_test(
            (row for row in candidate), (row for row in baseline),
            vehicle="equity", min_matched=3, min_coverage=1.0)
        self.assertEqual(result["matched"], 3)
        self.assertEqual(result["paired_adequacy"]["matched"], 3)
        self.assertTrue(result["adequate"])

        adequacy = paired_control_adequacy(
            (row for row in candidate), (row for row in baseline),
            vehicle="equity", min_matched=3, min_coverage=1.0)
        self.assertEqual(adequacy["candidate_count"], 3)
        self.assertEqual(adequacy["control_count"], 3)
        self.assertTrue(adequacy["adequate"])

    def test_chronological_split_never_bisects_a_trading_session(self):
        rows = [
            {"session_date": "2024-01-02", "opportunity_id": f"a-{index}"}
            for index in range(3)
        ] + [{"session_date": "2024-01-03", "opportunity_id": "b"}]
        fit, heldout = chronological_split(rows, fit_fraction=.5)
        self.assertEqual({row["session_date"] for row in fit}, {"2024-01-02"})
        self.assertEqual({row["session_date"] for row in heldout}, {"2024-01-03"})
        self.assertEqual(len(fit), 3)

    def test_structural_adequacy_is_independent_of_profitability(self):
        rows = [{"vehicle": "equity", "session_date": f"2024-01-{day:02d}",
                 "opportunity_id": str(day), "net_pnl": -10.0}
                for day in (2, 3)]
        report = structural_floor(
            rows, vehicle="equity", min_trades=2, min_sessions=2)
        self.assertTrue(report["adequate"])
        self.assertFalse(report["performance_passes"])

    def test_overall_sample_cannot_mask_fit_or_heldout_floor_failure(self):
        rows = [{"vehicle": "equity", "session_date": f"2024-01-0{day}",
                 "opportunity_id": str(day), "net_pnl": 1.0}
                for day in (2, 3, 4, 5)]
        fit, heldout = chronological_split(rows, fit_fraction=.5)
        self.assertTrue(structural_floor(
            rows, vehicle="equity", min_trades=3, min_sessions=3)["adequate"])
        self.assertFalse(structural_floor(
            fit, vehicle="equity", min_trades=3, min_sessions=3)["adequate"])
        self.assertFalse(structural_floor(
            heldout, vehicle="equity", min_trades=3, min_sessions=3)["adequate"])

    def test_matched_control_and_placebo_are_deterministic_and_strict(self):
        # Eight clusters: the sign-flip null can reach 1/256, so a genuine
        # effect is detectable.  Four clusters could not go below 1/16.
        candidate = _rows(8, net=lambda day: float(day))
        baseline = _rows(8, net=0.0, prefix="root")
        first = matched_cluster_test(candidate, baseline, vehicle="equity")
        second = matched_cluster_test(candidate, baseline, vehicle="equity")
        self.assertEqual(first, second)
        self.assertEqual(first["matched"], 8)
        self.assertGreater(first["mean_delta_lcb"], 0)
        placebo = deterministic_placebo_deltas(candidate, baseline, vehicle="equity")
        self.assertEqual(placebo, deterministic_placebo_deltas(
            candidate, baseline, vehicle="equity"))
        self.assertEqual(len(placebo["placebo"]), placebo["draws"])
        self.assertGreaterEqual(placebo["draws"], 10_000)
        self.assertFalse(falsification_gate([1.0, 2.0], [0.0, 0.0])["passes"])
        self.assertTrue(falsification_gate(
            placebo["observed"], placebo["placebo"])["passes"])

    def test_placebo_is_a_null_distribution_not_a_sign_reflection(self):
        # Ten constant +10 deltas: the old gate reflected the observations
        # onto themselves, so the ratio was exactly 1.0 by construction and
        # the check reduced to "no delta is negative".
        candidate = _rows(10, net=10.0)
        baseline = _rows(10, net=0.0, prefix="root")
        null = placebo_null_distribution(candidate, baseline, vehicle="equity")
        self.assertEqual(null["observed"], [10.0] * 10)
        self.assertNotEqual(sorted(null["placebo"]), sorted([10.0, -10.0] * 5))
        self.assertGreater(max(null["placebo"]), min(null["placebo"]))
        decision = falsification_gate(null["observed"], null["placebo"])
        self.assertTrue(decision["passes"])
        # Ten independent positive daily clusters really are significant; the
        # calibrated p is near 2**-10, not the degenerate ratio of exactly 1.
        self.assertLess(decision["p_value"], .01)
        self.assertNotEqual(decision["ratio"], 1.0)

        # A mixed-sign sample with the same mean magnitude is now rejected,
        # where the reflected placebo would have scored the identical ratio.
        mixed = _rows(10, net=lambda day: 10.0 if day % 2 else -10.0)
        mixed_null = placebo_null_distribution(mixed, baseline, vehicle="equity")
        self.assertFalse(falsification_gate(
            mixed_null["observed"], mixed_null["placebo"])["passes"])

    def test_falsification_reuses_preregistered_p_but_keeps_null_guards(self):
        placebo = [-1.0, -.5, .5, 1.0]
        decision = falsification_gate(
            [2.0, 2.0], placebo, preregistered_p_value=.01)
        self.assertTrue(decision["passes"])
        self.assertEqual(decision["p_value"], .01)
        self.assertEqual(decision["p_value_source"],
                         "heldout_paired_cluster_sign_flip")
        self.assertTrue(decision["positive_mean"])
        self.assertTrue(decision["distinct"])
        self.assertTrue(decision["ratio_adequate"])
        dependence = gate_dependence_report({
            "checks": {"heldout_p_significant": True, "falsification": True},
            "statistics": {"p_value": .01, "alpha": .05},
            "falsification": decision,
        })
        self.assertEqual(
            dependence["shared_source_statistics"]["statistics.p_value"]
            ["checks"],
            ["falsification", "heldout_p_significant"])

        # A significant preregistered p-value is necessary but cannot bypass
        # the independent falsification guards.
        self.assertFalse(falsification_gate(
            [-2.0], placebo, preregistered_p_value=.001)["passes"])
        self.assertFalse(falsification_gate(
            [2.0], [0.0, 0.0], preregistered_p_value=.001)["passes"])
        self.assertFalse(falsification_gate(
            [1.0], [-10.0, 10.0],
            preregistered_p_value=.001)["passes"])
        for invalid in (True, -0.1, 1.1, float("nan")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                falsification_gate(
                    [2.0], placebo, preregistered_p_value=invalid)

    def test_placebo_draws_are_reproducible_from_persisted_content(self):
        candidate = _rows(9, net=4.0)
        baseline = _rows(9, net=0.0, prefix="root")
        control = matched_cluster_test(candidate, baseline, vehicle="equity")
        null = placebo_null_distribution(candidate, baseline, vehicle="equity")
        decision = falsification_gate(
            null["observed"], null["placebo"],
            preregistered_p_value=control["p_value"])
        envelope = verified_gate_envelope(
            lane="backtest", vehicle="equity", fit=[], heldout=candidate,
            fit_floor={}, heldout_floor={},
            control=control, p_value=control["p_value"], q_value=.01, alpha=.05,
            falsification={**decision, "draws": null["draws"],
                           "seed": null["seed"]},
            separation={"passes": True}, checks={"falsification": True},
            passes=True)
        recomputed = recompute_gate_statistics(envelope)
        self.assertTrue(recomputed["available"])
        self.assertAlmostEqual(recomputed["p_value"], control["p_value"])
        self.assertAlmostEqual(recomputed["falsification_p_value"],
                               control["p_value"])
        self.assertAlmostEqual(recomputed["mean_delta"], 4.0)
        self.assertAlmostEqual(recomputed["mean_delta_lcb"],
                               control["mean_delta_lcb"])
        self.assertTrue(recomputed["falsification_passes"])

    def test_absolute_profitability_is_required_independently_of_the_control(self):
        losing = _rows(6, net=-2.0)
        report = performance_floor(losing, vehicle="equity")
        self.assertFalse(report["net_pnl_positive"])
        self.assertFalse(report["expectancy_positive"])
        self.assertTrue(performance_floor(
            _rows(6, net=2.0), vehicle="equity")["expectancy_positive"])

    def test_retirement_requires_a_powered_upper_bound_rejection(self):
        def retirement_rows(count, value):
            return [
                {"vehicle": "equity", "symbol": "SPY",
                 "session_date": (f"2026-{1 + index // 28:02d}-"
                                  f"{1 + index % 28:02d}"),
                 "opportunity_id": f"trade-{index}", "net_pnl": value * 100,
                 "risk_usd": 100.0, "r_multiple": value}
                for index in range(count)
            ]

        rejected = expectancy_rejection_report(
            retirement_rows(30, -0.1), vehicle="equity")
        self.assertTrue(rejected["sample_sufficient"])
        self.assertTrue(rejected["rejects_minimum_useful_edge"])
        self.assertEqual(rejected["unit"], "r_multiple")
        self.assertLessEqual(rejected["upper_bound"], 0.05)

        thin = expectancy_rejection_report(
            retirement_rows(29, -0.1), vehicle="equity")
        self.assertFalse(thin["sample_sufficient"])
        self.assertFalse(thin["rejects_minimum_useful_edge"])

        still_plausible = expectancy_rejection_report(
            retirement_rows(30, 0.1), vehicle="equity")
        self.assertFalse(still_plausible["rejects_minimum_useful_edge"])

    def test_retirement_evidence_is_bound_to_verified_heldout_rows(self):
        heldout = [
            {**row, "r_multiple": -0.1, "risk_usd": 10.0}
            for row in _rows(30, net=-1.0)
        ]
        baseline = _rows(30, net=0.0, prefix="baseline")
        walk = walk_forward_report(
            heldout, baseline, vehicle="equity", folds=3)
        retirement = expectancy_rejection_report(heldout, vehicle="equity")
        negative = [item for item in walk["results"]
                    if item.get("adequate") and item.get("net_pnl", 0.0) <= 0.0]
        retirement.update({
            "negative_forward_folds": len(negative),
            "independent_negative_windows": [
                list(item.get("test_sessions") or ()) for item in negative],
            "multi_window_negative": len(negative) >= 2,
        })
        control = matched_cluster_test(heldout, baseline, vehicle="equity")
        envelope = verified_gate_envelope(
            lane="shadow", vehicle="equity", fit=[], heldout=heldout,
            heldout_baseline=baseline,
            fit_floor=structural_floor(
                [], vehicle="equity", min_trades=0, min_sessions=0,
                required=False),
            heldout_floor=structural_floor(
                heldout, vehicle="equity", min_trades=1, min_sessions=1),
            control=control, p_value=control["p_value"], q_value=1.0,
            alpha=.05, falsification={"passes": False},
            separation={"passes": True}, checks={"falsification": False},
            passes=False, walk_forward=walk, retirement=retirement,
            performance={"heldout_net_pnl": -30.0,
                         "heldout_expectancy": -1.0,
                         "max_drawdown": 30.0})
        self.assertTrue(verify_gate_envelope(envelope))

        tampered = copy.deepcopy(envelope)
        tampered["retirement"]["upper_bound"] = 99.0
        from research.gates import _content_hash
        tampered["content_hash"] = _content_hash({
            key: value for key, value in tampered.items()
            if key != "content_hash"
        })
        self.assertFalse(verify_gate_envelope(tampered))

    def test_walk_forward_requires_a_majority_of_positive_folds(self):
        baseline = _rows(8, net=0.0, prefix="root")
        good = walk_forward_report(_rows(8, net=1.0), baseline,
                                   vehicle="equity", folds=3)
        self.assertTrue(good["majority_positive"])
        self.assertEqual(good["positive_folds"], 3)
        # Only the last fold is positive: one lucky window is not an edge.
        alternating = _rows(8, net=lambda day: 5.0 if day >= 9 else -1.0)
        weak = walk_forward_report(alternating, baseline, vehicle="equity",
                                   folds=3)
        self.assertFalse(weak["majority_positive"])
        self.assertFalse(walk_forward_report(
            _rows(2, net=1.0), baseline, vehicle="equity")["available"])

    def test_sealed_qualification_window_cannot_be_reused_or_shipped(self):
        rows = _rows(10, net=1.0)
        development, sealed = seal_final_window(
            rows, session_of=lambda row: row["session_date"], fraction=.2)
        self.assertEqual(len(sealed.session_dates), 2)
        self.assertEqual({row["session_date"] for row in development} &
                         set(sealed.session_dates), set())
        with self.assertRaises(SealedWindowError):
            pickle.dumps(sealed)
        with self.assertRaises(SealedWindowError):
            copy.deepcopy(sealed)
        released = sealed.release(reason="final qualification")
        self.assertEqual(len(released), 2)
        with self.assertRaises(SealedWindowError):
            sealed.release(reason="second look")

    def test_verified_envelope_is_content_addressed(self):
        fit = [{"vehicle": "equity", "session_date": "2024-01-02",
                "opportunity_id": "fit", "net_pnl": 1.0}]
        heldout = [{"vehicle": "equity", "session_date": "2024-01-03",
                    "opportunity_id": "held", "net_pnl": 1.0}]
        fit_floor = structural_floor(
            fit, vehicle="equity", min_trades=1, min_sessions=1)
        held_floor = structural_floor(
            heldout, vehicle="equity", min_trades=1, min_sessions=1)
        envelope = verified_gate_envelope(
            lane="backtest", vehicle="equity", fit=fit, heldout=heldout,
            fit_floor=fit_floor, heldout_floor=held_floor,
            control={"actual_control": True, "available": True, "matched": 1},
            p_value=.01, q_value=.02, alpha=.05,
            falsification={"passes": True}, separation={"passes": True},
            checks={"family_fdr_significant": True}, passes=True)
        # Sparse evidence is deterministically downgraded rather than being
        # allowed to manufacture a passing envelope.  A tampered re-signing
        # that flips it back to a pass still fails strict verification.
        self.assertFalse(envelope["passes"])
        self.assertTrue(verify_gate_envelope(envelope))
        # Re-signing a code-owned decision flag cannot detach it from the
        # source evidence, even on a non-authorizing diagnostic envelope.
        derived_tamper = copy.deepcopy(envelope)
        derived_tamper["checks"]["fit_delta_positive"] = not derived_tamper[
            "checks"]["fit_delta_positive"]
        from research.gates import _content_hash
        derived_tamper["content_hash"] = _content_hash({
            key: value for key, value in derived_tamper.items()
            if key != "content_hash"
        })
        self.assertFalse(verify_gate_envelope(derived_tamper))
        envelope["passes"] = True
        self.assertFalse(verify_gate_envelope(envelope))

    def test_verified_envelope_recomputes_complete_fdr_batch(self):
        heldout = [{"vehicle": "equity", "session_date": "2024-01-03",
                    "opportunity_id": "held", "net_pnl": 1.0}]
        candidate_id = "fixture-candidate"
        batch = fdr_batch_evidence(
            candidate_id=candidate_id,
            family_name="fixture",
            family_candidate_key=candidate_id,
            global_candidate_key=candidate_id,
            family_values={"fixture": {candidate_id: .01, "sibling": .04}},
            global_values={candidate_id: .01, "sibling": .04},
            alpha=.05,
            p_value_source="gate")
        family_q = batch["family_results"]["fixture"][candidate_id]["p_adjusted"]
        global_q = batch["global_results"][candidate_id]["p_adjusted"]
        envelope = verified_gate_envelope(
            lane="backtest", vehicle="equity", fit=[], heldout=heldout,
            fit_floor=structural_floor(
                [], vehicle="equity", min_trades=0, min_sessions=0,
                min_clusters=0, required=False),
            heldout_floor=structural_floor(
                heldout, vehicle="equity", min_trades=1, min_sessions=1,
                min_clusters=1),
            control={},
            p_value=.01, q_value=global_q, family_q_value=family_q,
            alpha=.05, fdr_batch=batch,
            falsification={}, separation={},
            checks={"family_fdr_significant": family_q <= .05,
                    "global_fdr_significant": global_q <= .05},
            passes=False, candidate_id=candidate_id)
        self.assertTrue(envelope["checks"]["multiple_testing_batch_bound"])
        self.assertTrue(verify_gate_envelope(envelope))

        scalar_tamper = copy.deepcopy(envelope)
        scalar_tamper["statistics"]["q_value"] = .001
        from research.gates import _content_hash
        scalar_tamper["content_hash"] = _content_hash({
            key: value for key, value in scalar_tamper.items()
            if key != "content_hash"
        })
        self.assertFalse(verify_gate_envelope(scalar_tamper))

        batch_tamper = copy.deepcopy(envelope)
        replacement = fdr_batch_evidence(
            candidate_id=candidate_id,
            family_name="fixture",
            family_candidate_key=candidate_id,
            global_candidate_key=candidate_id,
            family_values={"fixture": {candidate_id: .02, "sibling": .04}},
            global_values={candidate_id: .02, "sibling": .04},
            alpha=.05,
            p_value_source="gate")
        batch_tamper["fdr_batch"] = replacement
        batch_tamper["content_hash"] = _content_hash({
            key: value for key, value in batch_tamper.items()
            if key != "content_hash"
        })
        self.assertFalse(verify_gate_envelope(batch_tamper))

    def test_cluster_fdr_batch_is_bound_to_the_frozen_policy(self):
        from research.gates import _fdr_batch_matches

        batch = fdr_batch_evidence(
            candidate_id="candidate", family_name="family",
            family_candidate_key="candidate",
            global_candidate_key="candidate",
            family_values={"family": {"candidate": .01}},
            global_values={"candidate": .01}, alpha=.05,
            cluster_name="cluster-a", cluster_candidate_key="candidate",
            cluster_values={"cluster-a": {"candidate": .01}},
            policy_hash="policy-a")
        statistics = {
            "p_value": .01, "family_q_value": .01,
            "q_value": .01, "cluster_q_value": .01, "alpha": .05,
        }
        checks = {
            "family_fdr_significant": True,
            "global_fdr_significant": True,
            "cluster_fdr_significant": True,
        }
        self.assertTrue(_fdr_batch_matches(
            batch, statistics=statistics, checks=checks, provenance={},
            candidate_id="candidate",
            cluster_multiple_tests={"policy_hash": "policy-a"}))
        tampered = copy.deepcopy(batch)
        tampered["policy_hash"] = "policy-b"
        self.assertFalse(_fdr_batch_matches(
            tampered, statistics=statistics, checks=checks, provenance={},
            candidate_id="candidate",
            cluster_multiple_tests={"policy_hash": "policy-a"}))
        with self.assertRaises(ValueError):
            fdr_batch_evidence(
                candidate_id="candidate", family_name="family",
                family_candidate_key="candidate",
                global_candidate_key="candidate",
                family_values={"family": {"candidate": .01}},
                global_values={"candidate": .01}, alpha=.05,
                cluster_name="cluster-a", cluster_candidate_key="candidate",
                cluster_values={"cluster-a": {"candidate": .01}})

    def test_legacy_envelope_without_feed_rebuilds_under_historical_sip(self):
        from research.gates import _content_hash

        sip = self._equity_quote_row(
            entry_feed="sip", exit_feed="sip", entry_price=100.0,
            exit_price=101.0, quantity=1.0, risk_usd=10.0)
        fit_floor = structural_floor(
            [], vehicle="equity", min_trades=0, min_sessions=0,
            required=False, equity_feed="sip")
        heldout_floor = structural_floor(
            [sip], vehicle="equity", min_trades=1, min_sessions=1,
            equity_feed="sip")
        envelope = verified_gate_envelope(
            lane="shadow", vehicle="equity", fit=[], heldout=[sip],
            fit_floor=fit_floor, heldout_floor=heldout_floor,
            control={}, p_value=1.0, q_value=1.0, alpha=.05,
            falsification={}, separation={}, checks={}, passes=False,
            equity_feed="sip")
        self.assertEqual(envelope["equity_feed"], "sip")
        self.assertEqual(
            envelope["authorization_projection"]["heldout"]["counts"]
            ["eligible"], 1)
        self.assertTrue(verify_gate_envelope(envelope))

        legacy = copy.deepcopy(envelope)
        legacy.pop("equity_feed")
        legacy["risk_unit_report"].pop("equity_feed", None)
        for projection in legacy["authorization_projection"].values():
            projection.pop("equity_feed", None)
        legacy["content_hash"] = _content_hash({
            key: value for key, value in legacy.items()
            if key != "content_hash"
        })
        self.assertTrue(verify_gate_envelope(legacy))

    def test_v3_null_control_cannot_lower_the_protocol_match_floor(self):
        """A diagnostic caller cannot turn one null pair into gate evidence."""
        from research import gates as gates_module

        heldout = _rows(30, net=1.0)
        baseline = [{**row, "net_pnl": 0.0} for row in heldout]
        null_source = [dict(baseline[0])]
        control = matched_cluster_test(heldout, baseline, vehicle="equity")
        thin_null = matched_cluster_test(
            heldout, null_source, vehicle="equity",
            min_matched=1, min_coverage=0.0)
        thin_null["minimum_matched"] = 1
        thin_null["minimum_coverage"] = 0.0
        envelope = verified_gate_envelope(
            lane="backtest", vehicle="equity", fit=[], heldout=heldout,
            fit_baseline=[], heldout_baseline=baseline,
            null_source=null_source,
            fit_floor=structural_floor(
                [], vehicle="equity", min_trades=0, min_sessions=0,
                min_clusters=0, required=False),
            heldout_floor=structural_floor(
                heldout, vehicle="equity", min_trades=1, min_sessions=1,
                min_clusters=1, required=False),
            control=control, p_value=control["p_value"], q_value=1.0,
            alpha=.05, falsification={}, separation={}, checks={},
            passes=False, qualification={}, null_control=thin_null,
            fit_control={}, online_fdr={
                "required": False, "status": "deferred_to_live_shadow",
                "tested": False, "decision": False},
            candidate_id="thin-null")
        adequacy = envelope["null_control"]["paired_adequacy"]
        self.assertEqual(adequacy["matched"], 1)
        self.assertEqual(
            adequacy["minimum_matched"], gates_module.NULL_CONTROL_MIN_MATCHED)
        self.assertFalse(adequacy["adequate"])
        self.assertTrue(envelope["null_control"]["raw_available"])
        self.assertFalse(envelope["null_control"]["available"])
        self.assertFalse(envelope["checks"]["null_control_available"])
        self.assertTrue(verify_gate_envelope(envelope))

        forged = copy.deepcopy(envelope)
        weak = paired_control_adequacy(
            heldout, null_source, vehicle="equity",
            min_matched=1, min_coverage=0.0)
        forged["null_control"].update({
            "paired_adequacy": weak,
            "coverage": weak["coverage"],
            "adequate": True,
            "available": True,
            "minimum_matched": 1,
            "minimum_coverage": 0.0,
        })
        forged["checks"]["null_control_available"] = True
        forged["content_hash"] = gates_module._content_hash({
            key: value for key, value in forged.items()
            if key != "content_hash"})
        self.assertFalse(verify_gate_envelope(forged))

    def test_legacy_v2_null_replay_preserves_symmetric_adequacy_and_no_trade(self):
        """Historical v2 null coverage is not reinterpreted as the v3 estimand."""
        from research import gates as gates_module

        heldout = _rows(5, net=1.0)
        baseline = [{**row, "net_pnl": 0.0} for row in heldout]
        # The old null arm had additional unmatched control rows.  Its v2
        # symmetric denominator therefore reported 5/10 coverage even though
        # the current candidate-only rule would report 5/5.
        null_source = baseline + [{
            **row, "session_date": f"2024-02-{index + 1:02d}",
            "opportunity_id": f"null-extra-{index}", "net_pnl": 0.0
        } for index, row in enumerate(heldout)]
        fit_floor = structural_floor(
            [], vehicle="equity", min_trades=0, min_sessions=0,
            min_clusters=0, required=False)
        heldout_floor = structural_floor(
            heldout, vehicle="equity", min_trades=1, min_sessions=1,
            min_clusters=1, required=False)
        control = matched_cluster_test(heldout, baseline, vehicle="equity")
        null_control = matched_cluster_test(
            heldout, null_source, vehicle="equity")
        envelope = verified_gate_envelope(
            lane="backtest", vehicle="equity", fit=[], heldout=heldout,
            fit_baseline=[], heldout_baseline=baseline,
            null_source=null_source, fit_floor=fit_floor,
            heldout_floor=heldout_floor, control=control,
            p_value=control["p_value"], q_value=1.0, alpha=.05,
            falsification={}, separation={}, checks={}, passes=False,
            qualification={}, null_control=null_control, fit_control={},
            online_fdr={"required": False,
                        "status": "deferred_to_live_shadow",
                        "tested": False, "decision": False},
            candidate_id="legacy-v2-null")
        self.assertTrue(verify_gate_envelope(envelope))
        legacy = copy.deepcopy(envelope)
        legacy["schema"] = gates_module.LEGACY_GATE_ENVELOPE_SCHEMA_V2
        legacy.pop("fdr_batch", None)
        legacy["checks"].pop("actual_control_adequate", None)
        legacy["checks"].pop("multiple_testing_batch_bound", None)
        # Match the actual v2 report shape.  The current matched-test helper
        # adds risk-unit and top-level adequacy fields that did not exist in
        # v2; only the separately persisted v2 paired-adequacy report remains.
        for key in (
                "coverage", "adequate", "raw_available", "r_deltas",
                "r_delta_clusters", "r_matched", "mean_r_delta",
                "r_delta_lcb", "r_lower_bound"):
            legacy["null_control"].pop(key, None)
        for key in (
                "paired_adequacy", "coverage", "adequate", "r_deltas",
                "r_delta_clusters", "r_matched", "mean_r_delta",
                "r_delta_lcb", "r_lower_bound"):
            legacy["control"].pop(key, None)
        # v2's actual-control check was descriptive (matched > 0), without
        # the v3 powered adequacy veto.
        legacy["checks"]["actual_control_available"] = True
        legacy_adequacy = gates_module._legacy_v2_paired_control_adequacy(
            heldout, null_source, vehicle="equity", min_matched=1,
            min_coverage=.8, equity_feed="iex")
        legacy["null_control"]["available"] = bool(null_control["available"])
        legacy["null_control"]["paired_adequacy"] = legacy_adequacy
        legacy["null_control"]["minimum_matched"] = 1
        legacy["null_control"]["minimum_coverage"] = .8
        legacy["passes"] = False
        legacy["content_hash"] = gates_module._content_hash({
            key: value for key, value in legacy.items()
            if key != "content_hash"})
        self.assertEqual(legacy_adequacy["coverage"], .5)
        self.assertTrue(verify_gate_envelope(legacy))
        tampered = copy.deepcopy(legacy)
        tampered["null_control"]["paired_adequacy"]["coverage"] = 1.0
        tampered["content_hash"] = gates_module._content_hash({
            key: value for key, value in tampered.items()
            if key != "content_hash"})
        self.assertFalse(verify_gate_envelope(tampered))

        # v2's matched statistic retained no-trade rows while adequacy did
        # not.  Preserve that deliberate audit quirk independently of v3.
        no_trade = {**heldout[0], "session_date": "2024-03-01",
                    "opportunity_id": "legacy-no-trade", "no_trade": True,
                    "net_pnl": 0.0}
        legacy_no_trade = gates_module._legacy_v2_paired_control_adequacy(
            [*heldout, no_trade], [*null_source, no_trade],
            vehicle="equity", min_matched=1, min_coverage=.8,
            equity_feed="iex")
        self.assertEqual(legacy_no_trade["candidate_count"], len(heldout))
        self.assertEqual(legacy_no_trade["control_count"], len(null_source))
        self.assertEqual(legacy_no_trade["matched"], len(heldout) + 1)
        self.assertAlmostEqual(legacy_no_trade["coverage"],
                               (len(heldout) + 1) / len(null_source))

    def test_qualification_source_digests_are_recomputed_and_tampering_fails(self):
        candidate = _rows(3, net=2.0)
        baseline = _rows(3, net=0.0, prefix="baseline")
        qualification = qualification_report(
            candidate, baseline, vehicle="equity",
            sessions=sorted({row["session_date"] for row in candidate}))
        self.assertTrue(qualification["candidate_observation_digest"])
        control = matched_cluster_test(candidate, baseline, vehicle="equity")
        envelope = verified_gate_envelope(
            lane="shadow", vehicle="equity", fit=[], heldout=candidate,
            fit_floor=structural_floor([], vehicle="equity", min_trades=0,
                                        min_sessions=0, required=False),
            heldout_floor=structural_floor(
                candidate, vehicle="equity", min_trades=1, min_sessions=1),
            control={**control, "actual_control": True},
            p_value=control["p_value"], q_value=.5, alpha=.05,
            falsification={"passes": False},
            separation={"passes": True},
            checks={"global_fdr_significant": False,
                    "heldout_delta_positive": True},
            passes=False, qualification=qualification)
        self.assertTrue(verify_gate_envelope(envelope))
        tampered = copy.deepcopy(envelope)
        tampered["qualification"]["candidate_observations"][0]["net_pnl"] = 99.0
        # Re-signing the outer envelope does not make source evidence valid.
        from research.gates import _content_hash
        tampered["content_hash"] = _content_hash(
            {key: value for key, value in tampered.items()
             if key != "content_hash"})
        self.assertFalse(verify_gate_envelope(tampered))

    def test_non_passing_source_tampering_fails_after_resigning(self):
        """Diagnostic v2 envelopes remain bound to their source rows.

        A failed/underpowered decision is still audit evidence.  Recomputing
        only the outer content hash must not make a changed held-out outcome
        verify successfully just because the envelope was not authorizing.
        """
        candidate = _rows(2, net=1.0)
        baseline = _rows(2, net=0.0, prefix="baseline")
        control = matched_cluster_test(candidate, baseline, vehicle="equity")
        envelope = verified_gate_envelope(
            lane="shadow", vehicle="equity", fit=[], heldout=candidate,
            heldout_baseline=baseline,
            fit_floor=structural_floor(
                [], vehicle="equity", min_trades=0, min_sessions=0,
                required=False),
            heldout_floor=structural_floor(
                candidate, vehicle="equity", min_trades=1, min_sessions=1),
            control=control, p_value=control["p_value"], q_value=1.0,
            alpha=.05, falsification={"passes": False},
            separation={"passes": True},
            checks={"falsification": False}, passes=False)
        self.assertFalse(envelope["passes"])
        self.assertTrue(verify_gate_envelope(envelope))

        tampered = copy.deepcopy(envelope)
        tampered["heldout_source"][1]["net_pnl"] = 99.0
        from research.gates import _content_hash
        tampered["content_hash"] = _content_hash({
            key: value for key, value in tampered.items()
            if key != "content_hash"})
        self.assertFalse(verify_gate_envelope(tampered))

    def test_family_fdr_can_pass_while_global_fdr_fails(self):
        # This is the ordinary BH case: one p=.01 among six tests has global
        # q=.06, while its two-test family has q=.02.
        from research.stats import benjamini_hochberg
        family = benjamini_hochberg({"target": .01, "sibling": .9}, alpha=.05)
        global_ = benjamini_hochberg(
            {"target": .01, "sibling": .9, "other-a": .9,
             "other-b": .9, "other-c": .9, "other-d": .9}, alpha=.05)
        self.assertTrue(family["target"]["significant"])
        self.assertFalse(global_["target"]["significant"])

    def test_family_fdr_rejects_nonfinite_or_out_of_range_probabilities(self):
        from research.stats import benjamini_hochberg
        for value in (float("nan"), float("inf"), -0.01, 1.01, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                benjamini_hochberg({"bad": value, "ok": .5})
        with self.assertRaises(ValueError):
            benjamini_hochberg({"ok": .5}, alpha=float("nan"))

    def test_dependence_safe_fdr_is_stricter_than_bh(self):
        from research.stats import benjamini_hochberg, benjamini_yekutieli
        values = {"target": .02, "sibling": .9}
        bh = benjamini_hochberg(values, alpha=.05)
        by = benjamini_yekutieli(values, alpha=.05)
        self.assertTrue(bh["target"]["significant"])
        self.assertFalse(by["target"]["significant"])
        self.assertGreater(by["target"]["p_adjusted"],
                           bh["target"]["p_adjusted"])

    def test_matched_pairs_feed_block_bootstrap_in_market_chronology(self):
        from research.gates import matched_pairs
        candidate = []
        baseline = []
        # Match-key sorting would put symbol A's later sessions before symbol
        # B's earlier sessions.  The statistical order must instead be time.
        for symbol, days, pnl in (("A", range(6, 11), 10.0),
                                  ("B", range(1, 6), -10.0)):
            for day in days:
                row = self._equity_quote_row(
                    symbol=symbol, session_date=f"2026-01-{day:02d}",
                    opportunity_id=f"{symbol}-{day}", net_pnl=pnl)
                candidate.append(row)
                baseline.append({**row, "net_pnl": 0.0})
        pairs = matched_pairs(candidate, baseline, vehicle="equity")
        self.assertEqual(pairs["deltas"], [-10.0] * 5 + [10.0] * 5)
        self.assertEqual(pairs["timestamps"], sorted(pairs["timestamps"]))

    def test_naive_legacy_pair_timestamp_is_host_timezone_independent(self):
        from datetime import datetime, timezone
        from research.gates import matched_pairs
        row = self._equity_quote_row(
            symbol="SPY", session_date="2026-01-01",
            opportunity_id="naive", net_pnl=1.0)
        row["entry_timestamp"] = "2026-01-01T09:30:00"
        pairs = matched_pairs([row], [{**row, "net_pnl": 0.0}],
                              vehicle="equity")
        self.assertEqual(
            pairs["timestamps"],
            [datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc).timestamp()],
        )

    def test_actual_baseline_adequacy_fails_closed_for_thirty_vs_five(self):
        candidate = _rows(30, net=1.0)
        baseline = [dict(candidate[index], net_pnl=0.0)
                    for index in range(5)]
        report = paired_control_adequacy(
            candidate, baseline, vehicle="equity")
        self.assertEqual(report["matched"], 5)
        self.assertLess(report["coverage"], .8)
        self.assertFalse(report["adequate"])
        self.assertFalse(matched_cluster_test(
            candidate, baseline, vehicle="equity", iterations=10)["adequate"])

    def test_extra_baseline_trades_do_not_reduce_complete_candidate_coverage(self):
        candidate = _rows(30, net=1.0)
        baseline = [dict(row, net_pnl=0.0) for row in candidate]
        baseline.extend(_rows(
            10, net=0.0, symbol="QQQ", prefix="baseline-only"))
        report = paired_control_adequacy(
            candidate, baseline, vehicle="equity")
        self.assertEqual(report["matched"], 30)
        self.assertEqual(report["candidate_count"], 30)
        self.assertEqual(report["control_count"], 40)
        self.assertEqual(report["coverage"], 1.0)
        self.assertTrue(report["adequate"])

    def test_no_trade_rows_cannot_inflate_paired_coverage(self):
        candidate = _rows(30, net=1.0)
        baseline = [dict(row, net_pnl=0.0) for row in candidate]
        candidate[0]["no_trade"] = True
        report = paired_control_adequacy(
            candidate, baseline, vehicle="equity")
        self.assertEqual(report["matched"], 29)
        self.assertEqual(report["candidate_count"], 29)
        self.assertLessEqual(report["coverage"], 1.0)
        self.assertFalse(report["adequate"])

    def test_drawdown_uses_realized_chronology_and_intraday_marks(self):
        rows = [
            {"symbol": "B", "exit_timestamp": "2026-01-03T00:00:00+00:00",
             "net_pnl": -50.0},
            {"symbol": "A", "exit_timestamp": "2026-01-01T00:00:00+00:00",
             "net_pnl": 100.0},
            {"symbol": "C", "exit_timestamp": "2026-01-02T00:00:00+00:00",
             "net_pnl": -10.0},
        ]
        self.assertEqual(max_drawdown_of(rows), 60.0)
        marks = [
            {"timestamp": "2026-01-01T00:00:00+00:00", "account_equity": 100.0},
            {"timestamp": "2026-01-01T01:00:00+00:00", "account_equity": 80.0},
            {"timestamp": "2026-01-01T02:00:00+00:00", "account_equity": 95.0},
        ]
        self.assertEqual(max_drawdown_of(marks), 20.0)

        untimestamped = [
            {"opportunity_id": "z", "net_pnl": 100.0},
            {"opportunity_id": "a", "net_pnl": -50.0},
            {"opportunity_id": "y", "net_pnl": 100.0},
            {"opportunity_id": "b", "net_pnl": -120.0},
        ]
        self.assertEqual(max_drawdown_of(untimestamped), 120.0)

    def test_falsification_requires_independent_provenance_when_requested(self):
        rejected = falsification_gate(
            [2.0, 2.0], [-1.0, -.5, .5, 1.0],
            preregistered_p_value=.001, require_independent=True)
        self.assertFalse(rejected["passes"])
        accepted = falsification_gate(
            [2.0, 2.0], [-1.0, -.5, .5, 1.0],
            preregistered_p_value=.001,
            independent_p_value=.01,
            independent_method="independent_empirical_null_tail",
            independent_result_hash="fixture",
            require_independent=True)
        self.assertTrue(accepted["independent_supplied"])
        self.assertEqual(accepted["p_value_source"],
                         "heldout_paired_cluster_sign_flip")
        self.assertEqual(accepted["p_value"], .001)
        # A second Monte Carlo seed over the same held-out deltas is an audit
        # replication, not a second independent market experiment.  Its tail
        # estimate therefore cannot create an accidental double-significance
        # hurdle for the preregistered paired test.
        noisy_replication = falsification_gate(
            [2.0, 2.0], [-1.0, -.5, .5, 1.0],
            preregistered_p_value=.001,
            independent_p_value=.99,
            independent_method="independent_empirical_null_tail",
            independent_result_hash="different-seed",
            require_independent=True)
        self.assertTrue(noisy_replication["passes"])
        self.assertEqual(noisy_replication["p_value"], .001)

    def test_falsification_rejects_nonfinite_samples(self):
        for observed, placebo in (([float("inf")], [0.0]),
                                  ([float("nan")], [0.0]),
                                  ([1.0], [float("-inf")]),
                                  ([True], [0.0])):
            with self.subTest(observed=observed, placebo=placebo), \
                    self.assertRaises(ValueError):
                falsification_gate(
                    observed, placebo, preregistered_p_value=.001)


if __name__ == "__main__":
    unittest.main()
