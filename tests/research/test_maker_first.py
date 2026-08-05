"""B7.5 maker-first: the passive-entry primitive and its hard guarantee.

The guarantee is that the order is never left resting. Every exit from
``maker_first_entry`` either reports a fill or has cancelled, and when the
cancel fails the fill state is re-read rather than assumed - because
assuming unfilled would double the position on a later cross, and assuming
filled would leave one unmanaged.

Note what these tests do NOT cover, since it is the reason the primitive is
not wired into the entry path: they exercise the exchange call against a
mock. Fill rate is not knowable from history - the passive order was never
there - so the only real validation is a live demo account.
"""

import unittest
import unittest.mock
from unittest.mock import Mock

import ccxt

from agent import state
from agent.config import ConfigError, validate_config
from agent.exchange import (CredentialError, Exchange,
                            MakerFirstAmbiguousError,
                            OrderSubmissionAmbiguousError)
from tests.helpers import valid_config


def exchange(order_states, book=None):
    """An Exchange whose fetch_order walks a scripted sequence of states."""
    ex = Exchange.__new__(Exchange)
    ex.x = Mock()
    ex.retry = lambda fn, *a, **k: fn(*a, **k)
    ex.x.price_to_precision = lambda symbol, price: round(float(price), 4)
    ex.x.fetch_order_book.return_value = book or {
        "bids": [[99.0, 100]], "asks": [[101.0, 100]],
        "timestamp": 1_760_000_000_000,
    }
    ex.x.create_order.return_value = dict(order_states[0])
    remaining = list(order_states[1:]) or [dict(order_states[0])]
    ex.x.fetch_order.side_effect = (
        lambda *a, **k: dict(remaining.pop(0) if len(remaining) > 1
                             else remaining[0]))
    # A passive fill now runs the same settlement an IOC fill does, so the
    # fixture has to provide what that settlement reads. That the fixture
    # grew is the point of the refactor: the maker path can no longer create
    # a position without the protection audit running.
    ex.position = Mock(return_value={
        "contracts": 10, "markPrice": 100.0, "id": "pos-1",
        "liquidationPrice": 80.0, "info": {}})
    ex.ensure_protection = Mock(
        return_value={"stop_loss": True, "take_profit": True})
    ex._alert = Mock()
    return ex


class FillReportingTests(unittest.TestCase):
    def test_a_full_passive_fill_is_reported(self):
        ex = exchange([{"id": "o1", "filled": 10, "average": 99.0}])

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0,
            wait_seconds=0.01, reference_price=100.0)

        self.assertEqual(result["fill_rate"], 1.0)
        self.assertEqual(result["filled_contracts"], 10)
        self.assertFalse(result["cancelled"],
                         "a full fill does not issue a cancel request")
        self.assertFalse(result["resting"])

    def test_an_unfilled_order_is_cancelled(self):
        ex = exchange([{"id": "o1", "filled": 0, "average": None,
                        "status": "canceled"}])

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0,
            wait_seconds=0.01, reference_price=100.0)

        ex.x.cancel_order.assert_called_once()
        self.assertEqual(result["fill_rate"], 0.0)
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["resting"])

    def test_a_partial_fill_cancels_the_remainder(self):
        ex = exchange([{"id": "o1", "filled": 4, "average": 99.0}])

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0,
            wait_seconds=0.01, reference_price=100.0)

        ex.x.cancel_order.assert_called_once()
        self.assertAlmostEqual(result["fill_rate"], 0.4, 6)

    def test_a_failed_cancel_is_reported_as_possibly_resting(self):
        """Never assumed either way: one doubles, the other orphans."""
        ex = exchange([{"id": "o1", "filled": 0, "average": None}])
        ex.x.cancel_order.side_effect = RuntimeError("order not found")

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0,
            wait_seconds=0.01, reference_price=100.0)

        self.assertFalse(result["cancelled"])
        self.assertTrue(result["resting"])

    def test_a_fill_racing_the_cancel_is_re_read(self):
        ex = exchange([
            {"id": "o1", "filled": 0, "average": None},
            {"id": "o1", "filled": 10, "average": 99.0},
        ])
        ex.x.cancel_order.side_effect = RuntimeError("already filled")

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0,
            wait_seconds=0.01, reference_price=100.0)

        self.assertEqual(result["filled_contracts"], 10)
        self.assertFalse(result["resting"],
                         "a filled order is not resting")


class PassivePricingTests(unittest.TestCase):
    def test_a_buy_joins_the_bid(self):
        ex = exchange([{"id": "o1", "filled": 10, "average": 99.0}])

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0, 0.01, 100.0)

        self.assertEqual(result["limit_price"], 99.0)

    def test_a_sell_joins_the_ask(self):
        ex = exchange([{"id": "o1", "filled": 10, "average": 101.0}])

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "sell", 10, 2, 103.0, 97.0, 0.01, 100.0)

        self.assertEqual(result["limit_price"], 101.0)

    def test_the_order_is_post_only(self):
        """Otherwise it silently becomes the taker order this avoids."""
        ex = exchange([{"id": "o1", "filled": 10, "average": 99.0}])

        ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0, 0.01, 100.0)

        params = ex.x.create_order.call_args.args[-1]
        self.assertTrue(params["postOnly"])

    def test_protection_is_attached_to_the_passive_order(self):
        """A passive fill must arrive already protected, as an IOC does."""
        ex = exchange([{"id": "o1", "filled": 10, "average": 99.0}])

        ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0, 0.01, 100.0)

        params = ex.x.create_order.call_args.args[-1]
        self.assertEqual(params["stopLoss"]["triggerPrice"], 97.0)
        self.assertEqual(params["takeProfit"]["triggerPrice"], 103.0)

    def test_a_one_sided_book_raises_rather_than_guessing_a_price(self):
        ex = exchange([{"id": "o1", "filled": 0}],
                      book={"bids": [], "asks": [[101.0, 5]]})

        with self.assertRaises(RuntimeError):
            ex.maker_first_entry(
                "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0, 0.01, 100.0)


class CounterfactualTests(unittest.TestCase):
    """The measurement the maker-first counterfactual exists to produce."""

    def test_a_buy_below_the_reference_is_a_saving(self):
        ex = exchange([{"id": "o1", "filled": 10, "average": 99.0}])

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0, 0.01, 100.0)

        self.assertAlmostEqual(result["ioc_counterfactual_pct"], 1.0, 6)

    def test_a_sell_above_the_reference_is_a_saving(self):
        ex = exchange([{"id": "o1", "filled": 10, "average": 101.0}])

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "sell", 10, 2, 103.0, 97.0, 0.01, 100.0)

        self.assertAlmostEqual(result["ioc_counterfactual_pct"], 1.0, 6)

    def test_an_unfilled_attempt_reports_no_counterfactual(self):
        ex = exchange([{"id": "o1", "filled": 0, "average": None}])

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0, 0.01, 100.0)

        self.assertIsNone(result["ioc_counterfactual_pct"])


class FailClosedAdversarialTests(unittest.TestCase):
    """The maker hand-off has one safe crossing state and no others."""

    SYMBOL = "BTC/USDT:USDT"

    def _call(self, state, *, contracts=10, ex=None):
        ex = ex or exchange([state])
        result = ex.maker_first_entry(
            self.SYMBOL, "buy", contracts, 2, 97.0, 103.0,
            wait_seconds=0, reference_price=100.0)
        return ex, result

    def test_only_zero_fill_with_terminal_cancel_can_cross(self):
        ex, result = self._call({
            "id": "o1", "filled": 0, "average": None,
            "status": "canceled",
        })
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["resting"])
        # The helper has no observer/group correlation; the structured result
        # is still the sole safe crossing state.
        self.assertIsNone(ex._pending_entry)

    def test_missing_unknown_and_open_statuses_block_and_clear_pending(self):
        for status in (None, "unknown", "open"):
            state = {"id": "o1", "filled": 0, "average": None}
            if status is not None:
                state["status"] = status
            ex, result = self._call(state)
            self.assertFalse(result["cancelled"])
            self.assertTrue(result["resting"])
            self.assertTrue(result["blocking"])
            self.assertIsNone(ex._pending_entry)

    def test_cancel_or_status_read_failure_blocks(self):
        ex = exchange([{"id": "o1", "filled": 0, "average": None}])
        ex.x.cancel_order.side_effect = RuntimeError("cancel timeout")
        ex.x.fetch_order.side_effect = RuntimeError("read timeout")

        with self.assertRaises(MakerFirstAmbiguousError):
            ex.maker_first_entry(
                self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)
        self.assertIsNone(ex._pending_entry)

    def test_cancel_and_post_cancel_authentication_failures_propagate(self):
        ex = exchange([{"id": "o1", "filled": 0}])
        ex.x.cancel_order.side_effect = CredentialError("bad credentials")
        with self.assertRaises(CredentialError):
            ex.maker_first_entry(
                self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)
        self.assertIsNone(ex._pending_entry)

        ex = exchange([{"id": "o1", "filled": 0}])
        ex.x.fetch_order.side_effect = ccxt.AuthenticationError(
            "bad credentials")
        with self.assertRaises(ccxt.AuthenticationError):
            ex.maker_first_entry(
                self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)
        self.assertIsNone(ex._pending_entry)

    def test_partial_resting_blocks_without_settlement(self):
        ex = exchange([{
            "id": "o1", "filled": 4, "average": 99.0,
            "status": "open",
        }])
        ex.settle_fill = Mock()

        result = ex.maker_first_entry(
            self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)

        self.assertEqual(result["filled_contracts"], 4)
        self.assertTrue(result["resting"])
        self.assertIsNone(result["execution"])
        ex.settle_fill.assert_not_called()
        self.assertIsNone(ex._pending_entry)

    def test_partial_confirmed_cancel_settles_without_fallback(self):
        ex = exchange([{
            "id": "o1", "filled": 4, "average": 99.0,
            "status": "canceled",
        }])
        settled = {"filled": 4, "average": 99.0}
        ex.settle_fill = Mock(return_value=settled)

        result = ex.maker_first_entry(
            self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)

        self.assertTrue(result["cancelled"])
        self.assertFalse(result["resting"])
        self.assertIs(result["execution"], settled)
        self.assertIsNone(ex._pending_entry)

    def test_post_cancel_raw_accumulated_partial_settles_and_never_crosses(self):
        ex = exchange([
            {"id": "o1"},
            {"id": "o1", "status": "canceled",
             "info": {"accFillSz": "4", "avgPx": "99"}},
        ])
        settled = {"filled": 4, "average": 99.0}
        ex.settle_fill = Mock(return_value=settled)

        result = ex.maker_first_entry(
            self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)

        self.assertEqual(result["filled_contracts"], 4)
        self.assertEqual(result["quantity_evidence"], "info.accFillSz")
        self.assertIs(result["execution"], settled)
        self.assertIsNone(ex._pending_entry)

    def test_post_cancel_raw_accumulated_full_settles_and_never_crosses(self):
        ex = exchange([
            {"id": "o1"},
            {"id": "o1", "status": "canceled",
             "info": {"accFillSz": "10", "avgPx": "99"}},
        ])
        settled = {"filled": 10, "average": 99.0}
        ex.settle_fill = Mock(return_value=settled)

        result = ex.maker_first_entry(
            self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)

        self.assertEqual(result["filled_contracts"], 10)
        self.assertIs(result["execution"], settled)
        self.assertFalse(result["cancelled"])
        self.assertIsNone(ex._pending_entry)

    def test_post_cancel_missing_quantity_is_ambiguous_even_if_canceled(self):
        ex = exchange([
            {"id": "o1"},
            {"id": "o1", "status": "canceled", "info": {}},
        ])

        with self.assertRaisesRegex(
                MakerFirstAmbiguousError, "no explicit fill quantity"):
            ex.maker_first_entry(
                self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)
        self.assertIsNone(ex._pending_entry)

    def test_raw_accumulated_fill_cannot_be_hidden_by_zero_projection(self):
        ex = exchange([
            {"id": "o1", "filled": 0},
            {"id": "o1", "status": "canceled", "filled": 0,
             "info": {"accFillSz": "4", "avgPx": "99"}},
        ])

        with self.assertRaisesRegex(
                MakerFirstAmbiguousError, "fill quantities disagree"):
            ex.maker_first_entry(
                self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)
        self.assertIsNone(ex._pending_entry)

    def test_inconsistent_remaining_quantity_is_ambiguous(self):
        ex = exchange([{
            "id": "o1", "filled": 4, "remaining": 1,
            "average": 99.0, "status": "canceled",
        }])

        with self.assertRaisesRegex(
                MakerFirstAmbiguousError, "fill and remaining disagree"):
            ex.maker_first_entry(
                self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)

    def test_full_fill_settles_without_cancellation(self):
        ex = exchange([{"id": "o1", "filled": 10, "average": 99.0}])
        settled = {"filled": 10, "average": 99.0}
        ex.settle_fill = Mock(return_value=settled)

        result = ex.maker_first_entry(
            self.SYMBOL, "buy", 10, 2, 97.0, 103.0, 0, 100)

        self.assertFalse(result["cancelled"])
        self.assertFalse(result["resting"])
        self.assertIs(result["execution"], settled)
        ex.x.cancel_order.assert_not_called()
        self.assertIsNone(ex._pending_entry)

    def test_invalid_fill_and_average_values_block_and_clear_pending(self):
        for value in (float("nan"), float("inf"), -1, 11):
            ex = exchange([{"id": "o1", "filled": value,
                            "average": 99.0}])
            with self.assertRaises(RuntimeError):
                ex.maker_first_entry(
                    self.SYMBOL, "buy", 10, 2, 97, 103, 0, 100)
            self.assertIsNone(ex._pending_entry)

        for average in (None, 0, -1, float("nan"), float("inf")):
            ex = exchange([{"id": "o1", "filled": 10,
                            "average": average}])
            with self.assertRaises(RuntimeError):
                ex.maker_first_entry(
                    self.SYMBOL, "buy", 10, 2, 97, 103, 0, 100)
            self.assertIsNone(ex._pending_entry)

    def test_ack_recovery_and_settlement_errors_block_and_clear(self):
        ex = exchange([{"id": "o1", "filled": 0}])
        ex.x.create_order.return_value = ["not", "an", "ack"]
        with self.assertRaises(RuntimeError):
            ex.maker_first_entry(
                self.SYMBOL, "buy", 10, 2, 97, 103, 0, 100)
        self.assertIsNone(ex._pending_entry)

        ex = exchange([{"filled": 0}])
        ex._recover_order = Mock(side_effect=RuntimeError("recovery failed"))
        with self.assertRaises(RuntimeError):
            ex.maker_first_entry(
                self.SYMBOL, "buy", 10, 2, 97, 103, 0, 100)
        self.assertIsNone(ex._pending_entry)

        ex = exchange([{"id": "o1", "filled": 10, "average": 99.0}])
        ex.settle_fill = Mock(side_effect=RuntimeError("settle failed"))
        with self.assertRaises(RuntimeError):
            ex.maker_first_entry(
                self.SYMBOL, "buy", 10, 2, 97, 103, 0, 100)
        self.assertIsNone(ex._pending_entry)

    def test_engine_blocks_fallback_for_exception_and_resting_states(self):
        for maker_result in (RuntimeError("maker failed"), {
                "symbol": self.SYMBOL,
                "fill_rate": 0.0, "filled_contracts": 0,
                "requested_contracts": 10, "cancelled": False,
                "resting": True, "execution": None,
                "order_id": "o1", "quantity_evidence": "filled"}):
            engine = WiredSafelyTests()._engine()
            if isinstance(maker_result, Exception):
                engine.ex.maker_first_entry.side_effect = maker_result
            else:
                engine.ex.maker_first_entry.return_value = maker_result
            engine.ex.price.return_value = 100.0
            engine.ex.contracts_for_notional.return_value = 10.0
            engine.ex.guarded_entry_limit.return_value = {
                "mid": 100.0, "limit_price": 100.0,
                "spread_pct": 0.0,
            }
            with unittest.mock.patch(
                    "agent.engine.state.load_state",
                    return_value={"state": "RUNNING"}):
                result = engine._execute_open(WiredSafelyTests()._plan(), {})
            self.assertFalse(result)
            engine.ex.open_position.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_it_is_off_by_default(self):
        cfg = validate_config(valid_config())
        self.assertFalse(cfg["execution"]["maker_first_enabled"])

    def test_the_wait_is_bounded_inside_a_signal_bar(self):
        raw = valid_config()
        raw["execution"]["maker_first_wait_seconds"] = 600
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_a_valid_wait_is_accepted(self):
        raw = valid_config()
        raw["execution"]["maker_first_enabled"] = True
        raw["execution"]["maker_first_wait_seconds"] = 30

        cfg = validate_config(raw)

        self.assertTrue(cfg["execution"]["maker_first_enabled"])
        self.assertEqual(cfg["execution"]["maker_first_wait_seconds"], 30)


class WiredSafelyTests(unittest.TestCase):
    """The integration, and the guarantees that made it safe to add.

    The first attempt at this returned early on a passive fill and would have
    created a position with no journal row, no liquidation check and no
    protection audit. It was reverted. What made the second attempt safe was
    extracting Exchange.settle_fill and Engine._settle_entry so both entry
    paths run identical bookkeeping - these tests hold that.
    """

    def _engine(self, enabled=True):
        from unittest.mock import Mock as M
        from agent.engine import Engine

        engine = Engine.__new__(Engine)
        cfg = valid_config()
        cfg["execution"]["maker_first_enabled"] = enabled
        cfg["execution"]["maker_first_wait_seconds"] = 1
        engine.cfg = cfg
        engine._audit_json = staticmethod(lambda payload: "{}")
        engine.ex = M()
        engine.alerts = M()
        return engine

    def _plan(self):
        return {"symbol": "BTC/USDT:USDT", "direction": "long",
                "leverage": 2, "notional": 1000.0, "sl_pct": 2.0,
                "tp_pct": 4.0, "estimated_loss_pct": 2.62,
                "risk_budget_usd": 100.0, "price": 100.0}

    @staticmethod
    def _maker_result(filled=0, requested=10, execution=None,
                      *, resting=False):
        result = {
            "symbol": "BTC/USDT:USDT",
            "order_id": "o1",
            "requested_contracts": requested,
            "filled_contracts": filled,
            "fill_rate": filled / requested,
            "resting": resting,
            "execution": execution,
            "quantity_evidence": "filled",
        }
        if filled < requested and not resting:
            result.update({
                "cancelled": True,
                "cancellation_confirmed": True,
                "cancellation_status": "canceled",
                "cancellation_order_id": "o1",
            })
        return result

    def test_disabled_never_touches_the_exchange(self):
        engine = self._engine(enabled=False)

        result = engine._maker_first_attempt(
            self._plan(), {}, "BTC/USDT:USDT", "buy", 10, 97.0, 103.0, 100.0)

        self.assertIsNone(result)
        engine.ex.maker_first_entry.assert_not_called()

    def test_an_unfilled_attempt_falls_through_to_crossing(self):
        engine = self._engine()
        engine.ex.maker_first_entry.return_value = {
            "symbol": "BTC/USDT:USDT",
            "fill_rate": 0.0, "filled_contracts": 0,
            "requested_contracts": 10, "cancelled": True,
            "resting": False, "order_id": "o1", "execution": None,
            "cancellation_confirmed": True,
            "cancellation_status": "canceled",
            "cancellation_order_id": "o1",
            "quantity_evidence": "filled"}

        with unittest.mock.patch("agent.engine.state.log_event"):
            result = engine._maker_first_attempt(
                self._plan(), {}, "BTC/USDT:USDT", "buy", 10, 97.0, 103.0,
                100.0)

        self.assertIsNone(result, "None means cross as normal")

    def test_a_raising_attempt_blocks_crossing(self):
        """A broken maker attempt cannot prove that no order exists."""
        engine = self._engine()
        engine.ex.maker_first_entry.side_effect = RuntimeError("boom")

        with unittest.mock.patch("agent.engine.state.log_event"):
            result = engine._maker_first_attempt(
                self._plan(), {}, "BTC/USDT:USDT", "buy", 10, 97.0, 103.0,
                100.0)

        self.assertIs(result, False)

    def test_a_possibly_resting_order_stops_the_entry(self):
        """Crossing on top of a live order could double the position."""
        engine = self._engine()
        engine.ex.maker_first_entry.return_value = self._maker_result(
            4, resting=True)

        with unittest.mock.patch("agent.engine.state.log_event"):
            result = engine._maker_first_attempt(
                self._plan(), {}, "BTC/USDT:USDT", "buy", 10, 97.0, 103.0,
                100.0)

        self.assertIs(result, False, "False means stop, not cross")
        engine.alerts.send.assert_called_once()

    def test_a_fill_returns_a_settled_execution(self):
        engine = self._engine()
        settled = {"filled": 10, "average": 99.0,
                   "protection": {"stop_loss": True}}
        engine.ex.maker_first_entry.return_value = {
            "symbol": "BTC/USDT:USDT",
            "fill_rate": 1.0, "filled_contracts": 10, "resting": False,
            "requested_contracts": 10,
            "order_id": "o1", "execution": settled,
            "quantity_evidence": "filled"}

        with unittest.mock.patch("agent.engine.state.log_event"):
            result = engine._maker_first_attempt(
                self._plan(), {}, "BTC/USDT:USDT", "buy", 10, 97.0, 103.0,
                100.0)

        self.assertIs(result, settled)

    def test_the_attempt_is_journalled_without_the_execution_blob(self):
        engine = self._engine()
        engine.ex.maker_first_entry.return_value = {
            "symbol": "BTC/USDT:USDT",
            "fill_rate": 1.0, "filled_contracts": 10, "resting": False,
            "requested_contracts": 10,
            "order_id": "o1", "execution": {"filled": 10},
            "quantity_evidence": "filled"}

        with unittest.mock.patch("agent.engine.state.log_event") as logged:
            engine._maker_first_attempt(
                self._plan(), {}, "BTC/USDT:USDT", "buy", 10, 97.0, 103.0,
                100.0)

        self.assertEqual(logged.call_args.args[0], "maker_attempt")

    def test_partial_terminal_cancel_returns_execution_without_fallback(self):
        engine = self._engine()
        settled = {"filled": 4, "average": 99.0}
        engine.ex.maker_first_entry.return_value = self._maker_result(
            4, execution=settled)

        with unittest.mock.patch("agent.engine.state.log_event"):
            result = engine._maker_first_attempt(
                self._plan(), {}, "BTC/USDT:USDT", "buy", 10, 97, 103, 100)

        self.assertIs(result, settled)
        engine.ex.open_position.assert_not_called()

    def test_credentials_and_journal_errors_propagate(self):
        for error in (CredentialError("bad credentials"),
                      ccxt.AuthenticationError("bad credentials"),
                      state.JournalError("journal unavailable")):
            engine = self._engine()
            engine.ex.maker_first_entry.side_effect = error
            with self.assertRaises(type(error)):
                engine._maker_first_attempt(
                    self._plan(), {}, "BTC/USDT:USDT", "buy", 10,
                    97, 103, 100)

    def test_maker_attempt_journal_failure_after_fill_fail_stops(self):
        engine = self._engine()
        settled = {"filled": 10, "average": 99.0}
        engine.ex.maker_first_entry.return_value = self._maker_result(
            10, execution=settled)

        with unittest.mock.patch(
                "agent.engine.state.log_event",
                side_effect=state.JournalError("disk unavailable")):
            with self.assertRaises(state.JournalError):
                engine._maker_first_attempt(
                    self._plan(), {}, "BTC/USDT:USDT", "buy", 10,
                    97, 103, 100)

    def test_ambiguity_persists_operator_pause_and_blocks_next_open(self):
        engine = self._engine()
        engine.ex.maker_first_entry.side_effect = MakerFirstAmbiguousError(
            "unknown maker state", {"order_id": "o1"})
        state.set_state(state.RUNNING, operator_pause=False)
        st = {}
        try:
            with unittest.mock.patch("agent.engine.state.log_event"):
                self.assertFalse(engine._maker_first_attempt(
                    self._plan(), st, "BTC/USDT:USDT", "buy", 10,
                    97, 103, 100))
            persisted = state.load_state()
            self.assertEqual(persisted["state"], state.PAUSED)
            self.assertTrue(persisted["operator_pause"])
            self.assertEqual(st["state"], state.PAUSED)

            engine.ex.price.reset_mock()
            self.assertFalse(engine._execute_open(self._plan(), st))
            engine.ex.price.assert_not_called()
        finally:
            state.set_state(state.RUNNING, operator_pause=False)

    def test_safe_zero_confirmed_cancel_crosses_exactly_once(self):
        engine = self._engine()
        engine.ex.maker_first_entry.return_value = self._maker_result()
        engine.ex.price.return_value = 100.0
        engine.ex.contracts_for_notional.return_value = 10.0
        engine.ex.guarded_entry_limit.return_value = {
            "mid": 100.0, "limit_price": 100.0, "spread_pct": 0.0,
        }
        execution = {"filled": 10, "average": 100.0}
        engine.ex.open_position.return_value = execution
        engine._settle_entry = Mock(return_value=True)

        with unittest.mock.patch(
                "agent.engine.state.load_state",
                return_value={"state": state.RUNNING}), \
                unittest.mock.patch("agent.engine.state.log_event"):
            self.assertTrue(engine._execute_open(self._plan(), {}))

        engine.ex.open_position.assert_called_once()
        engine._settle_entry.assert_called_once()

    def test_pause_or_kill_during_maker_wait_blocks_fallback(self):
        for control_state in (state.PAUSED, state.KILLED):
            with self.subTest(control_state=control_state):
                engine = self._engine()
                engine.ex.maker_first_entry.return_value = self._maker_result()
                engine.ex.price.return_value = 100.0
                engine.ex.contracts_for_notional.return_value = 10.0
                engine.ex.guarded_entry_limit.return_value = {
                    "mid": 100.0, "limit_price": 100.0,
                    "spread_pct": 0.0,
                }
                with unittest.mock.patch(
                        "agent.engine.state.load_state", side_effect=[
                            {"state": state.RUNNING},
                            {"state": state.RUNNING},
                            {"state": control_state},
                        ]), unittest.mock.patch(
                            "agent.engine.state.log_event"):
                    self.assertFalse(engine._execute_open(
                        self._plan(), {}))
                engine.ex.open_position.assert_not_called()
                engine.ex._clear_pending_entry.assert_called()

    def test_direct_unrecovered_ambiguity_pauses_without_finite_retry(self):
        engine = self._engine(enabled=False)
        engine.ex.price.return_value = 100.0
        engine.ex.contracts_for_notional.return_value = 10.0
        engine.ex.guarded_entry_limit.return_value = {
            "mid": 100.0, "limit_price": 100.0, "spread_pct": 0.0,
        }
        engine.ex.open_position.side_effect = OrderSubmissionAmbiguousError(
            "unknown direct state", {"client_order_id": "client-1"})
        engine._remember_entry_failure = Mock()
        paused = {**state.load_state(), "state": state.PAUSED,
                  "operator_pause": True}

        with unittest.mock.patch(
                "agent.engine.state.load_state",
                return_value={"state": state.RUNNING}), \
                unittest.mock.patch(
                    "agent.engine.state.set_state", return_value=paused
                ) as set_state, unittest.mock.patch(
                    "agent.engine.state.log_event"):
            self.assertFalse(engine._execute_open(self._plan(), {}))

        set_state.assert_called_once()
        self.assertTrue(set_state.call_args.kwargs["operator_pause"])
        engine._remember_entry_failure.assert_not_called()

    def test_direct_journal_error_is_never_converted_to_entry_backoff(self):
        engine = self._engine(enabled=False)
        engine.ex.price.return_value = 100.0
        engine.ex.contracts_for_notional.return_value = 10.0
        engine.ex.guarded_entry_limit.return_value = {
            "mid": 100.0, "limit_price": 100.0, "spread_pct": 0.0,
        }
        engine.ex.open_position.side_effect = state.JournalError(
            "journal unavailable")
        engine._remember_entry_failure = Mock()

        with unittest.mock.patch(
                "agent.engine.state.load_state",
                return_value={"state": state.RUNNING}):
            with self.assertRaises(state.JournalError):
                engine._execute_open(self._plan(), {})

        engine._remember_entry_failure.assert_not_called()

    def test_failed_ambiguity_pause_persistence_fail_stops(self):
        engine = self._engine()
        engine.ex.maker_first_entry.side_effect = MakerFirstAmbiguousError(
            "unknown maker state", {"order_id": "o1"})

        with unittest.mock.patch(
                "agent.engine.state.load_state",
                return_value={"state": state.RUNNING}), \
                unittest.mock.patch(
                    "agent.engine.state.set_state",
                    side_effect=OSError("state disk unavailable")):
            with self.assertRaisesRegex(
                    state.JournalError, "pause persistence failed"):
                engine._maker_first_attempt(
                    self._plan(), {}, "BTC/USDT:USDT", "buy", 10,
                    97, 103, 100)


class SharedSettlementTests(unittest.TestCase):
    """Both entry paths must run the same post-fill verification."""

    def test_the_exchange_exposes_a_shared_settle_fill(self):
        self.assertTrue(hasattr(Exchange, "settle_fill"))

    def test_open_position_delegates_to_it(self):
        import inspect
        source = inspect.getsource(Exchange.open_position)
        self.assertIn("self.settle_fill(", source)

    def test_the_maker_path_delegates_to_it(self):
        import inspect
        source = inspect.getsource(Exchange.maker_first_entry)
        self.assertIn("self.settle_fill(", source)

    def test_the_engine_exposes_a_shared_settle_entry(self):
        from agent.engine import Engine
        self.assertTrue(hasattr(Engine, "_settle_entry"))

    def test_both_engine_paths_delegate_to_it(self):
        import inspect
        from agent.engine import Engine

        source = inspect.getsource(Engine._execute_open)

        # Once for the maker fill, once for the IOC fill.
        self.assertEqual(source.count("_settle_entry("), 2)

    def test_settlement_runs_the_protection_audit(self):
        import inspect
        source = inspect.getsource(Exchange.settle_fill)

        self.assertIn("ensure_protection", source)
        self.assertIn("unprotected_position", source)


if __name__ == "__main__":
    unittest.main()
