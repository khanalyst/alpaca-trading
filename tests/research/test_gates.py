import unittest

from research.gates import AcceptanceFloor, paired_delta


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


if __name__ == "__main__":
    unittest.main()
