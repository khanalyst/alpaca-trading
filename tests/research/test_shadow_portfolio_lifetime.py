"""Causal lifetime checks for legacy authorizing shadow portfolios."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.risk import RiskEngine
from research.costs import ReplayPolicy
from research.live_shadow import ShadowConfig, ShadowRunner, _safe_config
from research.quote_costs import cost_resolver_setup


UTC = timezone.utc


class ShadowPortfolioLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runner = ShadowRunner(ShadowConfig(
            root / "recorded.csv", root / "edge.sqlite3", root / "shadow.sqlite3"))
        self.base = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
        self.policy = ReplayPolicy(
            equity_feed="iex", equity_provider="alpaca",
            strict_market_data=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, symbol: str, minute: int) -> dict:
        opened = self.base + timedelta(minutes=minute)
        available = opened + timedelta(minutes=1)
        return {
            "event_key": f"event-{symbol}-{minute}",
            "event_type": "bar_1m",
            "symbol": symbol,
            "timestamp": opened.isoformat(),
            "as_of": available.isoformat(),
            "observed_at": available.isoformat(),
            "provider": "alpaca",
            "feed": "iex",
            "source_mode": "forward_observed",
            "open": "100",
            "high": "100.5",
            "low": "99.5",
            "close": "100",
        }

    def _exit_bar(self, symbol: str, minute: int) -> dict:
        row = self._event(symbol, minute)
        row.update({"high": "102", "low": "99.5", "close": "101"})
        return row

    def _plan(self, symbol: str, minute: int) -> dict:
        entry = self.base + timedelta(minutes=minute, seconds=1)
        return {
            "symbol": symbol,
            "session": "2026-01-02",
            "direction": "long",
            "entry_price": 100.0,
            "stop_price": 99.0,
            "target_price": 101.0,
            "notional": 50_000.0,
            "risk_usd": 500.0,
            "entry_timestamp": entry.isoformat(),
        }

    def _quote(self, symbol: str, stamp: datetime, *, observed_at: datetime | None = None) -> dict:
        observed = observed_at or stamp
        return {
            "event_key": f"quote-{symbol}-{stamp.isoformat()}",
            "event_type": "quote",
            "symbol": symbol,
            "timestamp": stamp.isoformat(),
            "as_of": stamp.isoformat(),
            "observed_at": observed.isoformat(),
            "provider": "alpaca",
            "feed": "iex",
            "source_mode": "forward_observed",
            "bid": "99.5",
            "ask": "100.5",
        }

    def test_future_exit_bar_does_not_release_until_available(self):
        plan = self._plan("AAA", 0)
        state = ([plan], {"AAA": dict(plan)}, 50_000.0)
        future_bar = self._exit_bar("AAA", 1)
        before_exit = self._event("BBB", 0)

        before = ShadowRunner._causal_worker_state(
            state, event=before_exit, bars={"AAA": (future_bar,)},
            policy=self.policy)
        self.assertEqual(len(before[0]), 1)
        self.assertEqual(before[2], 50_000.0)

        after_exit = self._event("BBB", 2)
        after = ShadowRunner._causal_worker_state(
            state, event=after_exit, bars={"AAA": (future_bar,)},
            policy=self.policy)
        self.assertEqual(after[0], [])
        self.assertEqual(after[1], {})
        self.assertEqual(after[2], 0.0)

    def test_overlapping_position_remains_in_risk_projection(self):
        plan = self._plan("AAA", 0)
        state = ([plan], {"AAA": dict(plan)}, 50_000.0)
        current = self._event("BBB", 1)
        future_exit = self._exit_bar("AAA", 3)

        projected = ShadowRunner._causal_worker_state(
            state, event=current, bars={"AAA": (future_exit,)},
            policy=self.policy)
        self.assertEqual([row["symbol"] for row in projected[0]], ["AAA"])
        self.assertEqual(set(projected[1]), {"AAA"})
        self.assertEqual(projected[2], 50_000.0)

    def test_authorizing_projection_releases_nonoverlap_and_keeps_session_symbol(self):
        events = {
            "2026-01-02": (
                self._event("AAA", 0),
                self._event("BBB", 2),
                self._event("CCC", 4),
            ),
        }
        bars = {
            "AAA": (self._exit_bar("AAA", 1),),
            "BBB": (self._exit_bar("BBB", 3),),
            "CCC": (),
        }
        plans = {symbol: self._plan(symbol, minute)
                 for symbol, minute in (("AAA", 0), ("BBB", 2), ("CCC", 4))}
        calls = []

        def evaluate(_arm, event, *_args):
            calls.append((event["symbol"], self.runner._portfolio_state("candidate")))
            plan = plans[event["symbol"]]
            return "open_incomplete", "open", {}, plan

        arm = {
            "candidate_id": "candidate",
            "strategy_id": "ibr",
            "variant_id": "ibr.baseline",
            "config": {"strategy": {"id": "ibr", "version": "v1",
                                      "variant_id": "ibr.baseline"},
                        "risk": {"risk_per_trade_pct": 0.5},
                        "execution": {}, "session": {}},
        }
        with patch.object(self.runner, "_evaluate", side_effect=evaluate):
            result = self.runner._evaluate_arm_snapshot(
                arm, events, {"2026-01-02": ((), (), ())}, bars, {}, {},
                ([], {}, 0.0))

        self.assertIsNone(result["error"])
        self.assertEqual([row["kind"] for row in result["decisions"]],
                         ["open_incomplete"] * 3)
        self.assertEqual([symbol for symbol, _state in calls],
                         ["AAA", "BBB", "CCC"])
        self.assertEqual([len(state[0]) for _symbol, state in calls], [0, 0, 0])

    def test_same_symbol_is_rejected_after_causal_release(self):
        events = {"2026-01-02": (self._event("AAA", 0),
                                 self._event("AAA", 2))}
        bars = {"AAA": (self._exit_bar("AAA", 1),)}
        plan = self._plan("AAA", 0)
        calls = []

        def evaluate(_arm, _event, *_args):
            calls.append(True)
            return "open_incomplete", "open", {}, dict(plan)

        arm = {
            "candidate_id": "candidate",
            "strategy_id": "ibr",
            "variant_id": "ibr.baseline",
            "config": {"strategy": {"id": "ibr", "version": "v1",
                                      "variant_id": "ibr.baseline"},
                        "risk": {"risk_per_trade_pct": 0.5},
                        "execution": {}, "session": {}},
        }
        with patch.object(self.runner, "_evaluate", side_effect=evaluate):
            result = self.runner._evaluate_arm_snapshot(
                arm, events, {"2026-01-02": ((), (), ())}, bars, {}, {},
                ([], {}, 0.0))

        self.assertIsNone(result["error"])
        self.assertEqual(calls, [True])
        self.assertEqual([row["kind"] for row in result["decisions"]],
                         ["open_incomplete", "no_trade"])
        self.assertEqual(result["decisions"][1]["reason"],
                         "virtual book has an incomplete open")

    def test_deadline_release_requires_fresh_quote_visible_by_cutoff(self):
        plan = self._plan("AAA", 0)
        deadline = self.base + timedelta(minutes=2)
        plan.update({
            "target_price": 120.0,
            "hold_deadline_ts": deadline.timestamp(),
        })
        bars = tuple(self._event("AAA", minute) for minute in (1, 2, 3))
        event = self._event("BBB", 3)  # availability cutoff is base + 4m
        future_quote = self._quote(
            "AAA", self.base + timedelta(minutes=3, seconds=40),
            observed_at=self.base + timedelta(minutes=5))
        blocked = ShadowRunner._causal_worker_state(
            ([plan], {"AAA": dict(plan)}, 50_000.0), event=event,
            bars={"AAA": bars}, quotes={"AAA": (future_quote,)},
            policy=self.policy)
        self.assertEqual(len(blocked[0]), 1)
        self.assertEqual(blocked[2], 50_000.0)

        fresh_quote = dict(future_quote)
        fresh_quote["observed_at"] = (
            self.base + timedelta(minutes=3, seconds=45)).isoformat()
        released = ShadowRunner._causal_worker_state(
            ([plan], {"AAA": dict(plan)}, 50_000.0), event=event,
            bars={"AAA": bars}, quotes={"AAA": (fresh_quote,)},
            policy=self.policy)
        self.assertEqual(released, ([], {}, 0.0))

    def test_deadline_prices_at_requested_cutoff_before_later_visible_quote(self):
        plan = self._plan("AAA", 0)
        deadline = self.base + timedelta(minutes=2)
        plan.update({"hold_deadline_ts": deadline.timestamp(),
                     "target_price": 120.0})
        first = self._quote("AAA", deadline)
        first.update({"bid": "99", "ask": "100"})
        later = self._quote(
            "AAA", self.base + timedelta(minutes=4, seconds=50))
        later.update({"bid": "80", "ask": "81"})
        event = self._event("BBB", 5)
        actual_cash = {"candidate": 100_000.0}
        actual_equity = {"candidate": 100_000.0}
        actual_marks = {"candidate": {}}
        projected = ShadowRunner._causal_worker_state(
            ([plan], {"AAA": dict(plan)}, 50_000.0), event=event,
            bars={"AAA": tuple(self._event("AAA", minute)
                                for minute in (1, 2, 3))},
            quotes={"AAA": (first, later)}, policy=self.policy,
            config={"costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                               "fee_bps": 0.5}},
            candidate_id="candidate", cash_state=actual_cash,
            equity_state=actual_equity, marks_state=actual_marks)
        expected_pnl = ShadowRunner._worker_exit_pnl(
            plan, {"reason": "time", "gapped": True,
                   "requested_at": deadline.timestamp()},
            quote_rows=(first,), visible_by=event["observed_at"] and
            datetime.fromisoformat(event["observed_at"]), policy=self.policy,
            config={"costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                               "fee_bps": 0.5}})
        self.assertEqual(projected, ([], {}, 0.0))
        self.assertIsNotNone(expected_pnl)
        self.assertAlmostEqual(actual_cash["candidate"],
                               100_000.0 + expected_pnl, places=9)
        later_pnl = ShadowRunner._worker_exit_pnl(
            plan, {"reason": "time", "gapped": True,
                   "requested_at": deadline.timestamp()},
            quote_rows=(later,), visible_by=datetime.fromisoformat(
                event["observed_at"]), policy=self.policy,
            config={"costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                               "fee_bps": 0.5}})
        self.assertNotEqual(expected_pnl, later_pnl)

    def test_legacy_entry_uses_bar_reference_and_explicit_quote_uses_quote_cost(self):
        resolved = {"reason": "target", "price": 101.0,
                    "bar_end_epoch": (self.base + timedelta(minutes=1)).timestamp()}
        config = {"costs": {"spread_bps": 4.0, "slippage_bps": 6.0,
                             "fee_bps": 0.5}}
        plan = self._plan("AAA", 0)
        actual = ShadowRunner._worker_exit_pnl(
            plan, resolved, quote_rows=(), visible_by=self.base,
            policy=self.policy, config=config)
        setup = cost_resolver_setup(config, vehicle="equity")
        model = setup.model
        entry = model.execution_price(100.0, "long", entry=True,
                                      executable_quote=False)
        exit_price = model.execution_price(101.0, "long", entry=False,
                                           executable_quote=False)
        expected = ((exit_price - entry) * 500.0 -
                    model.fees(entry, entry, 500.0, vehicle="equity") / 2.0 -
                    model.fees(exit_price, exit_price, 500.0,
                               vehicle="equity") / 2.0)
        self.assertAlmostEqual(actual, expected, places=9)

        quoted = dict(plan, entry_price=100.1, entry_reference=100.1,
                      executable_entry_reference=100.1,
                      entry_fill_source="quote", shares=500.0)
        quoted_actual = ShadowRunner._worker_exit_pnl(
            quoted, resolved, quote_rows=(), visible_by=self.base,
            policy=self.policy, config=config)
        quoted_entry = model.execution_price(100.1, "long", entry=True,
                                             executable_quote=True)
        quoted_expected = ((exit_price - quoted_entry) * 500.0 -
                           model.fees(quoted_entry, quoted_entry, 500.0,
                                      vehicle="equity") / 2.0 -
                           model.fees(exit_price, exit_price, 500.0,
                                      vehicle="equity") / 2.0)
        self.assertAlmostEqual(quoted_actual, quoted_expected, places=9)
        self.assertNotEqual(actual, quoted_actual)

    def test_mark_falls_back_to_exact_visible_completed_bar(self):
        plan = self._plan("AAA", 0)
        marked = self._event("AAA", 1)
        marked.update({"close": "101", "open": "100.5"})
        at = self.base + timedelta(minutes=3)
        result = ShadowRunner._worker_mark_pnl(
            plan, quote_rows=(), bar_rows=(marked,), at=at,
            policy=self.policy, config={"costs": {"spread_bps": 0.0,
                                                   "slippage_bps": 0.0,
                                                   "fee_bps": 0.0}})
        self.assertEqual(result, 500.0)
        delayed = dict(marked)
        delayed["observed_at"] = (self.base + timedelta(minutes=4)).isoformat()
        self.assertIsNone(ShadowRunner._worker_mark_pnl(
            plan, quote_rows=(), bar_rows=(delayed,), at=at,
            policy=self.policy, config={"costs": {"spread_bps": 0.0,
                                                   "slippage_bps": 0.0,
                                                   "fee_bps": 0.0}}))

    def test_authorizing_evaluate_records_executable_quote_geometry(self):
        event = self._event("AAA", 1)
        previous = self._event("AAA", 0)
        quote_at = datetime.fromisoformat(event["observed_at"])
        quote = self._quote("AAA", quote_at)
        quote.update({"bid": "100.5", "ask": "101.0"})
        signal = {
            "symbol": "AAA", "direction": "long", "setup_type": "ibr_breakout",
            "signal_ts": (self.base + timedelta(minutes=1)).timestamp(),
            "entry_price": 100.0, "stop_price": 98.0,
            "target_price": 104.0, "stop_distance": 2.0,
            "target_r": 2.0, "execution_profile": "shares",
        }
        setup_plan = dict(signal)
        setup_plan["entry_price"] = 100.0
        setup_plan["stop_distance"] = 2.0
        setup_plan["shares"] = 1.0
        setup_plan["notional"] = 100.0
        setup_plan["risk_usd"] = 2.0
        def risk(_engine, decision, *_args, **_kwargs):
            return dict(decision), None
        arm = {
            "candidate_id": "ordinary-shadow-candidate",
            "strategy_id": "ibr", "variant_id": "ibr.baseline",
            "status": "backtest_passed",
            "config": {
                "broker": {"provider": "alpaca", "data_feed": "iex"},
                "strategy": {"id": "ibr", "version": "v1",
                              "variant_id": "ibr.baseline"},
                "risk": {"risk_per_trade_pct": 0.5},
                "execution": {"max_market_data_age_seconds": 30,
                              "max_spread_bps": 100, "max_slippage_bps": 100},
                "session": {"timezone": "America/New_York"},
            },
        }
        with patch("research.live_shadow.generate_ibr_signal",
                   return_value=signal), patch(
                       "research.live_shadow.build_setup_plan",
                       return_value=(setup_plan, None)), patch(
                           "research.live_shadow.RiskEngine.vet_open",
                           autospec=True, side_effect=risk):
            kind, reason, _payload, plan = self.runner._evaluate(
                arm, event, {"AAA": [previous, event]}, {"AAA": [quote]}, {})
        self.assertEqual((kind, reason), ("open_incomplete",
                                          "virtual open; fills and P&L incomplete"))
        self.assertEqual(plan["entry_price"], 101.0)
        self.assertEqual(plan["entry_reference"], 101.0)
        self.assertEqual(plan["entry_fill_source"], "quote")
        self.assertEqual(plan["authored_entry_reference"], 100.0)
        self.assertEqual(plan["executable_entry_reference"], 101.0)
        self.assertAlmostEqual(plan["stop_distance"], 3.0)
        self.assertEqual(plan["stop_price"], 98.0)
        self.assertEqual(plan["target_price"], 104.0)

    def test_gap_exit_requires_fresh_quote_and_contiguous_prefix(self):
        plan = self._plan("AAA", 0)
        gap_bar = self._event("AAA", 1)
        gap_bar.update({"open": "102", "high": "102", "low": "101.5",
                        "close": "101.8"})
        event = self._event("BBB", 2)
        delayed = self._quote(
            "AAA", self.base + timedelta(minutes=2, seconds=30),
            observed_at=self.base + timedelta(minutes=4))
        blocked = ShadowRunner._causal_worker_state(
            ([plan], {"AAA": dict(plan)}, 50_000.0), event=event,
            bars={"AAA": (gap_bar,)}, quotes={"AAA": (delayed,)},
            policy=self.policy)
        self.assertEqual(len(blocked[0]), 1)

        available = dict(delayed)
        available["observed_at"] = (
            self.base + timedelta(minutes=2, seconds=40)).isoformat()
        released = ShadowRunner._causal_worker_state(
            ([plan], {"AAA": dict(plan)}, 50_000.0), event=event,
            bars={"AAA": (gap_bar,)}, quotes={"AAA": (available,)},
            policy=self.policy)
        self.assertEqual(released, ([], {}, 0.0))

        # A later visible bar cannot stand in for an unavailable middle bar.
        later = self._exit_bar("AAA", 3)
        gapped_prefix = ShadowRunner._causal_worker_state(
            ([plan], {"AAA": dict(plan)}, 50_000.0), event=self._event("BBB", 3),
            bars={"AAA": (gap_bar, later)}, policy=self.policy)
        self.assertEqual(len(gapped_prefix[0]), 1)

    def test_option_underlying_exit_does_not_release_virtual_position(self):
        plan = self._plan("AAA", 0)
        plan.update({"execution_profile": "options", "option_symbol": "AAA2601C100"})
        released_underlying = self._exit_bar("AAA", 1)
        projected = ShadowRunner._causal_worker_state(
            ([plan], {"AAA": dict(plan)}, 50_000.0), event=self._event("BBB", 2),
            bars={"AAA": (released_underlying,)}, policy=self.policy)
        self.assertEqual(len(projected[0]), 1)
        self.assertEqual(projected[2], 50_000.0)

    def test_missing_mark_sets_equity_unavailable_instead_of_defaulting(self):
        plan = self._plan("AAA", 0)
        equities = {"candidate": 100_000.0}
        cash = {"candidate": 100_000.0}
        marks = {"candidate": {}}
        projected = ShadowRunner._causal_worker_state(
            ([plan], {"AAA": dict(plan)}, 50_000.0),
            event=self._event("BBB", 1), bars={"AAA": ()}, quotes={},
            policy=self.policy, config={"risk": {}},
            equity_state=equities, candidate_id="candidate",
            cash_state=cash, marks_state=marks)
        self.assertEqual(len(projected[0]), 1)
        self.assertIsNone(equities["candidate"])

    def test_each_open_symbol_uses_its_own_bar_mark(self):
        plans = [self._plan(symbol, 0) for symbol in ("AAA", "BBB")]
        for plan in plans:
            plan["target_price"] = 120.0
        bar_a, bar_b = self._event("AAA", 1), self._event("BBB", 1)
        bar_a["close"], bar_b["close"] = "100.2", "100.4"
        cash, equity, marks = {"c": 100_000.0}, {}, {}
        projected = ShadowRunner._causal_worker_state(
            (plans, {p["symbol"]: dict(p) for p in plans}, 100_000.0),
            event=self._event("CCC", 2), bars={"AAA": (bar_a,), "BBB": (bar_b,)},
            policy=self.policy, config={"costs": {"spread_bps": 0.0,
                "slippage_bps": 0.0, "fee_bps": 0.0}},
            equity_state=equity, candidate_id="c", cash_state=cash,
            marks_state=marks)
        self.assertEqual(len(projected[0]), 2)
        self.assertAlmostEqual(marks["c"]["AAA"], 100.0)
        self.assertAlmostEqual(marks["c"]["BBB"], 200.0)
        self.assertAlmostEqual(equity["c"], 100_300.0)

    def test_realization_does_not_subtract_prior_unrealized_mark_from_cash(self):
        plan = self._plan("AAA", 0)
        cash, equity, marks = {"c": 100_000.0}, {}, {}
        cfg = {"costs": {"spread_bps": 0.0, "slippage_bps": 0.0,
                         "fee_bps": 0.0}}
        first = self._event("AAA", 1)
        first["close"] = "99.8"
        state = ShadowRunner._causal_worker_state(
            ([plan], {"AAA": dict(plan)}, 50_000.0),
            event=self._event("BBB", 1), bars={"AAA": (first,)},
            policy=self.policy, config=cfg, equity_state=equity,
            candidate_id="c", cash_state=cash, marks_state=marks)
        self.assertAlmostEqual(equity["c"], 99_900.0)
        self.assertEqual(cash["c"], 100_000.0)
        final = self._exit_bar("AAA", 2)
        state = ShadowRunner._causal_worker_state(
            state, event=self._event("BBB", 2),
            bars={"AAA": (first, final)}, policy=self.policy, config=cfg,
            equity_state=equity, candidate_id="c", cash_state=cash,
            marks_state=marks)
        self.assertEqual(state, ([], {}, 0.0))
        self.assertEqual(cash["c"], 100_500.0)
        self.assertEqual(equity["c"], 100_500.0)
        self.assertEqual(marks["c"], {})

    def test_durable_open_decision_blocks_same_symbol_after_poll_restart(self):
        self.runner.store.decision(
            candidate_id="candidate", event_key="prior-open",
            session_date="2026-01-02", symbol="AAA",
            kind="open_incomplete", reason="virtual open",
            payload={}, max_decisions=100)
        arm = {
            "candidate_id": "candidate",
            "strategy_id": "ibr",
            "variant_id": "ibr.baseline",
            "config": {"strategy": {"id": "ibr", "version": "v1",
                                      "variant_id": "ibr.baseline"},
                        "risk": {}, "execution": {}, "session": {}},
        }
        with patch.object(self.runner, "_evaluate") as evaluate:
            result = self.runner._evaluate_arm_snapshot(
                arm, {"2026-01-02": (self._event("AAA", 2),)},
                {"2026-01-02": ((), (), ())}, {}, {}, {},
                ([], {}, 0.0))
        evaluate.assert_not_called()
        self.assertIsNone(result["error"])
        self.assertEqual(result["decisions"][0]["kind"], "no_trade")
        self.assertEqual(result["decisions"][0]["reason"],
                         "virtual book has an incomplete open")

    def _risk_arm(self) -> dict:
        return {
            "candidate_id": "candidate",
            "strategy_id": "ibr",
            "variant_id": "ibr.baseline",
            "config": {
                "strategy": {"id": "ibr", "version": "v1",
                              "variant_id": "ibr.baseline"},
                "risk": {
                    "risk_per_trade_pct": 0.5,
                    "max_concurrent_positions": 3,
                    "max_position_notional_pct": 25.0,
                    "max_gross_exposure_pct": 50.0,
                },
                "execution": {"max_spread_bps": 100},
                "session": {},
            },
        }

    def _real_risk_evaluator(self, arm: dict):
        cfg = _safe_config(arm)
        risk = RiskEngine(cfg)
        observed_equities = []

        def evaluate(_arm, event, *_args):
            symbol = event["symbol"]
            observed_equities.append(
                self.runner._worker_state.equities["candidate"])
            positions, active, gross = self.runner._portfolio_state("candidate")
            decision = {
                "symbol": symbol,
                "direction": "long",
                "entry_price": 100.0,
                "stop_price": 99.0,
                "target_price": 101.0,
                "confidence": 1.0,
                "execution_profile": "shares",
                "entry_timestamp": event["timestamp"],
                "decision_timestamp": event["timestamp"],
            }
            market = {"price": 100.0, "stale": False,
                      "quote_stale": False, "spread_bps": 0.0}
            plan, why = risk.vet_open(
                decision, self.runner._worker_state.equities["candidate"],
                positions, {symbol: market}, {}, gross,
                active_trades=active, now=datetime.fromisoformat(
                    event["timestamp"]).timestamp())
            if plan is None:
                return "reject", why, {}, None
            return "open_incomplete", "actual risk accepted", {}, plan

        evaluate.observed_equities = observed_equities
        return evaluate

    def test_real_risk_engine_releases_nonoverlap_but_rejects_overlap_cap(self):
        arm = self._risk_arm()
        nonoverlap = {
            "2026-01-02": tuple(self._event(symbol, minute)
                                 for symbol, minute in (("AAA", 0), ("BBB", 2),
                                                        ("CCC", 4)))
        }
        exit_bars = {
            "AAA": (self._exit_bar("AAA", 1),),
            "BBB": (self._exit_bar("BBB", 3),),
            "CCC": (),
        }
        evaluator = self._real_risk_evaluator(arm)
        with patch.object(self.runner, "_evaluate", side_effect=evaluator):
            result = self.runner._evaluate_arm_snapshot(
                arm, nonoverlap, {"2026-01-02": ((), (), ())},
                exit_bars, {}, {}, ([], {}, 0.0))
        self.assertIsNone(result["error"])
        self.assertEqual([row["kind"] for row in result["decisions"]],
                         ["open_incomplete"] * 3)
        notionals = [row["plan"]["notional"]
                     for row in result["decisions"]]
        self.assertEqual(notionals[:2], [25_000.0, 25_000.0])
        self.assertGreater(notionals[2], 25_000.0)
        self.assertEqual(len(evaluator.observed_equities), 3)
        self.assertGreater(evaluator.observed_equities[1],
                           evaluator.observed_equities[0])

        overlap = {
            "2026-01-02": tuple(self._event(symbol, minute)
                                 for symbol, minute in (("AAA", 0), ("BBB", 1),
                                                        ("CCC", 2)))
        }
        evaluator = self._real_risk_evaluator(arm)
        with patch.object(self.runner, "_evaluate", side_effect=evaluator):
            result = self.runner._evaluate_arm_snapshot(
                arm, overlap, {"2026-01-02": ((), (), ())},
                {"AAA": (), "BBB": (), "CCC": ()},
                {"AAA": (
                    self._quote("AAA", self.base +
                                timedelta(minutes=1, seconds=30)),
                    self._quote("AAA", self.base +
                                timedelta(minutes=2, seconds=30)),
                ), "BBB": (
                    self._quote("BBB", self.base +
                                timedelta(minutes=2, seconds=30)),
                )},
                {}, ([], {}, 0.0))
        self.assertIsNone(result["error"])
        self.assertEqual([row["kind"] for row in result["decisions"]],
                         ["open_incomplete", "open_incomplete", "reject"])
        self.assertEqual(result["decisions"][-1]["reason"],
                         "max gross exposure cap reached")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
