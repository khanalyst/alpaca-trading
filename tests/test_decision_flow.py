import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from agent import state
from agent.engine import Engine
from agent.exchange import EntryLiquidityRejected
from tests.helpers import valid_config


def open_decision(symbol, confidence):
    return {
        "action": "open", "symbol": symbol, "direction": "long",
        "confidence": confidence, "size_pct_equity": 0, "leverage": 2,
        "stop_loss_pct": 1.0, "take_profit_pct": 2.0, "reasoning": "",
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
            "BTC/USDT:USDT": {"price": 100},
            "ETH/USDT:USDT": {"price": 100},
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
                entry_feedback):
            seen.append((
                decision["symbol"],
                [position["symbol"] for position in positions],
                gross,
            ))
            return plans[decision["symbol"]], None

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
            "opened_at": {},
            "active_trades": {},
            "protection": {},
        }

        engine.cycle(st)

        self.assertEqual(seen, [
            ("BTC/USDT:USDT", [], 0),
            ("ETH/USDT:USDT", ["BTC/USDT:USDT"], 100),
        ])
        self.assertEqual(engine._execute_open.call_count, 2)


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


if __name__ == "__main__":
    unittest.main()
