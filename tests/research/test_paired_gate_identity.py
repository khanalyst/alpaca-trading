import unittest

from research.edge_discovery_core import _discover_gate
from research.gates import matched_pairs, paired_delta


class PairedGateIdentityTests(unittest.TestCase):
    @staticmethod
    def _row(*, symbol="SPY", session="2026-01-02", opportunity_id="row",
             net_pnl=0.0, comparison_id=None, vehicle="equity", **changes):
        row = {
            "vehicle": vehicle,
            "symbol": symbol,
            "session_date": session,
            "opportunity_id": opportunity_id,
            "net_pnl": net_pnl,
        }
        if comparison_id is not None:
            row["comparison_id"] = comparison_id
        row.update(changes)
        return row

    @classmethod
    def _quote_row(cls, **changes):
        values = {
            "entry_fill_source": "quote", "exit_fill_source": "quote",
            "entry_feed": "iex", "exit_feed": "iex",
            "entry_provider": "fixture", "exit_provider": "fixture",
            "entry_quote_age_seconds": 0.0,
            "exit_quote_age_seconds": 0.0,
        }
        values.update(changes)
        return cls._row(**values)

    def test_factory_rule_specific_opportunity_ids_match_by_evidence_key(self):
        candidate = [
            self._row(
                session=f"2026-01-{index + 1:02d}",
                opportunity_id=f"factory_core1320:child-rule:{index}",
                net_pnl=2.0, r_multiple=0.5)
            for index in range(30)
        ]
        baseline = [
            {**row,
             "opportunity_id": f"factory_core1320:root-rule:{index}",
             "net_pnl": 0.5, "r_multiple": -0.25}
            for index, row in enumerate(candidate)
        ]

        report = paired_delta(candidate, baseline, vehicle="equity")

        self.assertEqual(report["matched"], 30)
        self.assertEqual(report["deltas"], [1.5] * 30)
        self.assertEqual(report["mean_delta"], 1.5)
        self.assertEqual(report["r_matched"], 30)
        self.assertEqual(report["r_deltas"], [0.75] * 30)
        self.assertEqual(report["mean_r_delta"], 0.75)
        self.assertEqual(
            report["matched"],
            matched_pairs(candidate, baseline, vehicle="equity")["matched"],
        )

    def test_explicit_comparison_id_takes_precedence(self):
        candidate = [self._row(
            symbol="SPY", session="2026-01-02", opportunity_id="candidate",
            comparison_id="sealed-event", net_pnl=2.0)]
        baseline = [self._row(
            symbol="QQQ", session="2026-02-03", opportunity_id="baseline",
            comparison_id="sealed-event", net_pnl=0.5)]
        self.assertEqual(
            paired_delta(candidate, baseline, vehicle="equity")["deltas"],
            [1.5],
        )

        baseline[0]["comparison_id"] = "different-event"
        baseline[0]["symbol"] = "SPY"
        baseline[0]["session_date"] = "2026-01-02"
        self.assertEqual(
            paired_delta(candidate, baseline, vehicle="equity")["matched"],
            0,
        )

    def test_duplicate_comparison_keys_fail_closed_on_either_arm(self):
        unique_candidate = self._row(
            opportunity_id="candidate", comparison_id="event", net_pnl=1.0)
        unique_baseline = self._row(
            opportunity_id="baseline", comparison_id="event", net_pnl=0.0)
        duplicate_candidate = [
            unique_candidate,
            {**unique_candidate, "opportunity_id": "candidate-copy"},
        ]
        duplicate_baseline = [
            unique_baseline,
            {**unique_baseline, "opportunity_id": "baseline-copy"},
        ]

        self.assertEqual(paired_delta(
            duplicate_candidate, [unique_baseline],
            vehicle="equity")["matched"], 0)
        self.assertEqual(paired_delta(
            [unique_candidate], duplicate_baseline,
            vehicle="equity")["matched"], 0)

    def test_unmatched_rows_are_not_pooled(self):
        candidate = [
            self._row(session="2026-01-02", opportunity_id="candidate-match",
                      net_pnl=3.0),
            self._row(session="2026-01-03", opportunity_id="candidate-only",
                      net_pnl=10_000.0),
        ]
        baseline = [
            self._row(session="2026-01-02", opportunity_id="baseline-match",
                      net_pnl=1.0),
            self._row(session="2026-01-04", opportunity_id="baseline-only",
                      net_pnl=-10_000.0),
        ]

        report = paired_delta(candidate, baseline, vehicle="equity")

        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["deltas"], [2.0])
        self.assertEqual(report["mean_delta"], 2.0)

    def test_symbol_and_session_both_separate_pairs(self):
        candidate = [
            self._row(symbol="SPY", session="2026-01-02",
                      opportunity_id="shared", net_pnl=1.0),
            self._row(symbol="QQQ", session="2026-01-03",
                      opportunity_id="shared", net_pnl=1.0),
        ]
        baseline = [
            self._row(symbol="QQQ", session="2026-01-02",
                      opportunity_id="shared", net_pnl=0.0),
            self._row(symbol="SPY", session="2026-01-03",
                      opportunity_id="shared", net_pnl=0.0),
        ]

        self.assertEqual(
            paired_delta(candidate, baseline, vehicle="equity")["matched"],
            0,
        )

    def test_legacy_ibr_rows_fall_back_to_opportunity_id(self):
        candidate = [{
            "vehicle": "equity", "opportunity_id": "ibr:legacy:1",
            "entry_timestamp": "2026-01-02T14:30:00+00:00",
            "net_pnl": 4.0,
        }]
        baseline = [{
            "vehicle": "equity", "opportunity_id": "ibr:legacy:1",
            "entry_timestamp": "2026-01-02T14:31:00+00:00",
            "net_pnl": 1.5,
        }]

        self.assertEqual(
            paired_delta(candidate, baseline, vehicle="equity")["deltas"],
            [2.5],
        )

    def test_signed_pnl_is_subtracted_without_direction_reinterpretation(self):
        candidate = [
            self._row(session="2026-01-02", opportunity_id="long-child",
                      direction="long", net_pnl=2.0),
            self._row(session="2026-01-03", opportunity_id="short-child",
                      direction="short", net_pnl=-2.0),
        ]
        baseline = [
            self._row(session="2026-01-02", opportunity_id="long-root",
                      direction="long", net_pnl=-1.0),
            self._row(session="2026-01-03", opportunity_id="short-root",
                      direction="short", net_pnl=1.0),
        ]

        report = paired_delta(candidate, baseline, vehicle="equity")

        self.assertEqual(report["deltas"], [3.0, -3.0])
        self.assertEqual(report["mean_delta"], 0.0)

    def test_invalid_evidence_and_wrong_vehicle_are_filtered(self):
        candidate = [
            self._quote_row(session="2026-01-02", opportunity_id="valid-child",
                            net_pnl=2.0),
            self._quote_row(session="2026-01-03", opportunity_id="stale-child",
                            net_pnl=100.0, entry_quote_age_seconds=31.0),
            self._quote_row(session="2026-01-04", opportunity_id="option-child",
                            vehicle="option", entry_feed="opra", exit_feed="opra",
                            net_pnl=100.0),
        ]
        baseline = [
            self._quote_row(session="2026-01-02", opportunity_id="valid-root",
                            net_pnl=0.5),
            self._quote_row(session="2026-01-03", opportunity_id="stale-root",
                            net_pnl=0.0),
            self._quote_row(session="2026-01-04", opportunity_id="option-root",
                            vehicle="option", entry_feed="opra", exit_feed="opra",
                            net_pnl=0.0),
        ]

        report = paired_delta(
            candidate, baseline, vehicle="equity", equity_provider="fixture")

        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["deltas"], [1.5])

    def test_discover_gate_sees_factory_descendants_as_paired_without_authorizing(self):
        candidate = [
            self._quote_row(
                session=f"2026-01-{index + 1:02d}",
                opportunity_id=f"factory_core1320:child-rule:{index}",
                net_pnl=1.0)
            for index in range(30)
        ]
        baseline = [
            {**row,
             "opportunity_id": f"factory_core1320:root-rule:{index}",
             "net_pnl": 0.0}
            for index, row in enumerate(candidate)
        ]

        gate = _discover_gate(
            candidate, baseline, vehicle="equity", min_trades=1,
            min_sessions=1, alpha=0.05, shadow=True,
            null_rows=baseline, test_iterations=10,
            equity_provider="fixture")

        all_pairs = gate["paired_baseline"]
        self.assertEqual(all_pairs["matched"], 30)
        self.assertEqual(all_pairs["mean_delta"], 1.0)
        paired = gate["heldout_paired_baseline"]
        self.assertEqual(paired["matched"], 30)
        self.assertEqual(paired["mean_delta"], 1.0)
        self.assertTrue(paired["paired_adequacy"]["adequate"])
        self.assertFalse(gate["passes_without_family"])


if __name__ == "__main__":
    unittest.main()
