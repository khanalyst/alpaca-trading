import copy
import pickle
import unittest

from research.gates import (
    AcceptanceFloor, SealedWindowError, chronological_split,
    deterministic_placebo_deltas, falsification_gate, matched_cluster_test,
    paired_delta, performance_floor, placebo_null_distribution,
    qualification_report, recompute_gate_statistics, seal_final_window, structural_floor,
    verified_gate_envelope, verify_gate_envelope, walk_forward_report,
)


def _rows(count, *, net, start=2, symbol="SPY", prefix="candidate"):
    return [{"vehicle": "equity", "symbol": symbol,
             "session_date": f"2024-01-{day:02d}",
             "opportunity_id": f"{prefix}-{day}",
             "net_pnl": float(net(day) if callable(net) else net)}
            for day in range(start, start + count)]


class EvidenceGateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
