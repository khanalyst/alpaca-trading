from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent import state
from agent.alpaca_domain import Order
from agent.order_timing import broker_timing
from research.calibration import _latest_orders, timing_report


class TimingTests(unittest.TestCase):
    def test_updated_at_is_not_a_fill_timestamp(self):
        order = Order("o", "SPY", Decimal(1), "buy", "canceled", "market", "day",
                      submitted_at=datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc),
                      updated_at=datetime(2026, 9, 8, 13, 31, tzinfo=timezone.utc))
        result = broker_timing(order)
        self.assertIsNotNone(result["broker_submitted_ts"])
        self.assertIsNone(result["broker_filled_ts"])

    def test_reconciliation_keeps_original_submission_timing(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE orders (order_id TEXT, ts REAL, status TEXT, decision_ts REAL, request_sent_ts REAL, submit_roundtrip_ms REAL, broker_filled_ts REAL)")
            db.execute("INSERT INTO orders VALUES ('o', 1,'new',10,10.2,30,NULL)")
            db.execute("INSERT INTO orders VALUES ('o', 2,'filled',NULL,NULL,NULL,11)")
            row = _latest_orders(db)["o"]
            self.assertEqual(row["decision_ts"], 10)
            self.assertEqual(row["broker_filled_ts"], 11)

    def test_invalid_broker_clock_is_reported_without_zero_latency(self):
        report = timing_report([{"decision_ts": None, "request_sent_ts": None,
            "broker_submitted_ts": 100, "broker_filled_ts": 99,
            "submit_roundtrip_ms": None, "qty": 10, "reference_price": 100,
            "runtime_mode": "paper", "vehicle": "equity", "symbol": "SPY", "adverse_bps": 1}])
        self.assertEqual(report["invalid_clock_intervals"], 1)
        self.assertIsNone(report["metrics"]["broker_submit_to_fill_ms"]["median_ms"])


if __name__ == "__main__":
    unittest.main()
