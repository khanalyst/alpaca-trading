import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from agent import state
from agent.engine import Engine
from agent.exchange import (EntryLiquidityRejected, EntryOrderRejected)
from tests.helpers import valid_config


def open_decision(symbol, confidence):
    return {
        "action": "open", "symbol": symbol, "direction": "long",
        "confidence": confidence,
        "setup_type": "trend_continuation",
        "invalidation_anchor": "structure",
        "exit_policy": "fixed_rr",
        "execution_choice": "normal",
        "reasoning": "",
    }


def setup_snapshot(signal_ts):
    return {
        "price": 100,
        "trend_15m": "up",
        "trend_1h": "up",
        "trend_4h": "up",
        "atr_1h_pct": 1.0,
        "ema20_1h_dist_pct": 0.5,
        "swing_low_pct": 1.2,
        "swing_high_pct": 2.5,
        "range_pos_pct": 70,
        "relative_volume_1h": 1.2,
        "mom_1h_pct": 0.5,
        "mom_15m_pct": 0.2,
        "funding_rate_pct": 0.0,
        "signal_ts": signal_ts,
        "signal_1h_ts": signal_ts - 100,
        "fresh_breakout_long": False,
        "fresh_breakout_short": False,
        "price_stabilized_long": True,
        "price_stabilized_short": False,
    }


class SortedOpensTests(unittest.TestCase):
    def test_opens_are_ordered_by_descending_confidence(self):
        opens, conflicted = Engine._sorted_opens([
            open_decision("ETH/USDT:USDT", 0.7),
            open_decision("BTC/USDT:USDT", 0.9),
        ])
        self.assertEqual([d["symbol"] for d in opens],
                         ["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self.assertEqual(conflicted, [])

    def test_open_and_close_on_one_symbol_drops_the_open(self):
        close = {"action": "close", "symbol": "ETH/USDT:USDT",
                 "reasoning": "thesis broken"}
        keep = open_decision("BTC/USDT:USDT", 0.7)
        conflict = open_decision("ETH/USDT:USDT", 0.95)
        opens, conflicted = Engine._sorted_opens([close, conflict, keep])
        # The higher-confidence open loses: SYSTEM forbids closing a symbol
        # and re-entering (or reversing) it in the same reply, and the
        # engine enforces that instead of trusting the prompt.
        self.assertEqual(opens, [keep])
        self.assertEqual(conflicted, [conflict])


class SetupIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.st = {"recent_setups": {}}

    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    def test_same_setup_on_same_signal_candle_is_rejected(
            self, log_event, commit):
        snapshot = {
            "BTC/USDT:USDT": setup_snapshot(1_000),
        }
        first, why = self.engine._prepare_setup_decision(
            open_decision("BTC/USDT:USDT", 0.8), snapshot, self.st)
        duplicate, duplicate_why = self.engine._prepare_setup_decision(
            open_decision("BTC/USDT:USDT", 0.8), snapshot, self.st)

        self.assertIsNone(why)
        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(
            duplicate_why,
            "symbol already evaluated for this completed signal candle")

    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    def test_new_candle_is_allowed_until_semantic_cooldown_is_applied(
            self, log_event, commit):
        decision = open_decision("BTC/USDT:USDT", 0.8)
        first, _ = self.engine._prepare_setup_decision(
            decision, {"BTC/USDT:USDT": setup_snapshot(1_000)}, self.st)
        second, why = self.engine._prepare_setup_decision(
            decision, {"BTC/USDT:USDT": setup_snapshot(2_000)}, self.st)

        self.assertIsNone(why)
        self.assertNotEqual(first["setup_id"], second["setup_id"])

    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    def test_contract_rejection_is_also_remembered_for_the_signal_candle(
            self, log_event, commit):
        snapshot = {"BTC/USDT:USDT": setup_snapshot(1_000)}
        invalid = open_decision("BTC/USDT:USDT", 0.8)
        invalid["setup_type"] = "range_breakout"

        first, first_why = self.engine._prepare_setup_decision(
            invalid, snapshot, self.st)
        second, second_why = self.engine._prepare_setup_decision(
            open_decision("BTC/USDT:USDT", 0.8), snapshot, self.st)

        self.assertIsNone(first)
        self.assertEqual(
            first_why, "range_breakout evidence contract is not met")
        self.assertIsNone(second)
        self.assertEqual(
            second_why,
            "symbol already evaluated for this completed signal candle")
        record = next(iter(self.st["recent_setups"].values()))
        self.assertEqual(record["status"], "risk_rejected")


class SequentialOpenValidationTests(unittest.TestCase):
    @patch("agent.engine.market.market_snapshot")
    @patch("agent.engine.state.log_equity")
    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.load_state", return_value={"state": "RUNNING"})
    def test_multiple_pairs_are_validated_in_order_against_updated_exposure(
            self, load_state, commit, log_event, log_equity, market_snapshot):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.alerts = Mock()
        engine.ex = Mock()
        engine.ex.equity_usdt.return_value = 10_000
        engine.ex.positions.return_value = []
        engine.ex.transfers_since.return_value = (0, 2)
        engine._reconcile_positions = Mock(return_value=[])
        engine._startup_reconciled = True
        engine._manage_positions = Mock(return_value=[])
        engine._portfolio_view = Mock(return_value={})
        engine._journal_llm_input = Mock()
        engine._journal_llm_output = Mock()
        engine.universe = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        engine.universe_ts = time.time()
        market_snapshot.return_value = {
            "BTC/USDT:USDT": setup_snapshot(1_000),
            "ETH/USDT:USDT": setup_snapshot(1_000),
        }
        engine.llm = Mock()
        engine.llm.decide.return_value = [
            open_decision("ETH/USDT:USDT", 0.8),
            open_decision("BTC/USDT:USDT", 0.9),
        ]
        plans = {
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT", "direction": "long",
                "notional": 100,
            },
            "ETH/USDT:USDT": {
                "symbol": "ETH/USDT:USDT", "direction": "long",
                "notional": 200,
            },
        }
        seen = []

        def vet(decision, equity, positions, snapshot, cooldowns, gross,
                entry_feedback, entry_failures, active_trades):
            seen.append((
                decision["symbol"],
                [position["symbol"] for position in positions],
                gross,
            ))
            return {
                **plans[decision["symbol"]],
                **{
                    key: decision[key] for key in (
                        "strategy_id", "strategy_version", "setup_id",
                        "setup_key", "setup_type", "signal_ts", "exit_policy",
                        "invalidation_anchor",
                    )
                },
            }, None

        engine.risk = Mock()
        engine.risk.vet_open.side_effect = vet
        engine._execute_open = Mock(return_value=True)
        st = {
            "state": state.RUNNING,
            "equity_basis": state.EQUITY_BASIS,
            "high_water_mark": 10_000,
            "day_start_equity": 10_000,
            "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "last_ledger_ts": 1,
            "cooldowns": {},
            "entry_feedback": {},
            "entry_failures": {},
            "opened_at": {},
            "active_trades": {},
            "protection": {},
            "recent_setups": {},
        }

        engine.cycle(st)

        self.assertEqual(seen, [
            ("BTC/USDT:USDT", [], 0),
            ("ETH/USDT:USDT", ["BTC/USDT:USDT"], 100),
        ])
        self.assertEqual(engine._execute_open.call_count, 2)


class UniverseRefreshTests(unittest.TestCase):
    @patch("agent.engine.market.market_snapshot", return_value={})
    @patch("agent.engine.market.select_universe")
    @patch("agent.engine.state.log_equity")
    @patch("agent.engine.state.log_event")
    @patch("agent.engine.state.commit")
    def test_empty_universe_waits_for_refresh_instead_of_retrying_each_cycle(
            self, commit, log_event, log_equity, select_universe,
            market_snapshot):
        select_universe.return_value = (
            [],
            {"selected": [], "candidates": [
                {"symbol": "ORCL/USDT:USDT",
                 "reason": "insufficient_4h_history"},
            ]},
        )
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.alerts = Mock()
        engine.ex = Mock()
        engine.ex.equity_usdt.return_value = 10_000
        engine.ex.positions.return_value = []
        engine.ex.transfers_since.return_value = (0, 2)
        engine._reconcile_positions = Mock(return_value=[])
        engine._startup_reconciled = True
        engine._manage_positions = Mock(return_value=[])
        engine.universe = []
        engine.universe_ts = 0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        st = {
            "state": state.RUNNING,
            "equity_basis": state.EQUITY_BASIS,
            "high_water_mark": 10_000,
            "day_start_equity": 10_000,
            "day": today,
            "last_ledger_ts": 1,
            "cooldowns": {},
            "entry_feedback": {},
            "entry_failures": {},
            "opened_at": {},
            "active_trades": {},
            "protection": {},
        }

        engine.cycle(st)
        engine.cycle(st)

        select_universe.assert_called_once_with(engine.ex, engine.cfg)
        universe_events = [
            call for call in log_event.call_args_list
            if call.args and call.args[0] == "universe_selection"
        ]
        self.assertEqual(len(universe_events), 1)
        self.assertEqual(market_snapshot.call_count, 2)


class PositionMetricTests(unittest.TestCase):
    def test_non_finite_ccxt_notional_uses_contract_fallback(self):
        engine = Engine.__new__(Engine)
        engine.ex = Mock()
        engine.ex.x.market.return_value = {"contractSize": 0.1}
        position = {
            "symbol": "BTC/USDT:USDT", "notional": float("nan"),
            "contracts": 2, "markPrice": 100,
        }
        self.assertEqual(engine._notional(position), 20)

    def test_completely_invalid_position_notional_returns_zero(self):
        engine = Engine.__new__(Engine)
        engine.ex = Mock()
        engine.ex.x.market.return_value = {"contractSize": 0.1}
        position = {
            "symbol": "BTC/USDT:USDT", "notional": float("nan"),
            "contracts": float("nan"), "markPrice": 100,
        }
        self.assertEqual(engine._notional(position), 0)


class OriginalThesisViewTests(unittest.TestCase):
    def test_portfolio_exposes_durable_entry_thesis_for_close_reasoning(self):
        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.ex = Mock()
        now = time.time()
        st = {
            "state": state.RUNNING,
            "cooldowns": {}, "entry_feedback": {}, "entry_failures": {},
            "recent_setups": {},
            "opened_at": {"BTC/USDT:USDT": now - 3600},
            "active_trades": {
                "BTC/USDT:USDT": {
                    "age_known": True,
                    "risk_usd": 150,
                    "entry_reason": "1h and 4h continuation aligned",
                    "setup_type": "trend_continuation",
                    "invalidation_anchor": "structure",
                    "exit_policy": "fixed_rr",
                    "stop_loss_pct": 2,
                    "take_profit_pct": 4,
                    "signal_ts": 1_000,
                    "entry_evidence": {
                        "trend_1h": "up", "trend_4h": "up",
                        "evidence_fingerprint": "abc",
                    },
                },
            },
        }
        positions = [{
            "symbol": "BTC/USDT:USDT", "side": "long",
            "entryPrice": 100, "markPrice": 101, "percentage": 1,
            "leverage": 2, "notional": 1_000,
        }]

        with patch("agent.engine.time.time", return_value=now):
            view = engine._portfolio_view(
                10_000, positions, st, 0, 0)

        held = view["open_positions"][0]
        self.assertTrue(held["age_verified"])
        self.assertEqual(
            held["original_thesis"]["entry_evidence"]["trend_1h"], "up")
        self.assertEqual(held["planned_risk_usd"], 150)


class LLMAuditEventTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.llm = Mock()

    @patch("agent.engine.state.log_event")
    def test_input_event_contains_exact_provider_request(self, log_event):
        request = {
            "provider": "openai",
            "request": {"model": "gpt-test", "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "snapshot"},
            ]},
        }
        self.engine.llm.audit_request.return_value = request

        self.engine._journal_llm_input({"price": 1}, {"equity": 10}, 1)

        kind, payload = log_event.call_args.args
        self.assertEqual(kind, "llm_input")
        self.assertEqual(json.loads(payload), request)
        self.engine.llm.audit_request.assert_called_once_with(
            {"price": 1}, {"equity": 10}, 1)

    @patch("agent.engine.state.log_event")
    def test_output_event_contains_raw_response_and_attempts(self, log_event):
        result = {
            "provider": "openai", "model": "gpt-test",
            "request_attempts": [{"temperature": 0.2}, {}],
            "response": {"id": "req-1", "raw_text": '{"decisions":[]}',
                         "effective_temperature": None,
                         "parsed_decisions": []},
        }
        self.engine.llm.call_audit.return_value = result

        self.engine._journal_llm_output()

        kind, payload = log_event.call_args.args
        self.assertEqual(kind, "llm_output")
        self.assertEqual(json.loads(payload), result)


class LiquidityFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.plan = {
            "symbol": "KAITO/USDT:USDT",
            "direction": "long",
            "margin_pct_equity": 10.0,
        }
        self.rejection = EntryLiquidityRejected(
            "insufficient depth",
            {
                "symbol": "KAITO/USDT:USDT",
                "reason": "insufficient_depth",
                "requested_contracts": 100.0,
                "available_contracts": 40.0,
                "requested_notional_usdt": 1_000.0,
                "available_notional_usdt": 400.0,
                "max_slippage_pct": 0.35,
            },
        )

    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    def test_first_rejection_feeds_one_smaller_retry_to_the_model(
            self, log_event, commit):
        st = {"entry_feedback": {}}
        with patch("agent.engine.time.time", return_value=1_000.0):
            record = self.engine._remember_liquidity_rejection(
                self.plan, st, self.rejection)

        self.assertEqual(record["consecutive_rejections"], 1)
        self.assertEqual(record["blocked_until"], 0)
        self.assertAlmostEqual(record["max_retry_size_pct_equity"], 2.8)
        self.assertEqual(log_event.call_args.args[0],
                         "entry_liquidity_rejected")
        commit.assert_called_once_with(st)


    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    def test_second_depth_rejection_creates_backoff(
            self, log_event, commit):
        st = {"entry_feedback": {}}
        with patch("agent.engine.time.time", return_value=1_000.0):
            self.engine._remember_liquidity_rejection(
                self.plan, st, self.rejection)
        with patch("agent.engine.time.time", return_value=1_300.0):
            record = self.engine._remember_liquidity_rejection(
                self.plan, st, self.rejection)

        self.assertEqual(record["consecutive_rejections"], 2)
        self.assertEqual(record["blocked_until"], 2_200.0)

    def test_portfolio_tells_llm_about_rejections_and_cooldowns(self):
        now = time.time()
        st = {
            "state": "RUNNING",
            "cooldowns": {"AAVE/USDT:USDT": now + 600},
            "opened_at": {},
            "entry_feedback": {
                "KAITO/USDT:USDT": {
                    "reason": "insufficient_depth",
                    "direction": "long",
                    "last_rejected_at": now - 60,
                    "expires_at": now + 1200,
                    "blocked_until": 0,
                    "consecutive_rejections": 1,
                    "available_ratio": 0.4,
                    "max_retry_size_pct_equity": 2.8,
                },
            },
        }

        with patch("agent.engine.time.time", return_value=now):
            view = self.engine._portfolio_view(10_000, [], st, 0, 0)

        self.assertEqual(
            view["post_loss_cooldowns"][0]["symbol"], "AAVE/USDT:USDT")
        feedback = view["recent_entry_feedback"][0]
        self.assertEqual(feedback["symbol"], "KAITO/USDT:USDT")
        self.assertTrue(feedback["retry_allowed"])
        self.assertEqual(feedback["max_retry_size_pct_equity"], 2.8)

    @patch("agent.engine.state.load_state", return_value={"state": "RUNNING"})
    def test_execution_converts_structured_depth_failure_into_feedback(
            self, load_state):
        self.engine.ex = Mock()
        self.engine.ex.price.return_value = 100
        self.engine.ex.contracts_for_notional.return_value = 100
        self.engine.ex.guarded_entry_limit.side_effect = self.rejection
        self.engine._remember_liquidity_rejection = Mock()
        st = {"entry_feedback": {}}
        plan = {
            **self.plan,
            "notional": 1_000,
            "price": 100,
            "leverage": 2,
        }

        self.assertFalse(self.engine._execute_open(plan, st))

        self.engine._remember_liquidity_rejection.assert_called_once_with(
            plan, st, self.rejection)

    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    def test_full_size_repeat_turns_feedback_into_backoff(
            self, log_event, commit):
        now = time.time()
        st = {
            "entry_feedback": {
                "KAITO/USDT:USDT": {
                    "blocked_until": 0,
                    "expires_at": now + 1800,
                },
            },
        }

        with patch("agent.engine.time.time", return_value=now):
            self.engine._backoff_ignored_liquidity_feedback(
                st, "KAITO/USDT:USDT",
                "liquidity retry requires an explicit smaller size")

        record = st["entry_feedback"]["KAITO/USDT:USDT"]
        self.assertEqual(record["blocked_until"], now + 900)
        self.assertEqual(log_event.call_args.args[0],
                         "entry_liquidity_backoff")
        commit.assert_called_once_with(st)


class EntryFailureBackoffTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.plan = {
            "symbol": "CL/USDT:USDT",
            "direction": "long",
        }

    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    def test_permanent_rejection_is_blocked_until_universe_refresh(
            self, log_event, commit):
        rejection = EntryOrderRejected(
            "rejected",
            {
                "symbol": "CL/USDT:USDT",
                "stage": "attached_entry",
                "classification": "permanent",
                "error_code": "51001",
                "error_message": "Instrument does not exist",
            },
        )
        st = {"entry_failures": {}}

        with patch("agent.engine.time.time", return_value=1_000.0):
            record = self.engine._remember_entry_failure(
                self.plan, st, rejection, "attached_entry")

        self.assertEqual(record["classification"], "permanent")
        self.assertEqual(record["blocked_until"], 4_600.0)
        self.assertEqual(record["error_code"], "51001")
        self.assertEqual(log_event.call_args.args[0],
                         "entry_execution_failed")
        commit.assert_called_once_with(st)

    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    def test_transient_failures_use_bounded_exponential_backoff(
            self, log_event, commit):
        st = {"entry_failures": {}}
        with patch("agent.engine.time.time", return_value=1_000.0):
            first = self.engine._remember_entry_failure(
                self.plan, st, RuntimeError("temporary"), "price_check")
        with patch("agent.engine.time.time", return_value=1_100.0):
            second = self.engine._remember_entry_failure(
                self.plan, st, RuntimeError("temporary"), "price_check")

        self.assertEqual(first["blocked_until"], 1_900.0)
        self.assertEqual(second["blocked_until"], 2_900.0)
        self.assertEqual(second["consecutive_failures"], 2)

    def test_portfolio_exposes_safe_failure_details_to_llm(self):
        now = time.time()
        st = {
            "state": "RUNNING",
            "cooldowns": {},
            "opened_at": {},
            "entry_feedback": {},
            "entry_failures": {
                "CL/USDT:USDT": {
                    "reason": "exchange_rejected",
                    "direction": "long",
                    "stage": "attached_entry",
                    "classification": "permanent",
                    "error_code": "51001",
                    "error_message": "Instrument does not exist",
                    "last_failed_at": now - 60,
                    "blocked_until": now + 600,
                    "expires_at": now + 3600,
                    "consecutive_failures": 2,
                },
            },
        }

        with patch("agent.engine.time.time", return_value=now):
            view = self.engine._portfolio_view(10_000, [], st, 0, 0)

        failure = view["recent_entry_failures"][0]
        self.assertEqual(failure["symbol"], "CL/USDT:USDT")
        self.assertEqual(failure["error_code"], "51001")
        self.assertEqual(failure["retry_after_minutes"], 10.0)

    @patch("agent.engine.state.load_state", return_value={"state": "RUNNING"})
    @patch("agent.engine.state.commit")
    @patch("agent.engine.state.log_event")
    def test_attached_entry_rejection_is_persisted_before_next_cycle(
            self, log_event, commit, load_state):
        rejection = EntryOrderRejected(
            "rejected",
            {
                "symbol": "CL/USDT:USDT",
                "stage": "attached_entry",
                "classification": "permanent",
                "error_code": "51001",
                "error_message": "Instrument does not exist",
            },
        )
        self.engine.ex = Mock()
        self.engine.ex.price.return_value = 100
        self.engine.ex.contracts_for_notional.return_value = 2
        self.engine.ex.guarded_entry_limit.return_value = {
            "limit_price": 100.25,
            "spread_pct": 0.05,
            "mid": 100,
        }
        self.engine.ex.open_position.side_effect = rejection
        st = {"entry_failures": {}}
        plan = {
            **self.plan,
            "notional": 200,
            "price": 100,
            "leverage": 2,
            "sl_pct": 2,
            "tp_pct": 4,
        }

        self.assertFalse(self.engine._execute_open(plan, st))

        failure = st["entry_failures"]["CL/USDT:USDT"]
        self.assertEqual(failure["classification"], "permanent")
        self.assertEqual(failure["error_code"], "51001")
        self.assertGreater(failure["blocked_until"], failure["last_failed_at"])
        self.assertEqual(log_event.call_args.args[0],
                         "entry_execution_failed")
        commit.assert_called_once_with(st)


class MinimumHoldTests(unittest.TestCase):
    """The model may not close inside strategy.min_hold_minutes.

    Measured forward return after a qualifying entry is at its worst around
    30 minutes in, and the shipped payoff depends on the minority of trades
    that reach a distant target. Sub-hour discretionary exits removed that
    tail while still paying a full taker round trip.
    """

    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.pos = {"symbol": "BTC/USDT:USDT", "side": "short"}

    def _state(self, minutes_ago):
        return {"opened_at": {
            "BTC/USDT:USDT": time.time() - minutes_ago * 60}}

    def _close(self, trigger="thesis_invalidated"):
        return {"action": "close", "symbol": "BTC/USDT:USDT",
                "close_trigger": trigger, "reasoning": "impulse reversed"}

    @patch("agent.engine.state.log_event")
    def test_close_inside_the_floor_is_blocked(self, log_event):
        self.assertTrue(self.engine._too_young_to_close(
            self.pos, self._close(), self._state(17)))
        self.assertEqual(log_event.call_args.args[0], "rejected")

    @patch("agent.engine.state.log_event")
    def test_close_after_the_floor_is_allowed(self, log_event):
        self.assertFalse(self.engine._too_young_to_close(
            self.pos, self._close(), self._state(120)))
        log_event.assert_not_called()

    @patch("agent.engine.state.log_event")
    def test_risk_reduction_is_never_blocked(self, log_event):
        # De-risking is not second-guessing the entry, so the floor must not
        # stand between the model and a position it wants to shrink.
        self.assertFalse(self.engine._too_young_to_close(
            self.pos, self._close("risk_reduction"), self._state(5)))
        log_event.assert_not_called()

    @patch("agent.engine.state.log_event")
    def test_unknown_age_allows_the_close(self, log_event):
        # Trapping a position we cannot age until the max-hold timer is the
        # more dangerous failure, so this direction fails open.
        self.assertFalse(self.engine._too_young_to_close(
            self.pos, self._close(), {"opened_at": {}}))
        log_event.assert_not_called()

    @patch("agent.engine.state.log_event")
    def test_zero_floor_disables_the_guard(self, log_event):
        self.engine.cfg["strategy"]["min_hold_minutes"] = 0
        self.assertFalse(self.engine._too_young_to_close(
            self.pos, self._close(), self._state(1)))
        log_event.assert_not_called()

    @patch("agent.engine.state.log_event")
    def test_the_observed_live_exits_would_all_have_been_blocked(
            self, log_event):
        # LTC 17 min, ETC 55 min, DOGE 75 min - every discretionary exit in
        # the losing window, none of them a risk reduction.
        for held_minutes in (17, 55, 75):
            with self.subTest(held_minutes=held_minutes):
                self.assertTrue(self.engine._too_young_to_close(
                    self.pos, self._close(), self._state(held_minutes)))


if __name__ == "__main__":
    unittest.main()
