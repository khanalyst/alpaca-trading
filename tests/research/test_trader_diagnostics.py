"""Focused contract tests for the pure trader diagnostics report."""

import json
import math
import unittest

from research.trader_diagnostics import (
    build_trader_diagnostics,
    validate_trader_diagnostics,
)


def _row(session="2026-01-02", *, pnl=1.0, opportunity=None, **extra):
    row = {
        "session_date": session,
        "symbol": "SPY",
        "direction": "long",
        "entry_timestamp": f"{session}T14:30:00+00:00",
        "exit_timestamp": f"{session}T14:31:00+00:00",
        "canonical_exit_reason": "target",
        "gross_pnl": pnl + 0.1,
        "costs": 0.1,
        "net_pnl": pnl,
        "no_trade": False,
    }
    if opportunity is not None:
        row["opportunity_id"] = opportunity
    row.update(extra)
    return row


class TraderDiagnosticsTests(unittest.TestCase):
    def test_canonical_reason_is_authoritative_and_discontinuity_detail_survives(self):
        rows = [
            _row(canonical_exit_reason="data_discontinuity",
                 exit_reason="time", hold_discontinuity_kind="internal_gap"),
            _row(canonical_exit_reason="unknown", exit_reason="time", pnl=-2.0),
            _row(canonical_exit_reason=None, exit_reason="discontinuity", pnl=0.5),
        ]
        report = build_trader_diagnostics({"arm": rows})
        arm = report["arms"]["arm"]

        self.assertEqual(arm["canonical_exit_counts"], {
            "data_discontinuity": 2, "unknown": 1})
        self.assertEqual(arm["data_discontinuity_details"]["internal_gap"], 1)
        self.assertEqual(arm["discontinuity_count"], 2)
        self.assertEqual(arm["right_censored_count"], 2)
        self.assertEqual(arm["canonical_exit_counts"].get("max_hold"), None)

    def test_censored_rows_remain_in_trade_totals_and_pnl(self):
        rows = [
            _row(pnl=2.0),
            _row(session="2026-01-03", pnl=-3.0,
                 canonical_exit_reason="max_hold", right_censored=True,
                 censor_reason="deadline_not_observed"),
            {"no_trade": True, "session_date": "2026-01-04",
             "reject_reason": "missing_quote", "net_pnl": -999.0},
        ]
        arm = build_trader_diagnostics({"arm": rows})["arms"]["arm"]

        self.assertEqual(arm["total_rows"], 3)
        self.assertEqual(arm["executed_trades"], 2)
        self.assertEqual(arm["right_censored_count"], 1)
        self.assertEqual(arm["discontinuity_count"], 0)
        self.assertEqual(arm["net_pnl"], -1.0)
        self.assertEqual(arm["coverage_issues"]["counts"]["deadline_not_observed"], 1)
        self.assertEqual(arm["coverage_issues"]["counts"]["missing_quote"], 1)

    def test_duplicate_opportunities_are_reported_as_overlap_not_portfolio(self):
        rows = {
            "a": [_row(opportunity="same", pnl=1.0,
                        session_complete=True, session_eligible=True),
                   _row(opportunity="a-only", session_complete=True,
                        session_eligible=True)],
            "b": [_row(opportunity="same", pnl=2.0,
                        session_complete=True, session_eligible=True),
                   _row(opportunity="b-only", session_complete=True,
                        session_eligible=True)],
        }
        report = build_trader_diagnostics(rows)

        self.assertEqual(report["opportunity_overlap"], {
            "unique_opportunities": 3,
            "overlapping_opportunities": 1,
            "overlapping_variant_rows": 2,
            "overlap_is_not_portfolio": True,
        })
        self.assertEqual(report["aggregate"]["net_pnl_overlapping_not_portfolio"], 5.0)
        self.assertIsNone(report["portfolio_pnl"])
        self.assertEqual(report["aggregate"]["interpretation"], "overlapping_not_portfolio")

    def test_no_signal_rows_stay_in_coverage_but_are_not_opportunities(self):
        blocked = {
            "no_trade": True,
            "execution_disposition": "no_signal",
            "reject_reason": "no_signal",
            "opportunity_id": "blocked-shared",
            "session_date": "2026-01-02",
        }
        legacy_blocked = {**blocked, "no_trade": False,
                          "opportunity_id": "blocked-legacy"}
        refused_signal = {**blocked, "opportunity_id": "signal-refused",
                          "reject_reason": "stressed_cost",
                          "signal_opportunity": True}
        report = build_trader_diagnostics({
            "a": [blocked, legacy_blocked, refused_signal],
            "b": [dict(blocked), dict(legacy_blocked), dict(refused_signal)],
        })

        self.assertEqual(report["opportunity_overlap"], {
            "unique_opportunities": 1,
            "overlapping_opportunities": 1,
            "overlapping_variant_rows": 2,
            "overlap_is_not_portfolio": True,
        })
        self.assertEqual(report["arms"]["a"]["coverage_issues"]["counts"],
                         {"no_signal": 2, "stressed_cost": 1})
        self.assertEqual(report["arms"]["a"]["executed_trades"], 0)
        self.assertEqual(report["arms"]["a"]["unique_opportunities"], 1)

    def test_shared_session_correlation_never_fills_missing_sessions_with_zero(self):
        left = [_row(f"2026-01-{day:02d}", pnl=float(day),
                     session_complete=True, session_eligible=True)
                for day in range(2, 6)]
        right = [_row(f"2026-01-{day:02d}", pnl=float(day) * 2,
                      session_complete=True, session_eligible=True)
                 for day in (2, 3, 5)]
        report = build_trader_diagnostics({"left": left, "right": right},
                                          min_shared_sessions=3)
        pair = report["cross_arm_dependence"]["pairs"][0]

        self.assertEqual(pair["shared_complete_eligible_sessions"], 3)
        self.assertNotIn("2026-01-04", pair["shared_sessions"])
        self.assertEqual(pair["status"], "measured")
        self.assertAlmostEqual(pair["correlation"], 1.0)

    def test_explicit_zero_session_is_measured_but_missing_session_is_not(self):
        left = [_row(f"2026-01-{day:02d}", pnl=float(day - 2),
                     session_complete=True, session_eligible=True)
                for day in range(2, 6)]
        right = [_row(f"2026-01-{day:02d}", pnl=0.0,
                      session_complete=True, session_eligible=True)
                 for day in range(2, 6)]
        report = build_trader_diagnostics({"left": left, "right": right},
                                          min_shared_sessions=4)
        pair = report["cross_arm_dependence"]["pairs"][0]

        self.assertEqual(pair["shared_complete_eligible_sessions"], 4)
        self.assertIn("2026-01-04", pair["shared_sessions"])
        self.assertIsNone(pair["correlation"])
        self.assertEqual(pair["status"], "zero_variance")

    def test_unknown_thirty_session_coverage_is_unmeasured(self):
        left = [_row(f"2026-02-{day:02d}", pnl=float(day))
                for day in range(1, 31)]
        right = [_row(f"2026-02-{day:02d}", pnl=float(day) * 2)
                 for day in range(1, 31)]
        report = build_trader_diagnostics({"left": left, "right": right})
        pair = report["cross_arm_dependence"]["pairs"][0]

        self.assertEqual(pair["shared_complete_eligible_sessions"], 0)
        self.assertEqual(pair["status"], "insufficient_shared_sessions")
        self.assertFalse(pair["measured"])
        self.assertIsNone(pair["correlation"])

    def test_insufficient_pairs_are_labeled_unmeasured_and_nonfinite_metrics_are_safe(self):
        rows = [_row(net_pnl=float("nan"), gross_pnl=float("inf"), costs=float("nan"))]
        report = build_trader_diagnostics({"a": rows, "b": [_row()]})
        pair = report["cross_arm_dependence"]["pairs"][0]

        self.assertEqual(pair["status"], "insufficient_shared_sessions")
        self.assertFalse(pair["measured"])
        self.assertIsNone(pair["correlation"])
        self.assertEqual(report["arms"]["a"]["invalid_metric_counts"], {
            "fees": 1, "gross_pnl": 1, "net_pnl": 1})
        encoded = json.dumps(report, allow_nan=False)
        self.assertTrue(encoded)
        self.assertTrue(validate_trader_diagnostics(report))
        self.assertFalse(any(math.isnan(value) for value in report["arms"]["a"].values()
                             if isinstance(value, float)))

    def test_holding_time_summary_requires_valid_timestamps(self):
        rows = [
            _row(entry_timestamp="2026-01-02T14:30:00+00:00",
                 exit_timestamp="2026-01-02T14:32:00+00:00"),
            _row(entry_timestamp="bad", exit_timestamp="also-bad"),
        ]
        summary = build_trader_diagnostics({"arm": rows})["arms"]["arm"]["holding_time"]
        self.assertTrue(summary["available"])
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["mean_seconds"], 120.0)
        self.assertEqual(summary["invalid_timestamp_rows"], 1)


if __name__ == "__main__":
    unittest.main()
