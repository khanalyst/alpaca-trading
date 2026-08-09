import unittest

from research.gates import (
    AcceptanceFloor, chronological_split, deterministic_placebo_deltas,
    falsification_gate, matched_cluster_test, paired_delta, structural_floor,
    verified_gate_envelope, verify_gate_envelope,
)


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
        candidate = [
            {"vehicle": "equity", "symbol": "SPY", "session_date": f"2024-01-0{day}",
             "opportunity_id": f"candidate-{day}", "net_pnl": float(day)}
            for day in (2, 3, 4, 5)
        ]
        baseline = [
            {"vehicle": "equity", "symbol": "SPY", "session_date": f"2024-01-0{day}",
             "opportunity_id": f"root-{day}", "net_pnl": 0.0}
            for day in (2, 3, 4, 5)
        ]
        first = matched_cluster_test(candidate, baseline, vehicle="equity")
        second = matched_cluster_test(candidate, baseline, vehicle="equity")
        self.assertEqual(first, second)
        self.assertEqual(first["matched"], 4)
        placebo = deterministic_placebo_deltas(candidate, baseline, vehicle="equity")
        self.assertEqual(placebo, deterministic_placebo_deltas(
            candidate, baseline, vehicle="equity"))
        self.assertFalse(falsification_gate([1.0, 2.0], [0.0, 0.0])["passes"])
        self.assertTrue(falsification_gate(
            placebo["observed"], placebo["placebo"])["passes"])

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
        self.assertTrue(verify_gate_envelope(envelope))
        envelope["passes"] = False
        self.assertFalse(verify_gate_envelope(envelope))


if __name__ == "__main__":
    unittest.main()
