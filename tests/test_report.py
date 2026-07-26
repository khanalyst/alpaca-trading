import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout

from report import (
    adjusted_equity_curve,
    match_round_trips,
    print_equity,
    print_per_strategy,
    print_rejections,
)


def event(**overrides):
    base = {
        "ts": 0, "symbol": "BTC/USDT:USDT", "action": None,
        "trade_id": None, "notional": None, "risk_usd": None,
        "realized_pnl_usd": None, "fee_usd": 0, "funding_usd": 0,
        "slippage_usd": 0, "adverse_slippage_usd": 0,
        "confidence": None, "fill_status": "closed",
        "funding_status": "available", "pnl_semantics": None,
        "strategy_id": None, "strategy_version": None,
        "setup_type": None, "entry_equity_usd": None,
    }
    base.update(overrides)
    return base


class PerformanceReportTests(unittest.TestCase):
    def test_round_trips_match_only_by_trade_id_and_include_partial_costs(self):
        events = [
            event(ts=1, action="open", trade_id="a", notional=1000,
                  risk_usd=20, fee_usd=0.5, slippage_usd=0.2,
                  confidence=0.8),
            event(ts=2, action="partial_close", trade_id="a", fee_usd=0.2,
                  slippage_usd=0.1, realized_pnl_usd=3),
            event(ts=3, action="close", trade_id="a", fee_usd=0.3,
                  funding_usd=-0.1, slippage_usd=0.2,
                  realized_pnl_usd=10),
            event(ts=4, action="open", trade_id="still-open", notional=500),
            event(ts=5, action="close", trade_id=None,
                  realized_pnl_usd=999),
        ]
        trades, diagnostics = match_round_trips(events)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["trade_id"], "a")
        self.assertAlmostEqual(trades[0]["fees_usd"], 1.0)
        self.assertAlmostEqual(trades[0]["slippage_usd"], 0.5)
        self.assertEqual(trades[0]["r_multiple"], 0.5)
        self.assertEqual(diagnostics["unmatched_opens"], 1)
        self.assertEqual(diagnostics["unmatchable_closes"], 1)

    def test_incremental_partial_and_final_pnl_are_summed_once(self):
        events = [
            event(
                ts=1, action="open", trade_id="a", notional=1_000,
                risk_usd=20, strategy_id="momentum",
                strategy_version="phase1-v1", setup_type="range_breakout",
                entry_equity_usd=10_000,
            ),
            event(
                ts=2, action="partial_close", trade_id="a",
                realized_pnl_usd=3, pnl_semantics="incremental_v1",
                slippage_usd=-0.2, adverse_slippage_usd=0,
            ),
            event(
                ts=3, action="close", trade_id="a",
                realized_pnl_usd=7, pnl_semantics="incremental_v1",
                slippage_usd=0.5, adverse_slippage_usd=0.5,
            ),
        ]

        trades, diagnostics = match_round_trips(events)

        self.assertEqual(trades[0]["net_pnl_usd"], 10)
        self.assertEqual(trades[0]["r_multiple"], 0.5)
        self.assertAlmostEqual(trades[0]["slippage_usd"], 0.3)
        self.assertAlmostEqual(trades[0]["adverse_slippage_usd"], 0.5)
        self.assertEqual(trades[0]["strategy_id"], "momentum")
        self.assertEqual(diagnostics["legacy_pnl_semantics"], 0)

    def test_unavailable_funding_is_reported_not_silently_complete(self):
        events = [
            event(ts=1, action="open", trade_id="a", notional=1_000),
            event(
                ts=2, action="close", trade_id="a",
                realized_pnl_usd=1, pnl_semantics="incremental_v1",
                funding_status="unavailable",
            ),
        ]
        trades, diagnostics = match_round_trips(events)
        self.assertFalse(trades[0]["funding_complete"])
        self.assertEqual(diagnostics["funding_incomplete"], 1)

    def test_legacy_missing_funding_tag_is_unknown_not_assumed_zero(self):
        events = [
            event(ts=1, action="open", trade_id="a", notional=1_000),
            event(
                ts=2, action="close", trade_id="a",
                realized_pnl_usd=1, pnl_semantics="incremental_v1",
                funding_status=None,
            ),
        ]

        trades, diagnostics = match_round_trips(events)

        self.assertEqual(trades[0]["funding_status"], "legacy_unknown")
        self.assertFalse(trades[0]["funding_complete"])
        self.assertEqual(diagnostics["funding_incomplete"], 1)

    def test_equity_curve_removes_deposits_and_withdrawals(self):
        equity = [(10, 1000), (20, 1510), (30, 1310)]
        transfers = [(5, 100), (15, 500), (25, -200)]
        adjusted, net_flow = adjusted_equity_curve(equity, transfers)
        self.assertEqual(adjusted, [(10, 1000), (20, 1010), (30, 1010)])
        self.assertEqual(net_flow, 300)

    def test_equity_report_never_chains_different_valuation_bases(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute(
            "CREATE TABLE equity ("
            "ts REAL, equity REAL, state TEXT, basis_id TEXT)")
        db.executemany(
            "INSERT INTO equity VALUES (?,?,?,?)",
            [
                (1, 81_000, "RUNNING", "legacy-okb-basis"),
                (2, 80_000, "RUNNING", "legacy-okb-basis"),
                (3, 72_000, "RUNNING", "usdt-only-basis"),
                (4, 72_720, "RUNNING", "usdt-only-basis"),
            ],
        )
        output = io.StringIO()

        with redirect_stdout(output):
            print_equity(db, [])

        rendered = output.getvalue()
        self.assertIn(
            "Returns are intentionally not chained across valuation bases",
            rendered,
        )
        self.assertIn("basis legacy-okb-basis", rendered)
        self.assertIn("basis usdt-only-basis", rendered)
        self.assertIn("+1.00%", rendered)

    def test_rejection_report_shows_universe_reason_and_exchange_subcode(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute(
            "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT)")
        db.executemany(
            "INSERT INTO events VALUES (?,?,?)",
            [
                (
                    1,
                    "universe_selection",
                    json.dumps({
                        "selected": ["BTC/USDT:USDT"],
                        "candidates": [{
                            "symbol": "NEW/USDT:USDT",
                            "selected": False,
                            "reason": "insufficient_4h_history",
                        }],
                    }),
                ),
                (
                    2,
                    "entry_execution_failed",
                    json.dumps({
                        "stage": "attached_order",
                        "classification": "account_or_margin",
                        "error_message": "Insufficient balance",
                        "diagnostics": {
                            "result_rows": [{
                                "code": "51008",
                                "sub_code": "51008",
                                "message": "Insufficient balance",
                            }],
                        },
                    }),
                ),
            ],
        )
        output = io.StringIO()

        with redirect_stdout(output):
            print_rejections(db)

        rendered = output.getvalue()
        self.assertIn("insufficient_4h_history", rendered)
        self.assertIn("51008", rendered)
        self.assertIn("account_or_margin", rendered)

    def test_strategy_report_separates_prompt_config_and_code_variants(self):
        events = [
            event(
                ts=1, action="open", trade_id="a", notional=1_000,
                risk_usd=20, strategy_id="momentum",
                strategy_version="phase1-v1", setup_type="range_breakout",
                entry_equity_usd=10_000, prompt_version="prompt-a",
                config_version="config-a", code_version="code-a",
            ),
            event(
                ts=2, action="close", trade_id="a",
                realized_pnl_usd=5, pnl_semantics="incremental_v1",
            ),
            event(
                ts=3, action="open", trade_id="b", notional=1_000,
                risk_usd=20, strategy_id="momentum",
                strategy_version="phase1-v1", setup_type="range_breakout",
                entry_equity_usd=10_000, prompt_version="prompt-a",
                config_version="config-b", code_version="code-a",
            ),
            event(
                ts=4, action="close", trade_id="b",
                realized_pnl_usd=-2, pnl_semantics="incremental_v1",
            ),
        ]
        trades, _ = match_round_trips(events)
        output = io.StringIO()

        with redirect_stdout(output):
            print_per_strategy(trades)

        rendered = output.getvalue()
        self.assertEqual(rendered.count("momentum / phase1-v1"), 2)
        self.assertIn("config=config-a", rendered)
        self.assertIn("config=config-b", rendered)
        self.assertIn("signed shortfall", rendered)

    def test_strategy_report_never_mixes_runtime_accounts(self):
        events = [
            event(
                ts=1, action="open", trade_id="demo", notional=1_000,
                risk_usd=20, strategy_id="momentum",
                strategy_version="phase1-v2", setup_type="range_breakout",
                entry_equity_usd=10_000, runtime_mode="demo",
                account_fingerprint="okx-demo-a"),
            event(
                ts=2, action="close", trade_id="demo",
                realized_pnl_usd=5, pnl_semantics="incremental_v1"),
            event(
                ts=3, action="open", trade_id="live", notional=1_000,
                risk_usd=20, strategy_id="momentum",
                strategy_version="phase1-v2", setup_type="range_breakout",
                entry_equity_usd=10_000, runtime_mode="live",
                account_fingerprint="okx-live-b"),
            event(
                ts=4, action="close", trade_id="live",
                realized_pnl_usd=-2, pnl_semantics="incremental_v1"),
        ]
        trades, _ = match_round_trips(events)
        output = io.StringIO()

        with redirect_stdout(output):
            print_per_strategy(trades)

        rendered = output.getvalue()
        self.assertIn("runtime demo / okx-demo-a", rendered)
        self.assertIn("runtime live / okx-live-b", rendered)
        self.assertEqual(rendered.count("momentum / phase1-v2"), 2)


if __name__ == "__main__":
    unittest.main()
