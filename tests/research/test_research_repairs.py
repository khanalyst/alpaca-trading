"""Regression coverage for the verified research-audit repairs."""

from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.costs import BAR, ReplayPolicy, recompute_row_cost_economics
from research.factory_core import simulate_account
from research.gates import authorization_projection, floor_feasibility
from research.quote_costs import measure_quote_costs, measured_cost_resolver
from research.mechanism_cohort_v3 import COHORT_ID
from research.strategy_factory import run_factory

from .test_costs import BASE, FLAT, RISING, SPEC, _bars
from .test_factory_end_to_end import edge_corpus


class ResearchRepairTests(unittest.TestCase):
    def test_v3_dispatch_replays_12_arms_without_a_factory_ledger(self):
        progress = []
        with tempfile.TemporaryDirectory() as directory, \
                patch("research.strategy_factory._tuned_variants",
                      side_effect=AssertionError("v3 must not tune")), \
                patch("research.strategy_factory._adapter",
                      side_effect=AssertionError("v3 must not use an LLM")):
            ledger = Path(directory) / "factory.sqlite3"
            result = run_factory(
                edge_corpus(2), db_path=ledger, diagnostic_only=True,
                cohort=COHORT_ID, strategy_llm={"enabled": True},
                progress_callback=lambda *event: progress.append(event))

        self.assertFalse(ledger.exists())
        self.assertEqual(result["strategies"], 12)
        self.assertEqual(result["variants"], 12)
        self.assertEqual(result["proofs"], [])
        self.assertFalse(result["authorizing"])
        self.assertEqual(progress[-1], ("evaluating", 12, 12))
        self.assertEqual(
            {arm["variant_id"] for report in result["reports"]
             for arm in report["variants"]},
            {arm["variant_id"] for family in result["mechanism_cohort"]["families"]
             for arm in family["arms"]},
        )

    def test_covered_measured_bar_rows_recompute_but_never_authorize(self):
        quote_rows = []
        for minute, price in ((4, 100.9), (8, 100.9)):
            timestamp = BASE + timedelta(minutes=minute)
            quote_rows.append({
                "kind": "quote", "symbol": "SPY",
                "timestamp": timestamp.isoformat(),
                "bid": price - .1, "ask": price + .1,
                "bid_size": 1000.0, "ask_size": 1000.0,
                "feed": "sip", "provider": "test",
            })
        schedule = measure_quote_costs(
            quote_rows, feed="sip", provider="test", min_quotes_per_cell=1)
        resolver = measured_cost_resolver(
            schedule, expected_feed="sip", expected_provider="test",
            fee_bps=0.0)
        book = simulate_account(
            _bars(RISING + FLAT), [], SPEC, vehicle="equity",
            account_id="measured-bar-repair", policy=ReplayPolicy(
                strict_market_data=False), cost_resolver=resolver)
        row = book["rows"][0]

        self.assertEqual(row["entry_fill_source"], BAR)
        self.assertEqual(row["exit_fill_source"], BAR)
        self.assertEqual(row["entry_bar_feed"], "sip")
        self.assertEqual(row["entry_bar_provider"], "test")
        self.assertEqual(row["entry_feed"], "sip")
        self.assertEqual(row["entry_provider"], "test")
        self.assertIsNotNone(row.get("cost_economics"))
        self.assertIsNotNone(
            recompute_row_cost_economics(row, vehicle="equity"))

        projection = authorization_projection(
            [row], vehicle="equity", strict=True,
            equity_feed="sip", equity_provider="test")
        self.assertEqual(projection["counts"]["eligible"], 0)
        self.assertIn(projection["excluded"][0]["reason"],
                      {"diagnostic_bar_fallback",
                       "non_authorizing_fill_source"})

    def test_raw_no_trade_capacity_does_not_become_impossible(self):
        rows = [{
            "vehicle": "equity", "symbol": "SPY",
            "session_date": (
                f"2026-{1 + (day - 1) // 28:02d}-{1 + (day - 1) % 28:02d}"),
            "opportunity_id": f"SPY:{day}",
            "signal_opportunity": True, "no_trade": True,
            "entry_fill_source": BAR, "reject_reason": "refused",
        } for day in range(1, 101)]
        report = floor_feasibility(
            rows, vehicle="equity", min_trades=100, min_sessions=2,
            available_sessions=2)

        self.assertEqual(report["available"]["trades"], 100)
        self.assertEqual(report["observed"]["trades"], 0)
        self.assertNotEqual(report["status"], "impossible")
        self.assertEqual(report["status"], "underpowered")


if __name__ == "__main__":
    unittest.main()
