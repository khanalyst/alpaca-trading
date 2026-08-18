import ast
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from agent.alpaca_domain import OptionContract, OptionSnapshot
from agent import engine as engine_module
from agent import risk as risk_module
from agent import risk_inputs
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

    def test_risk_input_facade_preserves_identity_and_keeps_engine_methods_local(self):
        moved = (
            "_OCC_OPTION_RE", "_object_mapping", "_normalize_option_candidate",
            "_candidate_sequence", "_num", "_timestamp",
            "_evaluation_timestamp", "_expiration_date",
            "_normalized_option_kind", "_option_kind",
            "_identity_alias_conflict",
        )
        for name in moved:
            with self.subTest(name=name):
                self.assertIs(getattr(risk_module, name),
                              getattr(risk_inputs, name))
        for name in ("Number", "re", "time", "fields", "is_dataclass"):
            with self.subTest(facade_import=name):
                self.assertIs(getattr(risk_module, name),
                              getattr(risk_inputs, name))
        with patch.object(risk_module.time, "time", return_value=123.0):
            self.assertEqual(risk_inputs._evaluation_timestamp(None), 123.0)

        with open(risk_module.__file__, encoding="utf-8") as source:
            risk_tree = ast.parse(source.read())
        top_level_defs = {
            node.name for node in risk_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertFalse(top_level_defs.intersection(moved))
        engine_node = next(
            node for node in risk_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "RiskEngine"
        )
        method_names = {
            node.name for node in engine_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue({
            "__init__", "_risk_usd", "_entry_stop", "size_shares",
            "_option_limits", "select_option_contract", "size_options",
            "vet_open",
        }.issubset(method_names))
        self.assertIs(engine_module.RiskEngine, risk_module.RiskEngine)

    def test_legacy_risk_helper_patches_intercept_selector(self):
        evaluation = datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc)
        candidate = {
            "type": "call", "dte": 14, "bid": 1.9, "ask": 2.0,
            "volume": 10, "open_interest": 100, "multiplier": 100,
            "quote_ts": evaluation, "quote_age_seconds": 0, "feed": "opra",
        }
        with patch.object(risk_module, "_candidate_sequence",
                          wraps=risk_inputs._candidate_sequence) as sequence, \
             patch.object(risk_module, "_normalize_option_candidate",
                          wraps=risk_inputs._normalize_option_candidate) as normalize, \
             patch.object(risk_module, "_timestamp",
                          wraps=risk_inputs._timestamp) as timestamp:
            selected = self.risk.select_option_contract(
                [candidate], direction="long", now=evaluation.timestamp())
        self.assertEqual(selected["type"], "call")
        self.assertGreaterEqual(sequence.call_count, 1)
        self.assertGreaterEqual(normalize.call_count, 1)
        self.assertGreaterEqual(timestamp.call_count, 1)

    def test_legacy_nested_helper_patches_reach_normalizer(self):
        evaluation = datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc)
        base = {
            "type": "call", "dte": 14, "bid": 1.9, "ask": 2.0,
            "volume": 10, "open_interest": 100, "multiplier": 100,
            "quote_ts": evaluation, "quote_age_seconds": 0,
        }
        patches = (
            ("_object_mapping", {}, base),
            ("_identity_alias_conflict", True, base),
            ("_option_kind", "put", base),
            ("_num", None, {**base, "strike": 600}),
            ("_expiration_date", None,
             {**base, "expiration": "2026-08-21"}),
        )
        for name, replacement, candidate in patches:
            with self.subTest(name=name):
                with patch.object(risk_module, name, return_value=replacement):
                    with self.assertRaisesRegex(ValueError, "no eligible"):
                        self.risk.select_option_contract(
                            [candidate], direction="long",
                            now=evaluation.timestamp())

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

    def test_boolean_numeric_inputs_fail_closed(self):
        base = {"type": "call", "dte": 14, "bid": 1.9, "ask": 2.0,
                "volume": 10, "open_interest": 100, "multiplier": 100}
        with self.assertRaisesRegex(ValueError, "no eligible"):
            self.risk.select_option_contract(
                [{**base, "multiplier": True}], direction="long")
        with self.assertRaisesRegex(ValueError, "risk_usd measurement"):
            self.risk.size_shares(10_000, 100, 2, risk_usd=True)
        with self.assertRaisesRegex(ValueError, "risk_usd measurement"):
            self.risk.size_options(10_000, True, [base], direction="long")
        plan, why = self.risk.vet_open(
            {"symbol": "SPY", "direction": "long", "entry_price": 101,
             "stop_price": 99, "target_price": 105, "risk_usd": True},
            10_000, [], {"SPY": {"price": 101}}, {}, 0, now=0)
        self.assertIsNone(plan)
        self.assertIn("risk_usd measurement is invalid", why)
        plan, why = self.risk.vet_open(
            {"symbol": "SPY", "direction": "long", "entry_price": 101,
             "stop_price": True, "stop_distance": 2, "target_price": 105},
            10_000, [], {"SPY": {"price": 101}}, {}, 0, now=0)
        self.assertIsNone(plan)

    def test_evaluation_clock_is_strict_but_legacy_candidates_remain_supported(self):
        evaluation = datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc)
        timestamped = {
            "type": "call", "dte": 14, "bid": 1.9, "ask": 2.0,
            "volume": 10, "open_interest": 100, "multiplier": 100,
            "quote_ts": evaluation, "quote_age_seconds": 0, "feed": "opra",
        }
        for clock in (evaluation, evaluation.timestamp()):
            with self.subTest(clock=clock):
                selected = self.risk.select_option_contract(
                    [timestamped], direction="long", now=clock)
                self.assertEqual(selected["type"], "call")

        # Historical offline fixtures have no quote timestamp and remain
        # usable through the timestamp-free/no-clock compatibility path.
        legacy = {key: value for key, value in timestamped.items()
                  if key not in {"quote_ts", "quote_age_seconds"}}
        self.assertEqual(
            self.risk.select_option_contract([legacy], "long")["type"], "call")

        invalid_clocks = (True, False, float("nan"), float("inf"),
                          float("-inf"), "2026-08-09T14:30:00+00:00",
                          datetime(2026, 8, 9, 14, 30))
        for clock in invalid_clocks:
            with self.subTest(clock=clock):
                with self.assertRaisesRegex(ValueError, "evaluation timestamp"):
                    self.risk.select_option_contract(
                        [timestamped], "long", now=clock)

        decision = {"symbol": "SPY", "direction": "long", "entry_price": 101,
                    "stop_price": 99, "target_price": 105}
        for clock in invalid_clocks:
            with self.subTest(vet_clock=clock):
                plan, why = self.risk.vet_open(
                    decision, 10_000, [], {"SPY": {"price": 101}}, {}, 0,
                    now=clock)
                self.assertIsNone(plan)
                self.assertIn("evaluation timestamp", why)

    def test_explicit_share_risk_budget_never_falls_back_to_default_when_malformed(self):
        invalid_budgets = (True, False, None, float("nan"), float("inf"),
                           float("-inf"), "not-a-number")
        for budget in invalid_budgets:
            with self.subTest(budget=budget):
                if budget is None:
                    # None is the sole explicit value that requests the
                    # configured default budget.
                    result = self.risk.size_shares(10_000, 100, 2,
                                                   risk_usd=budget)
                    self.assertEqual(result["shares"], 50)
                else:
                    with self.assertRaisesRegex(ValueError, "risk_usd measurement"):
                        self.risk.size_shares(10_000, 100, 2,
                                              risk_usd=budget)

    def test_option_sizing_requires_finite_positive_equity_and_risk(self):
        candidate = {"type": "call", "dte": 14, "bid": 1.9, "ask": 2.0,
                     "volume": 10, "open_interest": 100, "multiplier": 100}
        for field, values in {
            "equity": (True, False, 0, -1, float("nan"), float("inf"),
                       float("-inf"), "not-a-number"),
            "risk_usd": (True, False, 0, -1, float("nan"), float("inf"),
                         float("-inf"), "not-a-number"),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    kwargs = {"equity": 10_000, "risk_usd": 100,
                              "candidates": [candidate], "direction": "long"}
                    kwargs[field] = value
                    with self.assertRaisesRegex(ValueError,
                                                f"{field} measurement"):
                        self.risk.size_options(**kwargs)

    def test_identity_aliases_conflict_within_each_map_and_across_maps(self):
        base = {"type": "call", "dte": 14, "bid": 1.9, "ask": 2.0,
                "volume": 10, "open_interest": 100, "multiplier": 100,
                "expiration": "2026-08-21", "expiration_date": "2026-08-21",
                "strike": 600, "strike_price": 600,
                "right": "call", "option_type": "call",
                "contract_multiplier": 100, "contract_size": 100,
                "size": 100}
        cases = (
            ("outer expiry", {"expiration_date": "2026-08-22"}, None),
            ("outer strike", {"strike_price": 601}, None),
            ("outer right", {"option_type": "put"}, None),
            ("outer multiplier", {"size": 50}, None),
            ("nested expiry", {}, {"expiration": "2026-08-22"}),
            ("nested strike", {}, {"strike": 601}),
            ("nested right", {}, {"right": "put"}),
            ("nested multiplier", {}, {"multiplier": 50}),
            ("cross-map expiry", {"expiration": "2026-08-21"},
             {"expiration_date": "2026-08-22"}),
        )
        for label, outer_change, nested_change in cases:
            with self.subTest(label=label):
                candidate = {**base, **outer_change}
                if nested_change is not None:
                    candidate["contract"] = {
                        "expiration_date": "2026-08-21", "strike_price": 600,
                        "option_type": "call", "contract_size": 100,
                        **nested_change,
                    }
                with self.assertRaisesRegex(ValueError,
                                             "option identity metadata mismatch"):
                    self.risk.select_option_contract([candidate], "long")

    def test_explicit_debit_aliases_do_not_fall_back_to_ask(self):
        base = {"type": "call", "dte": 14, "bid": 1.9, "ask": 2.0,
                "volume": 10, "open_interest": 100, "multiplier": 100}
        for key in ("debit", "net_debit"):
            for value in (None, True, False, float("nan"), float("inf"),
                          float("-inf"), "not-a-number"):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, "invalid debit"):
                        self.risk.select_option_contract(
                            [{**base, key: value}], "long")

    def test_nested_contract_identity_alias_mismatches_fail_closed(self):
        base = {
            "symbol": "SPY260821C00600000", "underlying_symbol": "SPY",
            "expiration": "2026-08-21", "strike": 600, "type": "call",
            "multiplier": 100, "dte": 12, "bid": 1.9, "ask": 2.0,
            "volume": 10, "open_interest": 100,
            "contract": {
                "symbol": "SPY260821C00600000", "underlying_symbol": "SPY",
                "expiration_date": "2026-08-21", "strike_price": 600,
                "option_type": "call", "contract_size": 100,
            },
        }
        mismatches = (
            ("expiration", {"expiration": "2026-08-22"}),
            ("strike", {"strike": 601}),
            ("right", {"type": "put"}),
            ("multiplier", {"multiplier": 50}),
            ("malformed expiry", {"expiration": "not-a-date"}),
            ("malformed strike", {"strike": True}),
            ("malformed right", {"type": "unknown"}),
            ("malformed multiplier", {"multiplier": False}),
        )
        for label, change in mismatches:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "option identity metadata mismatch"):
                    self.risk.select_option_contract(
                        [{**base, **change}], direction="long", underlying="SPY")

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
        contract = OptionContract.from_sdk({"symbol": "SPY260821C00600000",
                                            "feed": "opra"})
        snapshot = OptionSnapshot(
            symbol=contract.symbol, contract=contract, bid=1.9, ask=2,
            volume=10, open_interest=100, timestamp=evaluation, feed="opra")
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
