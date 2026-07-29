"""B7.5 / H-K(ii): the passive-entry primitive, and its one hard guarantee.

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
from unittest.mock import Mock

from agent.config import ConfigError, validate_config
from agent.exchange import Exchange
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
    return ex


class FillReportingTests(unittest.TestCase):
    def test_a_full_passive_fill_is_reported(self):
        ex = exchange([{"id": "o1", "filled": 10, "average": 99.0}])

        result = ex.maker_first_entry(
            "BTC/USDT:USDT", "buy", 10, 2, 97.0, 103.0,
            wait_seconds=0.01, reference_price=100.0)

        self.assertEqual(result["fill_rate"], 1.0)
        self.assertEqual(result["filled_contracts"], 10)
        self.assertFalse(result["resting"])

    def test_an_unfilled_order_is_cancelled(self):
        ex = exchange([{"id": "o1", "filled": 0, "average": None}])

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
    """The measurement H-K(ii) exists to produce."""

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


class NotWiredTests(unittest.TestCase):
    """The integration is deliberately absent; this pins that it stays so.

    If someone wires it later, this test fails and points them at the
    reason - which is that a passive fill needs the same journal row,
    liquidation check and protection audit an IOC fill gets.
    """

    def test_the_entry_path_does_not_call_the_maker_primitive(self):
        import inspect
        from agent import engine as engine_mod

        source = inspect.getsource(engine_mod.Engine._execute_open)

        self.assertNotIn("maker_first_entry", source)
        self.assertNotIn("_maker_first_attempt", source)


if __name__ == "__main__":
    unittest.main()
