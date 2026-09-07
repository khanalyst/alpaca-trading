import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

import report
from deploy import dashboard


class EvidenceReportingTests(unittest.TestCase):
    def test_net_loser_is_not_a_win_even_without_a_risk_denominator(self):
        rows = [{"action": "close", "qty": 1, "gross_pnl": .05,
                 "net_pnl": -.05, "fees": .1, "variant_id": "edge"}]
        summary = report._summary(rows, [])
        self.assertEqual(summary["win_rate"], 0)
        self.assertAlmostEqual(summary["net_pnl_usd"], -.05)
        variant = dashboard._by_variant(rows)[0]
        self.assertEqual(variant["win_rate"], 0)
        self.assertIsNone(variant["total_r"])

    def test_retry_closes_share_entry_and_whole_risk_without_setup_id(self):
        rows = [{"action": "open", "order_id": "entry", "qty": 4, "risk_usd": 4},
                {"action": "open", "order_id": "entry", "qty": 6, "risk_usd": 6},
                {"action": "close", "trade_id": "close:entry:first", "qty": 4,
                 "requested_qty": 10, "net_pnl": -4, "risk_usd": 10},
                {"action": "close", "trade_id": "close:entry:retry", "qty": 6,
                 "requested_qty": 6, "net_pnl": 6, "risk_usd": 6}]
        self.assertEqual(report.closed_parent_trades(rows[:-1]), [])
        parents = report.closed_parent_trades(rows)
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0]["net"], 2)
        self.assertEqual(parents[0]["risk"], 10)
        self.assertAlmostEqual(parents[0]["r_multiple"], .2)

    def test_unknown_economics_stay_unknown(self):
        rows = [{"action": "close", "realized_pnl_usd": 1, "fees": .1,
                 "risk_usd": 2, "variant_id": "legacy"}]
        self.assertIsNone(report._summary(rows, [])["net_pnl_usd"])
        self.assertIsNone(dashboard._by_variant(rows)[0]["win_rate"])
        rows[0]["pnl_semantics"] = "broker_fill_pnl_minus_fees"
        self.assertAlmostEqual(report._summary(rows, [])["net_pnl_usd"], .9)
        self.assertEqual(report.closed_parent_trades([{"action": "partial_close"}]), [])

    def test_lifetime_rollup_is_not_limited_by_recent_fill_page(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ts REAL, action TEXT, qty REAL, net_pnl REAL, risk_usd REAL, variant_id TEXT)")
                db.executemany("INSERT INTO trades(ts,action,qty,net_pnl,risk_usd,variant_id) VALUES(?,'close',1,?,1,'edge')",
                               [(i, 1 if i else -1) for i in range(205)])
            result = dashboard._journal_view(path)
            self.assertEqual(len(result["trades"]), 200)
            self.assertEqual(result["lifetime"]["closed_trades"], 205)
            self.assertEqual(result["by_variant"][0]["net_pnl_usd"], 203)

    def test_direct_job_lease_and_progress_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "direct.json"
            path.write_text(json.dumps({"schema": "research-direct-status.v1",
                "status": "running", "lease_ts": 100, "updated_ts": 100,
                "scheduler_managed": None, "progress": {"phase": "evaluating", "updated_ts": 2}}))
            live = dashboard._direct_research_status(path, now=110)
            self.assertTrue(live["running"])
            self.assertIsNone(live["scheduler_managed"])
            self.assertEqual(live["progress"]["updated_ts"], 2)
            self.assertFalse(dashboard._direct_research_status(path, now=400)["running"])

    def test_charts_keep_runtime_mode_and_net_payoffs_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE equity(ts REAL, equity REAL, runtime_mode TEXT)")
                db.executemany("INSERT INTO equity VALUES(?,?,?)", [
                    (100, 1000, "paper"), (101, 9000, "live"), (102, 990, "paper")])
                db.execute("CREATE TABLE trades(id INTEGER PRIMARY KEY, ts REAL, action TEXT, net_pnl REAL, risk_usd REAL, runtime_mode TEXT)")
                db.executemany("INSERT INTO trades(ts,action,net_pnl,risk_usd,runtime_mode) VALUES(?,'close',?,1,?)", [
                    (100, -1, "paper"), (101, 500, "live"), (102, 2, "paper")])
            charts = dashboard._charts(path, "paper")
            self.assertEqual([p["value"] for p in charts["net_equity"]["points"]], [1000, 990])
            self.assertEqual([p["value"] for p in charts["drawdown"]["points"]], [0, -10])
            self.assertFalse(charts["net_equity"]["cash_flows_adjusted"])
            self.assertEqual(charts["payoff"]["values"], [-1, 2])
            self.assertFalse(charts["uncertainty"]["available"])
            self.assertTrue(dashboard._performance(path)["available"])

    def test_pinned_status_explains_authoritative_stops(self):
        result = dashboard._promotions({"strategy": {}}, Path("absent.db"))
        self.assertIn("authoritative drift", result["note"])
        self.assertNotIn("leave them in place", result["note"])

    def test_exposure_shows_shared_notional_without_inventing_option_delta(self):
        exposure = dashboard._portfolio_exposure([
            {"symbol": "SPY", "direction": "long", "qty": 10, "entry_price": 100},
            {"symbol": "QQQ", "direction": "short", "qty": 5, "entry_price": 100},
            {"symbol": "SPY260918C00100000", "underlying_symbol": "SPY",
             "vehicle": "option", "direction": "long", "qty": 1, "entry_price": 3}])
        self.assertEqual(exposure["groups"], [{"group": "US equity ETFs", "gross_usd": 1500,
                                             "net_usd": 500, "positions": 2}])
        self.assertEqual(exposure["unpriced_or_unmapped_positions"], 1)
        self.assertFalse(exposure["independent_bets_estimated"])


if __name__ == "__main__":
    unittest.main()
