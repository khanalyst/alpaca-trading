"""Cross-strategy shadow evidence keeps schemas and outcome models separate."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from research.edge_lab import Costs
from research.export_live import (
    BAR_MS,
    _registered_horizons,
    read_journal,
    resolve,
)


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
                     "momentum", "phase1-v3", "momentum.rr.3_0"),
                    (5, "shadow_decision", json.dumps({
                        "symbol": "SOL", "direction": "long"}),
                     "funding-unwind", "v1", "live"),
                ]
                conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", rows)

            decisions, summaries, trades = read_journal(db)

        self.assertEqual(len(decisions), 3)
        self.assertEqual(
            set(decisions["strategy_id"]), {"momentum", "funding-unwind"})
        self.assertTrue(
            decisions["inference_exclusion_reason"].notna().all())
        self.assertTrue(summaries.empty)
        self.assertTrue(trades.empty)


class OutcomeModelTests(unittest.TestCase):
    @staticmethod
    def frame(bars: int):
        ts = np.arange(bars, dtype=np.int64) * BAR_MS
        prices = np.full(bars, 100.0)
        return SimpleNamespace(
            symbol="BTC",
            ts=ts,
            df=pd.DataFrame({
                "open": prices, "high": prices,
                "low": prices, "close": prices,
            }),
        )

    @staticmethod
    def decision(strategy_id: str, signal_ts: int = 10 * BAR_MS) -> dict:
        return {
            "strategy_id": strategy_id,
            "symbol": "BTC", "direction": "long",
            "signal_ts": signal_ts, "atr_1h_pct": 1.0,
            "swing_low_pct": 1.0, "swing_high_pct": 1.0,
        }

    @staticmethod
    def costs() -> Costs:
        return Costs("test", fee_per_side=0.0, entry_slippage=0.0,
                     exit_slippage=0.0, stop_slippage=0.0,
                     include_funding=False)

    def test_validated_strategy_still_refuses_absent_bar_data(self):
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
            {"BTC: no bar data": 1})

    def test_each_strategy_uses_its_registered_horizon(self):
        decisions = pd.DataFrame([
            self.decision("flush-fade"),
            self.decision("momentum"),
        ])

        resolved = resolve(
            decisions, {"BTC": self.frame(120)}, self.costs(),
            max_hold_bars=None, min_stop_atr=1.0,
            buffer_atr=0.1, reward_risk=2.0)

        self.assertEqual(list(resolved["strategy_id"]), ["flush-fade"])
        self.assertEqual(int(resolved.iloc[0]["bars_held"]), 96)
        self.assertEqual(sum(resolved.attrs["unresolved"].values()), 1)
        self.assertIn(
            "momentum/BTC: incomplete 192-bar outcome window",
            resolved.attrs["unresolved"])

    def test_truncated_window_is_unresolved_not_a_max_hold_exit(self):
        resolved = resolve(
            pd.DataFrame([self.decision("momentum")]),
            {"BTC": self.frame(50)}, self.costs(),
            max_hold_bars=None, min_stop_atr=1.0,
            buffer_atr=0.1, reward_risk=2.0)

        self.assertTrue(resolved.empty)
        self.assertEqual(
            resolved.attrs["unresolved"],
            {"momentum/BTC: incomplete 192-bar outcome window": 1})

    def test_legacy_contract_rows_are_quarantined_before_resolution(self):
        decisions = pd.DataFrame([{
            **self.decision("momentum"),
            "inference_exclusion_reason": (
                "legacy evidence missing strategy contract provenance"),
        }])
        resolved = resolve(
            decisions, {"BTC": self.frame(240)}, self.costs(),
            max_hold_bars=1, min_stop_atr=1.0,
            buffer_atr=0.1, reward_risk=2.0)
        self.assertTrue(resolved.empty)
        self.assertEqual(
            resolved.attrs["unresolved"],
            {"momentum: legacy evidence missing strategy contract provenance": 1})

    def test_stale_strategy_id_does_not_break_export_metadata(self):
        self.assertEqual(
            _registered_horizons(["momentum", "retired-strategy"]),
            {"momentum": 192})


if __name__ == "__main__":
    unittest.main()
