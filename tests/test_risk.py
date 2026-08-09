import time
import unittest
from datetime import datetime, timezone

from agent.alpaca_domain import OptionContract, OptionSnapshot
from agent.risk import RiskEngine, select_option_contract, size_shares


class RiskProfileTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"risk": {"risk_per_trade_pct": 1.0,
                              "max_position_notional_pct": 50,
                              "max_concurrent_positions": 3,
                              "options_min_dte": 7,
                              "options_max_dte": 45,
                              "options_max_spread_pct": 10},
                    "execution": {}}
        self.risk = RiskEngine(self.cfg)

    def test_share_sizing_is_floor_risk_over_stop_and_notional_capped(self):
        result = self.risk.size_shares(equity=10_000, entry_price=100,
                                       stop_distance=2, risk_usd=101,
                                       symbol_data={})
        self.assertEqual(result["shares"], 50)  # 50% notional cap binds
        self.assertEqual(result["notional"], 5_000)

    def test_liquidity_cap_reduces_share_size(self):
        result = size_shares(equity=10_000, entry_price=100,
                             stop_distance=2, risk_usd=100,
                             risk_config={"max_position_notional_pct": 90},
                             symbol_data={"liquidity_cap_shares": 7})
        self.assertEqual(result["shares"], 7)

    def test_option_multiplier_is_returned_and_max_loss_is_debit_times_multiplier(self):
        option = self.risk.size_options(
            equity=10_000, risk_usd=100,
            candidates=[{"type": "call", "dte": 14, "bid": 1.9,
                         "ask": 2.0, "volume": 10, "open_interest": 100,
                         "multiplier": 10}], direction="long")
        self.assertEqual(option["multiplier"], 10)
        self.assertEqual(option["contracts"], 5)
        self.assertEqual(option["max_loss"], 100)

    def test_short_uses_put_and_debit_spread_is_rejected(self):
        put = select_option_contract(
            [{"type": "put", "dte": 21, "bid": 2.0, "ask": 2.1,
              "volume": 1, "open_interest": 2, "multiplier": 50}],
            direction="short", risk_config={})
        self.assertEqual(put["type"], "put")
        with self.assertRaisesRegex(ValueError, "multi-leg"):
            select_option_contract(
                [{"strategy": "debit_spread", "dte": 21, "debit": 1.2,
                  "volume": 1, "open_interest": 2, "multiplier": 25}],
                direction="long", risk_config={})

    def test_rejects_zero_dte_stale_wide_illiquid_and_naked_short(self):
        cases = [
            [{"type": "call", "dte": 0, "ask": 1, "volume": 1,
              "multiplier": 100}],
            [{"type": "call", "dte": 14, "ask": 1, "stale": True,
              "volume": 1, "multiplier": 100}],
            [{"type": "call", "dte": 14, "bid": 1, "ask": 2,
              "volume": 1, "multiplier": 100}],
            [{"type": "call", "dte": 14, "ask": 1, "volume": 0,
              "open_interest": 0, "multiplier": 100}],
            [{"type": "call", "side": "sell", "dte": 14, "ask": 1,
              "volume": 1, "multiplier": 100}],
        ]
        for candidates in cases:
            with self.subTest(candidates=candidates):
                with self.assertRaisesRegex(ValueError, "no eligible"):
                    self.risk.select_option_contract(candidates, direction="long")

    def test_vet_options_keeps_underlying_stop_target(self):
        decision = {"symbol": "SPY", "direction": "long", "entry_price": 101,
                    "stop_price": 99.5, "target_price": 104,
                    "execution_profile": "options", "option_chain": [
                        {"type": "call", "dte": 14, "bid": 1.9,
                         "ask": 2, "volume": 1, "open_interest": 10,
                         "multiplier": 10}], "force_flat": True}
        plan, why = self.risk.vet_open(
            decision, 10_000, [], {"SPY": {"price": 101}}, {}, 0)
        self.assertIsNone(why)
        self.assertEqual(plan["underlying_stop_price"], 99.5)
        self.assertEqual(plan["underlying_target_price"], 104)
        self.assertEqual(plan["contract_multiplier"], 10)

    def test_option_snapshot_dataclass_is_normalized_and_occ_identity_checked(self):
        evaluation = datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc)
        contract = OptionContract.from_sdk({"symbol": "SPY260821C00600000"})
        snapshot = OptionSnapshot(
            symbol=contract.symbol, contract=contract, bid=1.9, ask=2,
            volume=10, open_interest=100, timestamp=evaluation)
        selected = self.risk.select_option_contract(
            [snapshot], direction="long", now=evaluation.timestamp(),
            underlying="SPY")
        self.assertEqual(selected["symbol"], contract.symbol)
        self.assertEqual(selected["type"], "call")
        self.assertEqual(selected["right"], "call")
        self.assertEqual(selected["contract_size"], 100)
        self.assertEqual(selected["dte"], 12)

    def test_rejects_equity_like_bad_and_wrong_underlying_option_symbols(self):
        base = {"type": "call", "dte": 14, "bid": 1.9, "ask": 2,
                "volume": 1, "open_interest": 10, "multiplier": 100}
        for symbol in ("BAD", "SPY", "QQQ260821C00600000"):
            with self.subTest(symbol=symbol):
                with self.assertRaisesRegex(ValueError, "no eligible"):
                    select_option_contract(
                        [{**base, "symbol": symbol}], "long",
                        underlying="SPY")

    def test_occ_right_and_expiration_metadata_cannot_disagree(self):
        now = datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc)
        base = {"symbol": "SPY260821C00600000", "type": "call",
                "dte": 12, "expiration": "2026-08-21", "bid": 1.9,
                "ask": 2, "volume": 1, "open_interest": 10,
                "multiplier": 100, "quote_ts": now}
        mismatches = (
            {**base, "type": "put"},
            {**base, "expiration": "2026-08-22"},
            {**base, "dte": float("nan")},
            {**base, "strike": 601},
            {**base, "strike_price": 601},
            # An expired OCC contract must not be rescued by an in-range DTE.
            {**base, "symbol": "SPY260101C00600000",
             "expiration": "2026-01-01"},
        )
        for candidate in mismatches:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, "no eligible"):
                    self.risk.select_option_contract(
                        [candidate], "long", now=now.timestamp(),
                        underlying="SPY")

    def test_freshness_flags_and_quote_timestamps_are_strict(self):
        evaluation = datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc)
        base = {"type": "call", "dte": 14, "bid": 1.9, "ask": 2,
                "volume": 1, "open_interest": 10, "multiplier": 100}
        malformed = (
            {**base, "stale": "false"},
            {**base, "quote_stale": 0},
            {**base, "quote_ts": "2026-08-09T14:30:00", "quote_age_seconds": 0},
            {**base, "quote_ts": evaluation.replace(year=2027),
             "quote_age_seconds": 0},
            {**base, "quote_ts": evaluation.replace(hour=14, minute=0),
             "quote_age_seconds": 0},
            {**base, "quote_ts": evaluation,
             "quote_timestamp": evaluation.replace(year=2027),
             "quote_age_seconds": 0},
        )
        for candidate in malformed:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, "no eligible"):
                    self.risk.select_option_contract(
                        [candidate], "long", now=evaluation.timestamp())

    def test_non_finite_explicit_risk_and_daily_pnl_fail_closed(self):
        decision = {"symbol": "SPY", "direction": "long", "entry_price": 101,
                    "stop_price": 99, "target_price": 105}
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(field="risk_usd", value=value):
                plan, why = self.risk.vet_open(
                    {**decision, "risk_usd": value}, 10_000, [],
                    {"SPY": {"price": 101}}, {}, 0, now=0)
                self.assertIsNone(plan)
                self.assertIn("risk_usd measurement is invalid", why)

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(field="daily_pnl", value=value):
                plan, why = self.risk.vet_open(
                    {**decision, "daily_pnl": value}, 10_000, [],
                    {"SPY": {"price": 101}}, {}, 0, now=0)
                self.assertIsNone(plan)
                self.assertIn("daily P&L measurement is invalid", why)

        plan, why = self.risk.vet_open(
            decision, 10_000, [], {"SPY": {"price": 101, "stale": "false"}},
            {}, 0, now=0)
        self.assertIsNone(plan)
        self.assertIn("market freshness flag is invalid", why)


if __name__ == "__main__":
    unittest.main()
