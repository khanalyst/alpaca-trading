"""Unmocked parity checks for the opt-in diagnostic IBR shadow lane.

The fixtures are deliberately small forward-observed minute streams.  The
reference side uses the same live collection, IBR, setup, and risk contracts
as the runtime; the comparison side exercises the diagnostic shadow and its
persistent broker-free account book.  Nothing in this module authorizes an
order or treats a modeled return as evidence of profitability.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from agent.contracts.ibr import generate_ibr_signal
from agent.market_entry_risk import MarketEntryRiskMixin
from agent.risk import RiskEngine
from agent.startup_edge_policy import StartupEdgePolicyMixin
from agent.strategy import build_setup_plan
from deploy.recorder import INDEX_NAME
from research.costs import ReplayPolicy
from research.diagnostic_accounts import DiagnosticAccountBook, new_account_state
from research.diagnostic_shadow import build_diagnostic_cohort
from research.live_shadow import ShadowConfig, ShadowRunner


UTC = timezone.utc
SESSION_OPEN = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
SESSION = "2026-09-08"


def _runtime_config() -> dict:
    return {
        "mode": "paper",
        "broker": {
            "provider": "alpaca", "data_feed": "iex",
            "options_feed": "opra", "paper": True, "allow_live": False,
        },
        "session": {
            "timezone": "America/New_York",
            "entries_regular_session_only": True,
            "allow_exits_outside_session": True,
            "require_exact_calendar": False,
            "force_flat_minutes_before_close": 10,
            "reject_new_entries_minutes_before_close": 5,
        },
        "universe": {
            "symbols": ["SPY"], "asset_classes": ["us_equity"],
            "min_price": 1.0, "max_symbols": 1, "denylist": [],
        },
        "strategy": {
            "id": "ibr", "version": "v1", "variant_id": "ibr.baseline",
            "execution_mode": "shares", "range_minutes": 15,
            "breakout_buffer_bps": 5.0, "min_relative_volume": 1.0,
            "target_r": 2.0, "max_entry_extension_r": 1.0,
            "min_ibr_width_atr": 0.25, "max_ibr_width_atr": 3.0,
            "atr_period": 14, "max_ibr_width_pct": 2.0,
            "stale_minutes": 0.5, "max_spread_bps": 25.0,
            "latest_entry_time": "15:00",
            "force_flat_minutes_before_close": 10,
        },
        "risk": {
            "risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 2.0,
            "max_open_risk_pct": 2.0, "max_concurrent_positions": 3,
            "max_position_notional_pct": 25.0,
            "max_gross_exposure_pct": 50.0,
            "stressed_cost_scenario_bps": 25.0,
            "max_stressed_cost_to_risk_ratio": 0.30,
        },
        "execution": {
            "order_type": "market", "time_in_force": "day",
            "max_slippage_bps": 50.0,
            "max_market_data_age_seconds": 30.0,
            "max_spread_bps": 100.0, "strict_market_data": True,
        },
        "costs": {
            "spread_bps": 4.0, "slippage_bps": 6.0, "fee_bps": 0.5,
            "provenance": "ibr-runtime-parity-test",
        },
        "research": {"enabled": True, "require_validated_variant": True},
    }


def _bar(stamp: datetime, *, opened: float = 100.0, high: float = 100.5,
         low: float = 99.5, close: float = 100.0, volume: float = 1000.0,
         observed_at: datetime | None = None, key: str | None = None,
         halt: bool = False) -> dict:
    ended = stamp + timedelta(minutes=1)
    observed = observed_at or ended
    row = {
        "event_key": key or f"bar:{stamp.isoformat()}:{observed.isoformat()}",
        "event_type": "bar_1m", "symbol": "SPY",
        "timestamp": stamp.isoformat(), "as_of": ended.isoformat(),
        "observed_at": observed.isoformat(), "provider": "alpaca",
        "feed": "iex", "source_mode": "forward_observed",
        "open": opened, "high": high, "low": low, "close": close,
        "volume": volume,
    }
    if halt:
        row["halt"] = True
    return row


def _quote(stamp: datetime, bid: float = 101.2, ask: float = 101.4, *,
           observed_at: datetime | None = None, key: str | None = None) -> dict:
    observed = observed_at or stamp
    return {
        "event_key": key or f"quote:{stamp.isoformat()}:{bid}:{ask}",
        "event_type": "quote", "symbol": "SPY",
        "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
        "observed_at": observed.isoformat(), "provider": "alpaca",
        "feed": "iex", "source_mode": "forward_observed",
        "bid": bid, "ask": ask,
    }


def _opening_breakout(*, close: float = 101.2, observed_delay: float = 5.0,
                      halt: bool = False, range_high: float = 100.5,
                      range_low: float = 99.5) -> list[dict]:
    bars = [
        _bar(SESSION_OPEN + timedelta(minutes=index), high=range_high,
             low=range_low)
        for index in range(15)
    ]
    stamp = SESSION_OPEN + timedelta(minutes=15)
    bars.append(_bar(
        stamp, opened=100.2, high=max(close + 0.2, range_high),
        low=min(100.1, close - 0.2), close=close, volume=2000.0,
        observed_at=stamp + timedelta(minutes=1, seconds=observed_delay),
        key="bar:signal", halt=halt))
    return bars


def _available(row: dict, at: datetime) -> bool:
    values = []
    for name in ("timestamp", "as_of", "observed_at"):
        value = datetime.fromisoformat(str(row[name]).replace("Z", "+00:00"))
        values.append(value.astimezone(UTC))
    return max(values) <= at


class _LiveCollector(MarketEntryRiskMixin):
    def __init__(self, cfg: dict):
        self.cfg = cfg


class _CutoffProbe(StartupEdgePolicyMixin):
    def __init__(self, cfg: dict):
        self.cfg = cfg


class IBRRuntimeParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ibr-runtime-parity-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "recorded.csv"
        self.edge = self.root / "edge.sqlite3"
        self.shadow = self.root / "shadow.sqlite3"
        self.corpus.write_text("event_key,event_type\n", encoding="utf-8")
        self.runtime = _runtime_config()
        self.cohort = build_diagnostic_cohort(
            self.runtime, code_identity="a" * 64, include_ibr=True)
        self.arm = next(
            deepcopy(arm) for arm in self.cohort["arms"]
            if arm.get("strategy_id") == "ibr" and
            arm.get("variant_id") == "ibr.baseline")
        self.runner = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            diagnostic_include_ibr=True,
            runtime_config=deepcopy(self.runtime),
            runtime_config_path="/mounted/config.yaml",
            max_events=500, max_decisions=5000, max_workers=1,
            diagnostic_session_max_events=500))

    def _candidate(self, **strategy_overrides) -> dict:
        arm = deepcopy(self.arm)
        arm["config"]["strategy"].update(strategy_overrides)
        return arm

    def _live_row(self, candidate: dict, bars: list[dict], quotes: list[dict],
                  now: datetime) -> dict | None:
        result = _LiveCollector(candidate["config"])._collect(
            ["SPY"], now, {"SPY": {"bars": bars, "quotes": quotes}})
        return result.get("SPY")

    def _live_signal(self, candidate: dict, bars: list[dict],
                     quotes: list[dict], now: datetime) -> dict | None:
        row = self._live_row(candidate, bars, quotes, now)
        if row is None:
            return None
        return generate_ibr_signal(
            "SPY", row["bars"], config=candidate["config"], now=now,
            session_state={"signals": set()})

    def _evaluate(self, candidate: dict, bars: list[dict], quotes: list[dict],
                  event: dict | None = None,
                  signal_sessions: dict[str, str] | None = None):
        selected = event or bars[-1]
        sessions = signal_sessions if signal_sessions is not None else {}
        self.runner._worker_state.signal_sessions = sessions
        try:
            return self.runner._evaluate(
                candidate, selected, {"SPY": bars}, {"SPY": quotes}, {})
        finally:
            del self.runner._worker_state.signal_sessions

    def _direct_pipeline(self, candidate: dict, bars: list[dict],
                         quotes: list[dict], now: datetime):
        row = self._live_row(candidate, bars, quotes, now)
        self.assertIsNotNone(row)
        signal = generate_ibr_signal(
            "SPY", row["bars"], config=candidate["config"], now=now,
            session_state={"signals": set()})
        self.assertIsNotNone(signal)
        signal = dict(signal)
        snapshot = {
            **row, "price": signal["entry_price"],
            "entry_price": signal["entry_price"], "close": signal["entry_price"],
            "relative_volume": signal["relative_volume"],
            "spread_bps": row["spread_bps"], "stale": False,
            "quote_stale": False, "signal_ts": signal["signal_ts"],
            "session": signal["session"],
            "ibr_range": {
                "high": signal["range_high"], "low": signal["range_low"],
                "width": signal["range_width"],
                "range_end_ts": signal["signal_ts"] - 60.0,
                "complete": True,
            },
        }
        setup, why = build_setup_plan(signal, snapshot, candidate["config"])
        self.assertIsNone(why)
        self.assertIsNotNone(setup)
        executable = dict(setup)
        executable["entry_price"] = float(
            row["quote"]["ask"] if signal["direction"] == "long"
            else row["quote"]["bid"])
        executable["execution_profile"] = "shares"
        market = {
            **row, "price": executable["entry_price"], "stale": False,
            "quote_stale": False,
        }
        risk_plan, why = RiskEngine(candidate["config"]).vet_open(
            executable, 100_000.0, [], {"SPY": market}, {}, 0.0,
            active_trades={}, now=now.timestamp(), cost_cfg=candidate["config"])
        self.assertIsNone(why)
        self.assertIsNotNone(risk_plan)
        return signal, setup, risk_plan, row

    def _calendar(self, *, close: datetime) -> None:
        opened = datetime.combine(
            close.date(), datetime.min.time(), tzinfo=UTC).replace(
                hour=13, minute=30)
        (self.root / INDEX_NAME).write_text(json.dumps({
            "session_calendar": {SESSION: {
                "open": opened.isoformat(), "close": close.isoformat(),
                "source": "alpaca_calendar",
            }},
        }), encoding="utf-8")

    def test_ibr_cohort_is_explicitly_opt_in_and_non_authorizing(self):
        default = build_diagnostic_cohort(
            self.runtime, code_identity="b" * 64)
        opted = self.cohort
        self.assertEqual(len(default["arms"]), 24)
        self.assertFalse(any(
            arm.get("strategy_id") == "ibr" for arm in default["arms"]))
        ibr_arms = [arm for arm in opted["arms"]
                    if arm.get("strategy_id") == "ibr"]
        self.assertEqual(len(opted["arms"]), 31)
        self.assertEqual(
            {arm["variant_id"] for arm in ibr_arms}, {
                "ibr.baseline", "ibr.range.30", "ibr.range.45",
                "ibr.target.1_5r", "ibr.target.3r", "ibr.buffer.0bps",
                "ibr.buffer.10bps",
            })
        required = {
            "min_relative_volume", "min_ibr_width_atr",
            "max_ibr_width_atr", "max_ibr_width_pct", "atr_period",
            "max_entry_extension_r", "stale_minutes", "max_spread_bps",
        }
        for arm in ibr_arms:
            with self.subTest(variant_id=arm["variant_id"]):
                strategy = arm["config"]["strategy"]
                self.assertTrue(required <= set(strategy))
                self.assertEqual(arm["vehicle"], "equity")
                self.assertTrue(arm["diagnostic_only"])
                self.assertFalse(arm["authorizing"])
                marker = arm["config"]["diagnostic_shadow"]
                self.assertFalse(marker["authorizing"])
                self.assertFalse(marker["gate_eligible"])
                self.assertFalse(marker["promotion_eligible"])
                parity = marker["runtime_parity"]
                self.assertTrue(parity["shared_signal_setup_risk"])
                self.assertEqual(parity["broker_only_equivalence"],
                                 "unsupported")
                self.assertEqual(set(parity["unsupported_observations"]), {
                    "shortability", "buying_power", "pending_orders",
                    "actual_fills",
                })

    def test_all_runtime_filters_and_close_relative_buffer_match_diagnostic(self):
        cases = (
            ("min_relative_volume", {"min_relative_volume": 2.1}, {}),
            ("min_ibr_width_atr", {"min_ibr_width_atr": 1.5}, {}),
            ("max_ibr_width_atr", {"max_ibr_width_atr": 0.5}, {}),
            ("max_ibr_width_pct", {"max_ibr_width_pct": 0.5}, {}),
            ("atr_period", {"atr_period": 30}, {}),
            ("max_entry_extension_r", {"max_entry_extension_r": 0.5}, {}),
            ("stale_minutes", {"stale_minutes": 1.0 / 60.0},
             {"observed_delay": 2.0}),
            ("max_spread_bps", {"max_spread_bps": 5.0}, {}),
            ("halt", {}, {"halt": True}),
        )
        for name, overrides, bar_options in cases:
            with self.subTest(filter=name):
                candidate = self._candidate(**overrides)
                bars = _opening_breakout(**bar_options)
                now = datetime.fromisoformat(bars[-1]["observed_at"])
                quotes = [_quote(now)]
                direct = self._live_signal(candidate, bars, quotes, now)
                sessions: dict[str, str] = {}
                kind, reason, payload, plan = self._evaluate(
                    candidate, bars, quotes, signal_sessions=sessions)
                self.assertIsNone(direct)
                self.assertEqual(sessions, {})
                self.assertEqual((kind, reason, plan),
                                 ("no_trade", "no signal", None))
                self.assertIsNone(payload["signal"])

        for close, expected in ((100.5501, False), (100.551, True)):
            with self.subTest(buffer_close=close):
                candidate = self._candidate(max_entry_extension_r=2.0)
                bars = _opening_breakout(close=close)
                now = datetime.fromisoformat(bars[-1]["observed_at"])
                quotes = [_quote(now, bid=100.50, ask=100.55)]
                direct = self._live_signal(candidate, bars, quotes, now)
                kind, _reason, payload, _plan = self._evaluate(
                    candidate, bars, quotes)
                self.assertEqual(direct is not None, expected)
                self.assertEqual(payload.get("signal") is not None, expected,
                                 (kind, payload))

    def test_late_observed_bar_is_invisible_until_its_recorded_availability(self):
        candidate = self._candidate(stale_minutes=5.0)
        bars = _opening_breakout(close=100.52)
        early = bars[-1]
        early_at = datetime.fromisoformat(early["observed_at"])
        late_stamp = SESSION_OPEN + timedelta(minutes=16)
        late = _bar(
            late_stamp, opened=100.5, high=101.4, low=100.4, close=101.2,
            volume=2000.0,
            observed_at=late_stamp + timedelta(minutes=2), key="bar:late")
        all_bars = [*bars, late]
        early_quotes = [_quote(early_at, bid=100.48, ask=100.56)]
        visible = [row for row in all_bars if _available(row, early_at)]
        self.assertIsNone(self._live_signal(
            candidate, visible, early_quotes, early_at))
        first = self._evaluate(candidate, all_bars, early_quotes, early)
        self.assertEqual((first[0], first[1], first[3]),
                         ("no_trade", "no signal", None))
        self.assertIsNone(first[2]["signal"])

        late_at = datetime.fromisoformat(late["observed_at"])
        late_quotes = [*early_quotes, _quote(late_at)]
        visible = [row for row in all_bars if _available(row, late_at)]
        self.assertIsNotNone(self._live_signal(
            candidate, visible, late_quotes, late_at))
        second = self._evaluate(candidate, all_bars, late_quotes, late)
        self.assertIsNotNone(second[2].get("signal"), second)

    def test_stale_quote_does_not_consume_the_live_signal_session(self):
        fresh_bars = _opening_breakout()
        event_at = datetime.fromisoformat(fresh_bars[-1]["observed_at"])
        stale_quote = _quote(event_at - timedelta(seconds=31))
        self.assertIsNone(self._live_row(
            self.arm, fresh_bars, [stale_quote], event_at))
        stale_sessions: dict[str, str] = {}
        stale = self._evaluate(
            self.arm, fresh_bars, [stale_quote],
            signal_sessions=stale_sessions)
        self.assertEqual((stale[0], stale[1], stale[3]),
                         ("unpriced", "stale or unavailable quote", None))
        self.assertEqual(stale_sessions, {})

    def test_exact_latest_entry_and_force_flat_cutoffs_match_runtime(self):
        self._calendar(close=datetime(2026, 9, 8, 20, 0, tzinfo=UTC))
        cutoff = self._candidate(latest_entry_time="09:46")
        cutoff["config"]["session"]["require_exact_calendar"] = True
        exact_bars = _opening_breakout(observed_delay=0.0)
        exact_at = datetime.fromisoformat(exact_bars[-1]["observed_at"])
        exact_quotes = [_quote(exact_at)]
        self.assertTrue(_CutoffProbe(cutoff["config"])._latest_entry_allowed(
            exact_at, cutoff["config"]))
        self.assertIsNotNone(self._live_signal(
            cutoff, exact_bars, exact_quotes, exact_at))
        exact = self._evaluate(cutoff, exact_bars, exact_quotes)
        self.assertNotEqual(exact[1], "session entry cutoff reached", exact)
        self.assertIsNotNone(exact[2].get("signal"), exact)

        flat = self._candidate(
            latest_entry_time="15:59", force_flat_minutes_before_close=10)
        flat["config"]["session"]["require_exact_calendar"] = True
        flat_at = datetime(2026, 9, 8, 19, 50, tzinfo=UTC)
        flat_event = _bar(
            flat_at - timedelta(minutes=1), close=101.2,
            observed_at=flat_at, key="bar:flat-cutoff")
        blocked = self._evaluate(
            flat, [*exact_bars[:-1], flat_event], [_quote(flat_at)], flat_event)
        self.assertEqual((blocked[0], blocked[1], blocked[3]),
                         ("no_trade", "session force-flat cutoff reached", None))

    def test_authored_bracket_then_executable_quote_matches_shared_risk(self):
        for direction, close, bid, ask in (
                ("long", 101.2, 101.2, 101.4),
                ("short", 98.8, 98.6, 98.8)):
            with self.subTest(direction=direction):
                candidate = self._candidate()
                bars = _opening_breakout(close=close)
                now = datetime.fromisoformat(bars[-1]["observed_at"])
                quotes = [_quote(now, bid=bid, ask=ask)]
                signal, setup, expected, live_row = self._direct_pipeline(
                    candidate, bars, quotes, now)
                self.assertEqual(signal["direction"], direction)
                kind, reason, payload, actual = self._evaluate(
                    candidate, bars, quotes)
                self.assertEqual((kind, reason), (
                    "open_incomplete", "virtual open; fills and P&L incomplete"),
                    payload)
                self.assertIsNotNone(actual)
                executable = (live_row["quote"]["ask"] if direction == "long"
                              else live_row["quote"]["bid"])
                self.assertAlmostEqual(payload["signal"]["entry_price"],
                                       signal["entry_price"])
                self.assertAlmostEqual(payload["setup_plan"]["entry_price"],
                                       executable)
                self.assertAlmostEqual(payload["setup_plan"]["stop_price"],
                                       setup["stop_price"])
                self.assertAlmostEqual(payload["setup_plan"]["target_price"],
                                       setup["target_price"])
                self.assertAlmostEqual(actual["authored_entry_reference"],
                                       signal["entry_price"])
                self.assertAlmostEqual(actual["executable_entry_reference"],
                                       executable)
                for field in (
                        "entry_price", "stop_price", "target_price",
                        "stop_distance", "shares", "contracts", "risk_usd",
                        "notional", "authored_stop_price",
                        "authored_target_price", "broker_normalized_stop_price",
                        "broker_normalized_target_price",
                        "stressed_cost_scenario_bps", "stressed_cost_usd",
                        "stressed_cost_to_risk_ratio"):
                    with self.subTest(direction=direction, field=field):
                        self.assertAlmostEqual(float(actual[field]),
                                               float(expected[field]), places=10)
                self.assertAlmostEqual(actual["stop_price"], setup["stop_price"])
                self.assertAlmostEqual(actual["target_price"],
                                       setup["target_price"])

    def test_signal_session_persists_before_rejection_and_survives_retry(self):
        candidate = self._candidate(
            max_entry_extension_r=10.0, min_ibr_width_atr=0.0,
            max_ibr_width_atr=100.0, max_ibr_width_pct=10.0)
        bars = _opening_breakout(
            close=100.07, range_high=100.01, range_low=99.99)
        first_event = bars[-1]
        first_at = datetime.fromisoformat(first_event["observed_at"])
        quotes = [_quote(first_at, bid=100.06, ask=100.08)]
        candidate_id = str(candidate["candidate_id"])
        cohort_id = str(candidate["cohort_identity"])
        self.runner.store.seed_diagnostic_accounts(
            cohort_identity=cohort_id, candidate_ids=[candidate_id],
            starting_cash=100_000.0)
        initial = self.runner.store.diagnostic_account_snapshot(
            cohort_identity=cohort_id, candidate_id=candidate_id)
        first = self.runner._evaluate_diagnostic_arm_snapshot(
            candidate, {SESSION: [first_event]},
            {SESSION: (bars, quotes, ())}, {"SPY": bars},
            {"SPY": quotes}, {}, initial)
        self.assertIsNone(first["error"], first)
        self.assertEqual(len(first["decisions"]), 1)
        decision = first["decisions"][0]
        self.assertIsNotNone(decision["payload"].get("signal"), decision)
        self.assertEqual((decision["kind"], decision["reason"], decision["plan"]),
                         ("reject", "stressed_cost_risk_limit", None))
        sessions = first["account_batch"]["account"]["state"].get(
            "signal_sessions")
        self.assertIsInstance(sessions, dict)
        self.assertIn(SESSION, sessions.values())
        committed = self.runner.store.record_diagnostic_batch(
            cohort_identity=cohort_id, candidate_id=candidate_id,
            cursor_inserted_at=1.0, cursor_event_key=first_event["event_key"],
            processed_events=1, rollups={SESSION: {}}, pending_sessions=[],
            decisions=first["decisions"], warmup_session=None,
            max_decisions=self.runner.config.max_decisions,
            account_batch=first["account_batch"])
        self.assertEqual(committed, 1)
        self.assertEqual(self.runner.store.record_diagnostic_batch(
            cohort_identity=cohort_id, candidate_id=candidate_id,
            cursor_inserted_at=1.0, cursor_event_key=first_event["event_key"],
            processed_events=1, rollups={SESSION: {}}, pending_sessions=[],
            decisions=first["decisions"], warmup_session=None,
            max_decisions=self.runner.config.max_decisions,
            account_batch=first["account_batch"]), 0)

        later_stamp = SESSION_OPEN + timedelta(minutes=16)
        later = _bar(
            later_stamp, opened=100.06, high=100.12, low=100.05,
            close=100.08, volume=2200,
            observed_at=later_stamp + timedelta(minutes=1, seconds=5),
            key="bar:retry")
        later_at = datetime.fromisoformat(later["observed_at"])
        later_quotes = [*quotes, _quote(later_at, bid=100.07, ask=100.09)]
        restarted = ShadowRunner(ShadowConfig(
            self.corpus, self.edge, self.shadow, diagnostic=True,
            diagnostic_include_ibr=True,
            runtime_config=deepcopy(self.runtime),
            runtime_config_path="/mounted/config.yaml",
            max_events=500, max_decisions=5000, max_workers=1,
            diagnostic_session_max_events=500))
        saved = restarted.store.diagnostic_account_snapshot(
            cohort_identity=cohort_id, candidate_id=candidate_id)
        second = restarted._evaluate_diagnostic_arm_snapshot(
            candidate, {SESSION: [later]},
            {SESSION: ([*bars, later], later_quotes, ())},
            {"SPY": [*bars, later]}, {"SPY": later_quotes}, {}, saved)
        self.assertIsNone(second["error"], second)
        self.assertEqual(len(second["decisions"]), 1)
        repeat = second["decisions"][0]
        self.assertEqual((repeat["kind"], repeat["reason"], repeat["plan"]),
                         ("no_trade", "no signal", None))
        self.assertIsNone(repeat["payload"]["signal"])

    def test_signal_session_is_consumed_before_setup_rejection(self):
        candidate = self._candidate()
        candidate["candidate_id"] += ":setup-reject"
        candidate["config"]["strategy"]["variant_id"] = "ibr.unregistered"
        bars = _opening_breakout()
        first = bars[-1]
        first_at = datetime.fromisoformat(first["observed_at"])
        later_stamp = SESSION_OPEN + timedelta(minutes=16)
        later = _bar(
            later_stamp, opened=101.2, high=101.5, low=101.1,
            close=101.3, volume=2200.0,
            observed_at=later_stamp + timedelta(minutes=1, seconds=5),
            key="bar:after-setup-reject")
        later_at = datetime.fromisoformat(later["observed_at"])
        quotes = [
            _quote(first_at, bid=101.2, ask=101.4, key="quote:first"),
            _quote(later_at, bid=101.3, ask=101.5, key="quote:later"),
        ]
        candidate_id = str(candidate["candidate_id"])
        cohort_id = str(candidate["cohort_identity"])
        self.runner.store.seed_diagnostic_accounts(
            cohort_identity=cohort_id, candidate_ids=[candidate_id],
            starting_cash=100_000.0)
        initial = self.runner.store.diagnostic_account_snapshot(
            cohort_identity=cohort_id, candidate_id=candidate_id)
        result = self.runner._evaluate_diagnostic_arm_snapshot(
            candidate, {SESSION: [first, later]},
            {SESSION: ([*bars, later], quotes, ())},
            {"SPY": [*bars, later]}, {"SPY": quotes}, {}, initial)
        self.assertIsNone(result["error"], result)
        self.assertEqual(len(result["decisions"]), 2)
        rejected, duplicate = result["decisions"]
        self.assertEqual(rejected["kind"], "reject")
        self.assertIn("strategy contract mismatch", rejected["reason"])
        self.assertIsNotNone(rejected["payload"].get("signal"))
        self.assertEqual((duplicate["kind"], duplicate["reason"],
                          duplicate["plan"]),
                         ("no_trade", "no signal", None))
        self.assertIsNone(duplicate["payload"]["signal"])
        sessions = result["account_batch"]["account"]["state"][
            "signal_sessions"]
        self.assertEqual(sessions, {"SPY": SESSION})

    def _book(self, candidate: dict) -> DiagnosticAccountBook:
        return DiagnosticAccountBook(
            account=new_account_state(
                cohort_identity="cohort", candidate_id="ibr-arm",
                starting_cash=100_000.0),
            positions=[], config=candidate["config"],
            policy=ReplayPolicy.from_config(candidate["config"]),
            rule_spec={})

    def test_ibr_tie_gap_and_force_flat_have_no_invented_rule_hold(self):
        candidate = self._candidate()
        bars = _opening_breakout()
        entry_event = bars[-1]
        entry_at = datetime.fromisoformat(entry_event["observed_at"])
        entry_quotes = [_quote(entry_at, bid=101.2, ask=101.4)]
        _signal, setup, plan, _row = self._direct_pipeline(
            candidate, bars, entry_quotes, entry_at)
        self.assertIsNone(setup.get("max_hold_bars"))
        self.assertIsNone(plan.get("max_hold_bars"))

        tie_book = self._book(candidate)
        self.assertTrue(tie_book.open_requested_position(
            event=entry_event, plan=plan, quote_rows=entry_quotes))
        tie = _bar(
            SESSION_OPEN + timedelta(minutes=17), opened=101.4,
            high=float(plan["target_price"]) + 0.5,
            low=float(plan["stop_price"]) - 0.5, close=101.0,
            key="bar:tie")
        tie_book.advance_completed_bar(tie, quote_rows=[])
        tie_position = next(iter(tie_book.positions.values()))
        self.assertEqual(tie_position["exit_reason"], "stop")
        self.assertTrue(tie_position["tie_broken"])

        gap_book = self._book(candidate)
        self.assertTrue(gap_book.open_requested_position(
            event=entry_event, plan=plan, quote_rows=entry_quotes))
        gap_stamp = SESSION_OPEN + timedelta(minutes=17)
        gap = _bar(
            gap_stamp, opened=float(plan["stop_price"]) - 0.5,
            high=float(plan["stop_price"]) - 0.1,
            low=float(plan["stop_price"]) - 1.0,
            close=float(plan["stop_price"]) - 0.6, key="bar:gap")
        gap_quote = _quote(
            gap_stamp, bid=float(plan["stop_price"]) - 0.55,
            ask=float(plan["stop_price"]) - 0.45)
        gap_book.advance_completed_bar(gap, quote_rows=[gap_quote])
        gap_position = next(iter(gap_book.positions.values()))
        self.assertEqual(gap_position["exit_reason"], "stop")
        self.assertTrue(gap_position["gap_fill"])
        self.assertAlmostEqual(gap_position["exit_reference"],
                               gap_quote["bid"])

        flat_book = self._book(candidate)
        self.assertTrue(flat_book.open_requested_position(
            event=entry_event, plan=plan, quote_rows=entry_quotes))
        force_flat = datetime.fromtimestamp(float(plan["force_flat_ts"]), UTC)
        flat_bar = _bar(
            force_flat, opened=101.5, high=101.6, low=101.4, close=101.5,
            key="bar:force-flat")
        flat_quote = _quote(force_flat, bid=101.45, ask=101.55)
        flat_book.advance_completed_bar(flat_bar, quote_rows=[flat_quote])
        flat_position = next(iter(flat_book.positions.values()))
        self.assertEqual(flat_position["exit_reason"], "session_force_flat")
        self.assertNotEqual(flat_position["exit_reason"], "max_hold")
        self.assertEqual(flat_position["deadline"]["reason"],
                         "session_force_flat")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
