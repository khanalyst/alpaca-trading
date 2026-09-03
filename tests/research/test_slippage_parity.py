"""Runtime/replay entry-slippage parity regressions."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest

from research.costs import CostModel, ReplayPolicy
from research.edge_discovery_core import null_control_account
from research.ibr import replay_ibr
from research.market_data import normalize_quote
from tests.research.test_ibr import bars_for_day, equity_quote, permissive_config
from tests.research.test_costs import _bars


class EntrySlippageParityTests(unittest.TestCase):
    def test_ibr_quote_beyond_runtime_cap_is_a_stable_refusal(self):
        # The bar reference is 101.00 and the executable ask is 101.06
        # (5.94 bps), beyond the shared five-bps cap.
        costs = CostModel(spread_bps=0, slippage_bps=0, fee_bps=0,
                          option_fee_per_contract_side=0,
                          max_slippage_bps=5)
        result = replay_ibr(
            bars_for_day(),
            config=permissive_config(stop_pct=.01, target_pct=.02,
                                     costs=costs),
            quotes=[equity_quote(31, 100.98, 101.06)],
        )
        self.assertEqual(result.trades, [])
        self.assertEqual(len(result.refusals), 1)
        refusal = result.refusals[0]
        self.assertEqual(refusal.reason, "entry_slippage_exceeds_limit")
        self.assertAlmostEqual(refusal.detail["adverse_bps"],
                               (101.06 - 101.0) / 101.0 * 10_000.0)

    def test_null_control_quote_beyond_cap_is_a_no_trade_row(self):
        bars = _bars([100.0] * 12)
        quotes = [normalize_quote({
            "symbol": "SPY", "timestamp": bar.timestamp.isoformat(),
            "bid": 99.99, "ask": 101.0,
            "provider": "test", "feed": "sip",
        }) for bar in bars]
        costs = CostModel(spread_bps=0, slippage_bps=0, fee_bps=0,
                          option_fee_per_contract_side=0,
                          max_slippage_bps=5)
        reference = [{
            "symbol": "SPY", "session_date": bars[0].session_date.isoformat(),
            "underlying_entry": 100.0, "stop_price": 99.0,
            "direction": "long", "no_trade": False,
        }]
        result = null_control_account(
            bars, [], {"target_r": 2, "max_hold_bars": 2},
            vehicle="equity", reference_rows=reference, account_id="parity",
            costs=costs, quotes=quotes,
            policy=ReplayPolicy(strict_market_data=False),
        )
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["rows"][0]["reject_reason"],
                         "entry_slippage_exceeds_limit")

    def test_null_exit_deadline_tie_prefers_force_flat_canonical_reason(self):
        bars = _bars([100.0] * 12)
        reference = [{
            "symbol": "SPY", "session_date": bars[0].session_date.isoformat(),
            "underlying_entry": 100.0, "stop_price": 99.0,
            "stop_distance": 1.0, "direction": "long", "no_trade": False,
        }]
        spec = {"target_r": 2, "max_hold_bars": 3}
        baseline = null_control_account(
            bars, [], spec, vehicle="equity", reference_rows=reference,
            account_id="deadline-tie",
            policy=ReplayPolicy(strict_market_data=False),
        )
        entry = datetime.fromisoformat(baseline["rows"][0]["entry_timestamp"])
        force_flat = (entry.astimezone(ZoneInfo("America/New_York")) +
                      timedelta(minutes=4)).timetz().replace(tzinfo=None)
        tied = null_control_account(
            bars, [], spec, vehicle="equity", reference_rows=reference,
            account_id="deadline-tie",
            policy=ReplayPolicy(strict_market_data=False,
                                force_flat_time=force_flat),
        )
        row = tied["rows"][0]
        self.assertFalse(row["no_trade"])
        self.assertEqual(row["exit_reason"], "time")
        self.assertEqual(row["canonical_exit_reason"], "session_force_flat")
        self.assertFalse(row["directional_authorizing"])
        self.assertFalse(row["authorizing"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
