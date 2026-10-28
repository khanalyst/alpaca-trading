"""Execution calibration that can veto: like-for-like entry costs, fresh evidence.

Review findings D1-06 (entries were scored against the limit price itself,
so equity entry costs could never veto, and unfilled orders were invisible)
and D1-12 (stale calibration was said to veto, but nothing measured age).
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
import unittest
from pathlib import Path

from research.calibration import calibrate
from research.costs import CostModel
from research.entry_calibration import (
    entry_calibration_from_path, score_entries)

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
MODEL = CostModel(spread_bps=4.0, slippage_bps=6.0, fee_bps=0.5)


def _limit_fills(count=24, *, age_days=1.0, half_spread_bps=10.0, **extra):
    """Buy entries filled exactly at their limit, the IEX ask.

    The mid is ``half_spread_bps`` below the ask, as the runtime would have
    seen it at decision time.
    """
    rows = []
    for index in range(count):
        ask = 100.0
        mid = ask / (1.0 + half_spread_bps / 10_000.0)
        rows.append({
            "ts": (NOW - timedelta(days=age_days, minutes=index)).timestamp(),
            "price": ask, "qty": 10.0, "action": "open", "side": "buy",
            "reference_price": ask, "order_id": f"order-{index}",
            "vehicle": "equity", "execution_profile": "shares",
            "runtime_mode": "paper", "decision_mid": mid,
            "signal_close": mid * (1.0 - 2.0 / 10_000.0), **extra})
    return rows


class MidAnchoredEntryCostTests(unittest.TestCase):
    """D1-06."""

    def test_limit_referenced_calibration_cannot_see_the_spread(self):
        rows = _limit_fills()
        # The existing check scores the fill against the limit: 0 bps
        # against an 8 bps model, so it authorizes (the defect).
        self.assertEqual(calibrate(rows, MODEL)["authorization_verdict"],
                         "authorized")
        report = score_entries(rows, (), MODEL)
        self.assertAlmostEqual(report["cost_versus_decision_mid"]["mean_bps"],
                               10.0, places=6)
        self.assertAlmostEqual(report["cost_versus_signal_close"]["mean_bps"],
                               12.0, places=2)
        self.assertEqual(report["cost_versus_limit_reference"]["mean_bps"], 0.0)
        self.assertEqual(report["verdict"], "optimistic")
        self.assertEqual(report["authorization_verdict"], "veto_optimistic_cost")
        self.assertEqual(report["authorization_exit_code"], 2)

    def test_a_narrow_spread_inside_the_model_is_authorized(self):
        report = score_entries(_limit_fills(half_spread_bps=1.0), (), MODEL)
        self.assertEqual(report["verdict"], "conservative")
        self.assertEqual(report["authorization_verdict"], "authorized")

    def test_without_a_decision_mid_the_report_fails_closed(self):
        rows = [{key: value for key, value in row.items()
                 if key not in {"decision_mid", "signal_close"}}
                for row in _limit_fills()]
        report = score_entries(rows, (), MODEL)
        self.assertEqual(report["authorization_verdict"], "insufficient_data")
        self.assertEqual(report["unscored_orders"], 24)
        self.assertEqual(report["missing_runtime_fields"],
                         ["decision_mid", "signal_close"])
        # Quote sides recorded at decision time are enough.
        sided = [{**row, "decision_bid": 99.98, "decision_ask": 100.0}
                 for row in rows]
        self.assertAlmostEqual(score_entries(sided, (), MODEL)[
            "cost_versus_decision_mid"]["mean_bps"], 1.0, places=3)

    def test_unfilled_entry_orders_are_missed_fills(self):
        rows = _limit_fills(half_spread_bps=1.0)
        missed = [{"order_id": f"missed-{index}", "action": "open", "side": "buy",
                   "status": "expired", "filled_qty": 0, "vehicle": "equity",
                   "decision_mid": 100.0,
                   "expiry_mid": 100.0 * (1.0 + 15.0 / 10_000.0) if index % 2 else None}
                  for index in range(8)]
        filled = [{"order_id": row["order_id"], "action": "open", "side": "buy",
                   "status": "filled", "filled_qty": 10.0, "vehicle": "equity"}
                  for row in rows]
        report = score_entries(rows, missed + filled, MODEL)
        self.assertEqual(report["missed_orders"], 8)
        self.assertAlmostEqual(report["missed_fill_rate"], 8 / 32)
        self.assertTrue(report["missed_fill_veto"])
        self.assertEqual(report["authorization_verdict"], "veto_missed_fills")
        opportunity = report["missed_opportunity_cost"]
        self.assertEqual(opportunity["orders"], 4)
        self.assertEqual(opportunity["unmeasured_orders"], 4)
        self.assertAlmostEqual(opportunity["mean_bps"], 15.0, places=6)
        self.assertIn("expiry_mid", report["missing_runtime_fields"])

    def test_reads_new_fields_from_a_runtime_journal(self):
        rows = _limit_fills()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ts REAL, "
                           "symbol TEXT, side TEXT, action TEXT, qty REAL, "
                           "price REAL, order_id TEXT, vehicle TEXT, "
                           "runtime_mode TEXT, reference_price REAL)")
                db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, ts REAL, "
                           "order_id TEXT, side TEXT, action TEXT, qty REAL, "
                           "status TEXT, filled_qty REAL, vehicle TEXT, "
                           "decision_mid REAL, signal_close REAL, expiry_mid REAL)")
                for row in rows:
                    db.execute("INSERT INTO orders (ts,order_id,side,action,qty,"
                               "status,filled_qty,vehicle,decision_mid,signal_close)"
                               " VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (row["ts"] - 1, row["order_id"], "buy", "open", 10,
                                "filled", 10, "equity", row["decision_mid"],
                                row["signal_close"]))
                    db.execute("INSERT INTO trades (ts,symbol,side,action,qty,price,"
                               "order_id,vehicle,runtime_mode,reference_price) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (row["ts"], "SPY", "buy", "open", 10, row["price"],
                                row["order_id"], "equity", "paper",
                                row["reference_price"]))
                db.execute("INSERT INTO orders (ts,order_id,side,action,qty,status,"
                           "filled_qty,vehicle,decision_mid,expiry_mid) VALUES "
                           "(?,?,?,?,?,?,?,?,?,?)",
                           (NOW.timestamp(), "never", "buy", "open", 10, "canceled",
                            0, "equity", 100.0, 99.9))
            report = entry_calibration_from_path(str(path), MODEL)
        self.assertEqual(report["filled_orders"], 24)
        self.assertAlmostEqual(report["cost_versus_decision_mid"]["mean_bps"],
                               10.0, places=6)
        self.assertEqual(report["missed_orders"], 1)
        self.assertAlmostEqual(report["missed_opportunity_cost"]["mean_bps"],
                               -10.0, places=6)


class CalibrationAgeTests(unittest.TestCase):
    """D1-12."""

    def test_old_evidence_vetoes_when_a_maximum_age_is_given(self):
        rows = _limit_fills(age_days=120)
        self.assertEqual(calibrate(rows, MODEL)["authorization_verdict"],
                         "authorized")
        report = calibrate(rows, MODEL, as_of=NOW, max_age_days=30)
        self.assertEqual(report["authorization_verdict"], "veto_stale_calibration")
        self.assertEqual(report["authorization_exit_code"], 2)
        self.assertTrue(report["stale_evidence"])
        self.assertEqual(report["recent_fills"], 0)
        self.assertAlmostEqual(report["newest_fill_age_days"], 120.0, places=3)

    def test_fresh_evidence_still_authorizes(self):
        report = calibrate(_limit_fills(age_days=2), MODEL, as_of=NOW,
                           max_age_days=30)
        self.assertEqual(report["authorization_verdict"], "authorized")
        self.assertFalse(report["stale_evidence"])
        self.assertEqual(report["recent_fills"], 24)

    def test_one_fresh_fill_does_not_revive_a_stale_journal(self):
        rows = _limit_fills(age_days=120)[:23] + _limit_fills(count=1, age_days=1)
        rows[-1]["order_id"] = "fresh"
        report = calibrate(rows, MODEL, as_of=NOW, max_age_days=30)
        self.assertEqual(report["recent_fills"], 1)
        self.assertEqual(report["authorization_verdict"], "veto_stale_calibration")

    def test_evidence_after_as_of_is_not_evidence_then(self):
        report = calibrate(_limit_fills(age_days=1), MODEL,
                           as_of=NOW - timedelta(days=5), max_age_days=30)
        self.assertEqual(report["fills_after_as_of"], 24)
        self.assertEqual(report["authorization_verdict"], "veto_stale_calibration")

    def test_as_of_needs_a_maximum_age(self):
        with self.assertRaises(ValueError):
            calibrate(_limit_fills(), MODEL, as_of=NOW)
        with self.assertRaises(ValueError):
            calibrate(_limit_fills(), MODEL, max_age_days=0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
