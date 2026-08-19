import copy
import pickle
import unittest

from research.gates import (
    AcceptanceFloor, SealedWindowError, chronological_split,
    deterministic_placebo_deltas, falsification_gate, matched_cluster_test,
    expectancy_rejection_report, paired_delta, performance_floor,
    placebo_null_distribution,
    qualification_report, recompute_gate_statistics, seal_final_window, structural_floor,
    risk_unit_report, verified_gate_envelope, verify_gate_envelope,
    walk_forward_report,
)


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
            "entry_feed": "sip", "exit_feed": "sip",
            "entry_provider": "alpaca", "exit_provider": "alpaca",
            "entry_quote_age_seconds": 0.0,
            "exit_quote_age_seconds": 0.0,
        }
        row.update(changes)
        return row

    def test_equity_fill_quality_requires_sip_quote_provenance_on_both_legs(self):
        from research.gates import fill_source_summary
        self.assertTrue(fill_source_summary(
            [self._equity_quote_row()], vehicle="equity")["adequate"])
        for changes in (
                {"entry_feed": "iex"}, {"exit_feed": "iex"},
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
            "entry_feed": "sip", "exit_feed": "sip",
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

    def test_placebo_draws_are_reproducible_from_persisted_content(self):
        candidate = _rows(9, net=4.0)
        baseline = _rows(9, net=0.0, prefix="root")
        control = matched_cluster_test(candidate, baseline, vehicle="equity")
        null = placebo_null_distribution(candidate, baseline, vehicle="equity")
        envelope = verified_gate_envelope(
            lane="backtest", vehicle="equity", fit=[], heldout=candidate,
            fit_floor={}, heldout_floor={},
            control=control, p_value=control["p_value"], q_value=.01, alpha=.05,
            falsification={"passes": True, "draws": null["draws"],
                           "seed": null["seed"], "p_value": null["p_value"]},
            separation={"passes": True}, checks={"falsification": True},
            passes=True)
        recomputed = recompute_gate_statistics(envelope)
        self.assertTrue(recomputed["available"])
        self.assertAlmostEqual(recomputed["p_value"], control["p_value"])
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


if __name__ == "__main__":
    unittest.main()
