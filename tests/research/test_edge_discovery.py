from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research.edge_lab import DiscoveryError, EdgeLedger, discover


def _sessions(start: datetime, count: int) -> list[dict]:
    rows: list[dict] = []
    for offset in range(count):
        session = start + timedelta(days=offset)
        values = (
            (100, 101, 99, 100),    # one-minute opening range
            (100, 102, 99, 102),    # confirmed breakout
            (102, 103, 101, 102),   # next-bar entry
            (102, 107, 101, 106),   # 1.5R target, but not 2R
        )
        for minute, (open_, high, low, close) in enumerate(values):
            rows.append({
                "symbol": "SPY",
                "timestamp": (session + timedelta(minutes=minute)).isoformat(),
                "open": open_, "high": high, "low": low, "close": close,
                "volume": 1, "provider": "alpaca", "feed": "sip",
            })
    return rows


class EdgeDiscoveryLifecycleTests(unittest.TestCase):
    def test_paper_outcomes_demote_a_champion_that_breaks_the_rolling_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="earlier target", config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            for lane in ("backtest", "shadow"):
                ledger.append_run(
                    candidate_id, lane=lane, vehicle="equity",
                    fit=[{"session_date": "2024-01-02"}],
                    heldout=[{"session_date": "2024-01-03"}],
                    metrics={"gate": {"passes": True}, "confidence": .99,
                             "heldout_ci_low": .1, "max_drawdown": 1,
                             "heldout_trades": 100})
                if lane == "backtest":
                    ledger.transition(candidate_id, "backtest_passed",
                                      reason="backtest gates passed")
                else:
                    ledger.transition(candidate_id, "shadow",
                                      reason="shadow evidence started")
                    ledger.transition(candidate_id, "validated",
                                      reason="shadow gates passed")
            ledger.transition(candidate_id, "champion",
                              reason="best validated evidence")
            for index in range(20):
                outcome = ledger.ingest_paper_outcome(candidate_id, {
                    "vehicle": "equity", "opportunity_id": f"paper-{index}",
                    "session_date": f"2024-02-{index + 1:02d}",
                    "net_pnl": -1, "r_multiple": -.1,
                })
            self.assertEqual(outcome["status"], "demoted")

    def test_discovery_operational_error_is_distinct_from_insufficient_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options_only = root / "options.jsonl"
            options_only.write_text(
                json.dumps({"kind": "quote", "symbol": "SPY"}) + "\n",
                encoding="utf-8")
            result = subprocess.run([
                sys.executable, "research.py", "edge", "discover",
                "--data", str(options_only), "--db", str(root / "edge.sqlite3")],
                cwd=Path(__file__).resolve().parents[2], check=False,
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_forward_lifecycle_cannot_be_manually_advanced_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="earlier target", config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            with self.assertRaisesRegex(ValueError, "passing backtest evidence"):
                ledger.transition(candidate_id, "backtest_passed", reason="operator request")
            ledger.append_run(
                candidate_id, lane="backtest", vehicle="equity",
                fit=[{"session_date": "2024-01-02"}],
                heldout=[{"session_date": "2024-01-03"}],
                metrics={"gate": {"passes": True}})
            self.assertEqual(
                ledger.transition(candidate_id, "backtest_passed",
                                  reason="gates passed")["status"],
                "backtest_passed")
            with self.assertRaisesRegex(ValueError, "passing shadow evidence"):
                ledger.transition(candidate_id, "shadow", reason="operator request")

    def test_auto_requires_a_later_forward_tail_for_shadow(self):
        registry = {"variants": [
            {"variant_id": "ibr.baseline", "strategy_id": "ibr",
             "base_version": "v1", "overrides": {}, "vehicles": ["equity"],
             "hypothesis": "registered baseline"},
            {"variant_id": "ibr.target.1_5r", "strategy_id": "ibr",
             "base_version": "v1", "overrides": {"strategy.target_r": 1.5},
             "vehicles": ["equity"], "hypothesis": "earlier target"},
        ]}
        config = {"strategy": {"range_minutes": 1, "range_stop": True,
                                "target_r": 2}}
        first = _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20)
        later = _sessions(datetime(2024, 2, 1, 14, 30, tzinfo=timezone.utc), 20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_one = root / "first.jsonl"
            data_all = root / "all.jsonl"
            variants = root / "variants.yaml"
            db = root / "edge.sqlite3"
            data_one.write_text("\n".join(json.dumps(row) for row in first), encoding="utf-8")
            data_all.write_text("\n".join(json.dumps(row) for row in first + later), encoding="utf-8")
            variants.write_text(json.dumps(registry), encoding="utf-8")

            initial = discover(data_one, db_path=db, variants_path=variants,
                               config=config, min_trades=5, min_sessions=5,
                               lane="auto")
            candidate = initial["variants"][0]
            self.assertEqual(candidate["status"], "backtest_passed")
            self.assertIsNone(candidate["shadow_run_id"])

            with self.assertRaises(DiscoveryError):
                discover(data_one, db_path=db, variants_path=variants,
                         config=config, min_trades=5, min_sessions=5,
                         lane="shadow")

            forward = discover(data_all, db_path=db, variants_path=variants,
                               config=config, min_trades=5, min_sessions=5,
                               lane="auto")
            candidate = forward["variants"][0]
            self.assertIn(candidate["status"], {"validated", "champion"})
            self.assertEqual(candidate["mode"], "shadow")
            self.assertEqual(candidate["unseen_sessions"], 20)
            trades = EdgeLedger(db).trades(candidate["candidate_id"], lane="shadow")
            self.assertTrue(trades)
            self.assertTrue(all(row["session_date"] >= "2024-02-01" for row in trades))


if __name__ == "__main__":
    unittest.main()
