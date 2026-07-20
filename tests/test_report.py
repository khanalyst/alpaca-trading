import unittest

from report import adjusted_equity_curve, match_round_trips


def event(**overrides):
    base = {
        "ts": 0, "symbol": "BTC/USDT:USDT", "action": None,
        "trade_id": None, "notional": None, "risk_usd": None,
        "realized_pnl_usd": None, "fee_usd": 0, "funding_usd": 0,
        "slippage_usd": 0, "confidence": None, "fill_status": "closed",
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

    def test_equity_curve_removes_deposits_and_withdrawals(self):
        equity = [(10, 1000), (20, 1510), (30, 1310)]
        transfers = [(5, 100), (15, 500), (25, -200)]
        adjusted, net_flow = adjusted_equity_curve(equity, transfers)
        self.assertEqual(adjusted, [(10, 1000), (20, 1010), (30, 1010)])
        self.assertEqual(net_flow, 300)


if __name__ == "__main__":
    unittest.main()
