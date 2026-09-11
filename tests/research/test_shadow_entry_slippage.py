"""Diagnostic live-shadow entry-slippage parity regressions."""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research.costs import (
    ENTRY_SLIPPAGE_INVALID_REASON, ENTRY_SLIPPAGE_REJECT_REASON,
    check_entry_slippage,
)
from research.diagnostic_shadow import build_diagnostic_cohort
from research.live_shadow import ShadowConfig, ShadowRunner


UTC = timezone.utc
ABSENT_EXECUTION = object()


def _runtime_config() -> dict:
    return {
        "mode": "paper",
        "broker": {"provider": "alpaca", "data_feed": "iex",
                   "options_feed": "opra", "paper": True,
                   "allow_live": False},
        "session": {"timezone": "America/New_York",
                    "entries_regular_session_only": True,
                    "allow_exits_outside_session": True,
                    "require_exact_calendar": False,
                    "force_flat_minutes_before_close": 10,
                    "reject_new_entries_minutes_before_close": 5},
        "universe": {"symbols": ["SPY"], "asset_classes": ["us_equity"],
                     "min_price": 1.0, "max_symbols": 1, "denylist": []},
        "risk": {"risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 2.0,
                 "max_open_risk_pct": 2.0, "max_concurrent_positions": 3,
                 "max_position_notional_pct": 25.0,
                 "max_gross_exposure_pct": 50.0,
                 "stressed_cost_scenario_bps": 25.0,
                 "max_stressed_cost_to_risk_ratio": 0.30},
        "execution": {"order_type": "market", "time_in_force": "day",
                      "max_slippage_bps": 50,
                      "max_market_data_age_seconds": 30,
                      "max_spread_bps": 100, "strict_market_data": True},
        "costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                  "fee_bps": 0.5,
                  "provenance": "shadow_entry_slippage_test"},
        "research": {"enabled": True, "require_validated_variant": True},
    }


class ShadowEntrySlippageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"
        self.corpus.write_text("event_key,event_type\n", encoding="utf-8")
        self.runtime = _runtime_config()
        cohort = build_diagnostic_cohort(
            self.runtime, code_identity="a" * 64)
        self.arm = next(
            deepcopy(arm) for arm in cohort["arms"]
            if arm["family"] == "opening_range_breakout" and
            arm["role"] == "baseline")
        self.runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            runtime_config=deepcopy(self.runtime),
            runtime_config_path="/mounted/config.yaml",
            max_events=100, max_decisions=1000, max_workers=1))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _bar(stamp: datetime, *, event_key: str) -> dict:
        observed = stamp + timedelta(minutes=1)
        return {
            "event_key": event_key, "event_type": "bar_1m",
            "symbol": "SPY", "timestamp": stamp.isoformat(),
            "as_of": observed.isoformat(),
            "observed_at": observed.isoformat(),
            "provider": "alpaca", "feed": "iex",
            "source_mode": "forward_observed",
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0, "volume": 1000.0,
        }

    def _market(self, bid, ask, *, quote_age_seconds: float = 0.0):
        first_at = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
        previous = self._bar(first_at, event_key="bar-previous")
        event = self._bar(
            first_at + timedelta(minutes=1), event_key="bar-signal")
        event_at = datetime.fromisoformat(event["observed_at"])
        quote_at = event_at - timedelta(seconds=quote_age_seconds)
        quote = {
            "event_key": "quote-entry", "event_type": "quote",
            "symbol": "SPY", "timestamp": quote_at.isoformat(),
            "as_of": quote_at.isoformat(), "observed_at": quote_at.isoformat(),
            "provider": "alpaca", "feed": "iex",
            "source_mode": "forward_observed", "bid": bid, "ask": ask,
        }
        return event, {"SPY": [previous, event]}, {"SPY": [quote]}

    @staticmethod
    def _signal(direction: str, reference) -> dict:
        return {
            "symbol": "SPY", "direction": direction,
            "setup_type": "rule_probe", "signal_ts": 1_767_365_460.0,
            "entry_price": reference,
            "stop_price": 98.0 if direction == "long" else 102.0,
            "target_price": 102.0 if direction == "long" else 98.0,
            "stop_distance": 2.0, "target_r": 1.0,
            "execution_profile": "shares",
        }

    @staticmethod
    def _risk_plan(_engine, decision, *_args, **_kwargs):
        plan = dict(decision)
        plan.update(shares=1.0, contracts=1.0,
                    notional=float(plan["entry_price"]), risk_usd=2.0)
        return plan, None

    def _evaluate_case(
            self, *, direction: str, reference, bid, ask, cap=50,
            quote_age_seconds: float = 0.0, diagnostic: bool = True,
            risk_reason: str | None = None):
        candidate = deepcopy(self.arm)
        if cap is ABSENT_EXECUTION:
            candidate["config"].pop("execution", None)
        else:
            candidate["config"].setdefault("execution", {})[
                "max_slippage_bps"] = cap
        if not diagnostic:
            candidate["candidate_id"] = "ordinary-shadow-candidate"
            candidate["status"] = "backtest_passed"
            candidate["config"].pop("diagnostic_shadow", None)
            candidate.pop("diagnostic_only", None)
        event, bars, quotes = self._market(
            bid, ask, quote_age_seconds=quote_age_seconds)
        signal = self._signal(direction, reference)
        setup_calls: list[tuple[dict, dict]] = []

        def setup(signal_arg, snapshot_arg, _cfg):
            setup_calls.append((deepcopy(dict(signal_arg)),
                                deepcopy(dict(snapshot_arg))))
            plan = {
                **dict(signal_arg),
                "entry_price": snapshot_arg["entry_price"],
                "execution_profile": "shares",
            }
            return plan, None

        def risk(_engine, decision, *_args, **_kwargs):
            if risk_reason is not None:
                return None, risk_reason
            return self._risk_plan(_engine, decision)

        with patch("research.live_shadow.generate_rule_signal",
                   return_value=signal), patch(
                       "research.live_shadow.build_setup_plan",
                       side_effect=setup), patch(
                           "research.live_shadow.RiskEngine.vet_open",
                           autospec=True, side_effect=risk), patch(
                               "research.live_shadow.check_entry_slippage",
                               wraps=check_entry_slippage) as slippage:
            result = self.runner._evaluate(
                candidate, event, bars, quotes, {})
        return result, slippage, setup_calls, candidate, event, bars, quotes

    def test_adverse_long_and_short_use_shared_runtime_factory_reason(self):
        for direction, bid, ask, side, executable in (
                ("long", 100.99, 101.0, "buy", 101.0),
                ("short", 99.0, 99.01, "sell", 99.0)):
            with self.subTest(direction=direction):
                result, helper, setup_calls, *_ = self._evaluate_case(
                    direction=direction, reference=100.0,
                    bid=bid, ask=ask)
                kind, reason, payload, plan = result
                self.assertEqual((kind, reason, plan),
                                 ("reject", ENTRY_SLIPPAGE_REJECT_REASON, None))
                helper.assert_called_once_with(side, 100.0, executable, 50)
                telemetry = payload["snapshot"]["entry_slippage"]
                self.assertEqual(telemetry["reason"],
                                 ENTRY_SLIPPAGE_REJECT_REASON)
                self.assertAlmostEqual(telemetry["adverse_bps"], 100.0)
                self.assertEqual(payload["signal"]["entry_price"], 100.0)
                self.assertEqual(payload["snapshot"]["entry_price"], executable)
                for key in ("snapshot", "setup_plan", "risk_plan"):
                    self.assertEqual(payload[key]["authored_entry_reference"],
                                     100.0)
                    self.assertEqual(payload[key]["executable_entry_reference"],
                                     executable)
                self.assertEqual(len(setup_calls), 1)

    def test_exact_cap_and_favorable_quotes_keep_executable_geometry(self):
        cases = (
            ("exact-cap", "long", 100.49, 100.5, 100.5, 50.0),
            ("favorable-long", "long", 98.99, 99.0, 99.0, 0.0),
            ("favorable-short", "short", 101.0, 101.01, 101.0, 0.0),
        )
        for name, direction, bid, ask, executable, adverse in cases:
            with self.subTest(name=name):
                result, helper, setup_calls, *_ = self._evaluate_case(
                    direction=direction, reference=100.0,
                    bid=bid, ask=ask)
                kind, _reason, payload, plan = result
                self.assertEqual(kind, "open_incomplete")
                self.assertIsNotNone(plan)
                self.assertEqual(plan["entry_price"], executable)
                self.assertEqual(
                    plan["stop_price"], 98.0 if direction == "long" else 102.0)
                self.assertEqual(
                    plan["target_price"], 102.0 if direction == "long" else 98.0)
                self.assertEqual(payload["setup_plan"]["entry_price"], executable)
                self.assertEqual(payload["risk_plan"]["entry_price"], executable)
                telemetry = plan["entry_slippage"]
                self.assertTrue(telemetry["accepted"])
                self.assertAlmostEqual(telemetry["adverse_bps"], adverse)
                self.assertEqual(setup_calls[0][0]["entry_price"], 100.0)
                self.assertEqual(setup_calls[0][1]["entry_price"], executable)
                self.assertEqual(helper.call_count, 1)

    def test_absent_execution_block_uses_runtime_default_cap(self):
        result, helper, *_ = self._evaluate_case(
            direction="long", reference=100.0, bid=100.49, ask=100.5,
            cap=ABSENT_EXECUTION)
        kind, _reason, payload, plan = result
        self.assertEqual(kind, "open_incomplete")
        self.assertIsNotNone(plan)
        helper.assert_called_once_with("buy", 100.0, 100.5, 50)
        self.assertEqual(payload["snapshot"]["entry_slippage"][
            "max_slippage_bps"], 50.0)

    def test_malformed_references_and_explicit_caps_fail_closed(self):
        cases = (
            ("missing-reference", None, 99.99, 100.1, 50),
            ("string-reference", "100", 99.99, 100.1, 50),
            ("nan-reference", float("nan"), 99.99, 100.1, 50),
            ("infinite-reference", float("inf"), 99.99, 100.1, 50),
            ("missing-cap", 100.0, 99.99, 100.1, None),
            ("string-cap", 100.0, 99.99, 100.1, "50"),
            ("nan-cap", 100.0, 99.99, 100.1, float("nan")),
            ("negative-cap", 100.0, 99.99, 100.1, -1.0),
        )
        for name, reference, bid, ask, cap in cases:
            with self.subTest(name=name):
                result, helper, _setup_calls, *_ = self._evaluate_case(
                    direction="long", reference=reference,
                    bid=bid, ask=ask, cap=cap)
                kind, reason, payload, plan = result
                self.assertEqual((kind, reason, plan),
                                 ("reject", ENTRY_SLIPPAGE_INVALID_REASON, None))
                self.assertEqual(helper.call_count, 1)
                telemetry = payload["snapshot"]["entry_slippage"]
                self.assertFalse(telemetry["accepted"])
                self.assertEqual(telemetry["reason"],
                                 ENTRY_SLIPPAGE_INVALID_REASON)
                self.assertEqual(payload["snapshot"][
                    "executable_entry_reference"], 100.1)

    def test_malformed_missing_and_stale_quotes_remain_unpriced(self):
        cases = (
            ("missing", None, None, 0.0),
            ("numeric-string", 99.99, "100.1", 0.0),
            ("nan", 99.99, float("nan"), 0.0),
            ("infinite", 99.99, float("inf"), 0.0),
            ("stale", 100.99, 101.0, 31.0),
        )
        for name, bid, ask, age in cases:
            with self.subTest(name=name):
                result, helper, _setup_calls, *_ = self._evaluate_case(
                    direction="long", reference=100.0, bid=bid, ask=ask,
                    quote_age_seconds=age)
                kind, reason, payload, plan = result
                if name == "numeric-string":
                    self.assertEqual(
                        (kind, reason, plan),
                        ("reject", ENTRY_SLIPPAGE_INVALID_REASON, None))
                else:
                    self.assertEqual(
                        (kind, reason, plan),
                        ("unpriced", "stale or unavailable quote", None))
                self.assertEqual(helper.call_count, 1)
                self.assertFalse(payload["snapshot"][
                    "entry_slippage"]["accepted"])

    def test_existing_risk_rejection_precedes_slippage_refusal(self):
        result, helper, *_ = self._evaluate_case(
            direction="long", reference=100.0, bid=100.99, ask=101.0,
            risk_reason="spread is too wide")
        self.assertEqual(result[:2], ("reject", "spread is too wide"))
        self.assertIsNone(result[3])
        helper.assert_called_once_with("buy", 100.0, 101.0, 50)
        self.assertEqual(result[2]["snapshot"]["entry_slippage"]["reason"],
                         ENTRY_SLIPPAGE_REJECT_REASON)

    def test_non_diagnostic_candidate_does_not_apply_shadow_slippage_gate(self):
        result, helper, setup_calls, *_ = self._evaluate_case(
            direction="long", reference=100.0, bid=100.99, ask=101.0,
            diagnostic=False)
        self.assertEqual(result[0], "open_incomplete")
        self.assertEqual(result[3]["entry_price"], 100.0)
        self.assertEqual(setup_calls[0][1]["entry_price"], 100.0)
        helper.assert_not_called()

    def test_refused_entry_persists_without_diagnostic_order_or_fill(self):
        candidate = deepcopy(self.arm)
        event, bars, quotes = self._market(100.99, 101.0)
        session = "2026-01-02"
        candidate_id = str(candidate["candidate_id"])
        cohort_identity = str(candidate["cohort_identity"])
        self.runner.store.upsert_candidate(candidate)
        self.runner.store.seed_diagnostic_accounts(
            cohort_identity=cohort_identity, candidate_ids=[candidate_id],
            starting_cash=self.runner.config.equity)
        initial = self.runner.store.diagnostic_account_snapshot(
            cohort_identity=cohort_identity, candidate_id=candidate_id)
        signal = self._signal("long", 100.0)

        def setup(signal_arg, snapshot_arg, _cfg):
            return ({**dict(signal_arg),
                     "entry_price": snapshot_arg["entry_price"],
                     "execution_profile": "shares"}, None)

        with patch("research.live_shadow.generate_rule_signal",
                   return_value=signal), patch(
                       "research.live_shadow.build_setup_plan",
                       side_effect=setup), patch(
                           "research.live_shadow.RiskEngine.vet_open",
                           autospec=True, side_effect=self._risk_plan):
            evaluated = self.runner._evaluate_diagnostic_arm_snapshot(
                candidate, {session: [event]},
                {session: (bars["SPY"], quotes["SPY"], ())},
                bars, quotes, {}, initial)
        self.assertIsNone(evaluated["error"], evaluated)
        self.assertEqual(len(evaluated["decisions"]), 1)
        decision = evaluated["decisions"][0]
        self.assertEqual((decision["kind"], decision["reason"],
                          decision["plan"]),
                         ("reject", ENTRY_SLIPPAGE_REJECT_REASON, None))
        self.assertEqual(evaluated["account_batch"]["orders"], [])
        self.assertEqual(evaluated["account_batch"]["fills"], [])
        self.runner.store.record_diagnostic_batch(
            cohort_identity=cohort_identity, candidate_id=candidate_id,
            cursor_inserted_at=1.0, cursor_event_key=event["event_key"],
            processed_events=1, rollups={session: {"reject": 1}},
            pending_sessions=[session], decisions=evaluated["decisions"],
            warmup_session=None,
            max_decisions=self.runner.config.max_decisions,
            account_batch=evaluated["account_batch"])
        with closing(sqlite3.connect(self.shadow)) as db:
            account = db.execute(
                "SELECT order_count,fill_count,open_position_count "
                "FROM diagnostic_accounts WHERE cohort_identity=? "
                "AND candidate_id=?", (cohort_identity, candidate_id)).fetchone()
            orders = db.execute(
                "SELECT count(*) FROM diagnostic_orders WHERE cohort_identity=? "
                "AND candidate_id=?", (cohort_identity, candidate_id)).fetchone()[0]
            fills = db.execute(
                "SELECT count(*) FROM diagnostic_fills WHERE cohort_identity=? "
                "AND candidate_id=?", (cohort_identity, candidate_id)).fetchone()[0]
        self.assertEqual(account, (0, 0, 0))
        self.assertEqual((orders, fills), (0, 0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
