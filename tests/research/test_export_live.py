"""Cross-strategy shadow evidence keeps schemas and outcome models separate."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research.export_live import read_journal, resolve


class JournalSchemaTests(unittest.TestCase):
    def test_parameter_variants_are_not_read_as_strategy_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
                    "strategy_id TEXT, strategy_version TEXT, variant_id TEXT)")
                conn.execute(
                    "CREATE TABLE trades (ts REAL, symbol TEXT, side TEXT, "
                    "action TEXT, strategy_id TEXT, setup_type TEXT, "
                    "pnl_pct REAL, realized_pnl_usd REAL)")
                rows = [
                    (1, "strategy_shadow_decision", json.dumps({
                        "symbol": "BTC", "direction": "long"}),
                     "momentum", "phase1-v3", None),
                    (2, "variant_shadow_decision", json.dumps({
                        "symbol": "BTC", "direction": "long"}),
                     None, None, "momentum.rr.2_5"),
                    (3, "shadow_decision", json.dumps({
                        "symbol": "ETH", "direction": "short"}),
                     "momentum", "phase1-v3", None),
                    (4, "shadow_decision", json.dumps({
                        "symbol": "ETH", "direction": "short"}),
                     None, None, "momentum.rr.3_0"),
                ]
                conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", rows)

            decisions, summaries, trades = read_journal(db)

        self.assertEqual(len(decisions), 2)
        self.assertEqual(set(decisions["strategy_id"]), {"momentum"})
        self.assertTrue(summaries.empty)
        self.assertTrue(trades.empty)


class OutcomeModelTests(unittest.TestCase):
    def test_unvalidated_strategy_is_not_scored_with_momentum_exits(self):
        decisions = pd.DataFrame([{
            "strategy_id": "funding-carry", "symbol": "BTC",
            "direction": "long", "signal_ts": 1,
        }])

        resolved = resolve(
            decisions, {}, costs=None, max_hold_bars=1,
            min_stop_atr=1.0, buffer_atr=0.1, reward_risk=2.0)

        self.assertTrue(resolved.empty)
        self.assertEqual(
            resolved.attrs["unresolved"],
            {"funding-carry: no validated forward outcome model": 1})


if __name__ == "__main__":
    unittest.main()
