"""The public statistics endpoints backing the positioning contracts.

These fields degrade to None by design - no order depends on them. That is
correct for an outage and dangerous for a typo: a wrong endpoint name would
leave flush-fade and ls-ratio-fade silently never firing, with nothing in the
logs to say why. This file is the guard against exactly that.

It caught one real bug: the first implementation built URLs from
``urls["api"]["rest"]``, which on OKX is the unexpanded template
``https://{hostname}``, so every call failed silently.
"""

import unittest
from unittest.mock import Mock

from agent import market


class EndpointNamesExistTests(unittest.TestCase):
    """The ccxt methods we call must actually exist on the OKX client."""

    def test_declared_endpoints_are_real_ccxt_methods(self):
        import ccxt
        client = ccxt.okx()
        for method in ("publicGetRubikStatContractsOpenInterestHistory",
                       "publicGetRubikStatContractsLongShortAccountRatio"):
            with self.subTest(endpoint=method):
                self.assertTrue(
                    callable(getattr(client, method, None)),
                    f"ccxt no longer exposes {method}; the enrichment field "
                    f"it backs would silently stop populating")


class FakeExchange:
    def __init__(self, rows, method=None):
        self.rows = rows
        self.method = method
        self.calls = []
        self.x = Mock()
        self.x.market.return_value = {"info": {"instId": "BTC-USDT-SWAP"}}

    def public_call(self, method, params):
        self.calls.append((method, params))
        if self.method and method != self.method:
            return []
        return self.rows


class OpenInterestChangeTests(unittest.TestCase):
    def setUp(self):
        market._series_cache.clear()

    def _rows(self, values):
        # OKX returns newest first: [ts, oi, oiCcy, oiUsd]
        return [["0", "0", "0", str(v)] for v in values]

    def test_change_is_measured_against_older_bars(self):
        ex = FakeExchange(self._rows([110, 105, 104, 102, 100, 99]))
        out = market._open_interest_change(ex, "BTC/USDT:USDT")
        self.assertAlmostEqual(out["oi_change_1h_pct"], 4.762, places=2)
        self.assertAlmostEqual(out["oi_change_4h_pct"], 10.0, places=3)

    def test_a_falling_series_reports_negative_change(self):
        # The direction the flush-fade contract keys on.
        ex = FakeExchange(self._rows([90, 95, 96, 98, 100, 101]))
        out = market._open_interest_change(ex, "BTC/USDT:USDT")
        self.assertLess(out["oi_change_4h_pct"], 0)

    def test_too_few_rows_degrades_to_none(self):
        ex = FakeExchange(self._rows([100, 99]))
        self.assertEqual(
            market._open_interest_change(ex, "BTC/USDT:USDT"),
            {"oi_change_1h_pct": None, "oi_change_4h_pct": None})

    def test_an_outage_degrades_instead_of_raising(self):
        ex = FakeExchange([])
        self.assertIsNone(
            market._open_interest_change(ex, "BTC/USDT:USDT")["oi_change_4h_pct"])


class LongShortRatioTests(unittest.TestCase):
    def setUp(self):
        market._series_cache.clear()

    def test_percentile_places_the_current_value_in_its_own_range(self):
        # Newest first. 2.0 is the highest of the six, so 100th percentile.
        rows = [["0", v] for v in ("2.0", "1.0", "1.1", "1.2", "0.9", "1.3")]
        out = market._long_short_ratio(FakeExchange(rows), "BTC/USDT:USDT")
        self.assertEqual(out["long_short_ratio"], 2.0)
        self.assertAlmostEqual(out["long_short_percentile_30"], 100.0)

    def test_a_low_reading_sits_at_the_bottom_of_its_range(self):
        rows = [["0", v] for v in ("0.5", "1.0", "1.1", "1.2", "0.9", "1.3")]
        out = market._long_short_ratio(FakeExchange(rows), "BTC/USDT:USDT")
        self.assertLess(out["long_short_percentile_30"], 20.0)

    def test_it_is_queried_per_currency_not_per_instrument(self):
        # Load-bearing caveat: OKX aggregates this across every contract for
        # the coin, which is why the contract keys on percentile, not level.
        ex = FakeExchange([["0", "1.5"]])
        market._long_short_ratio(ex, "BTC/USDT:USDT")
        _, params = ex.calls[0]
        self.assertEqual(params.get("ccy"), "BTC")
        self.assertNotIn("instId", params)

    def test_an_outage_degrades_instead_of_raising(self):
        out = market._long_short_ratio(FakeExchange([]), "BTC/USDT:USDT")
        self.assertIsNone(out["long_short_ratio"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        market._series_cache.clear()

    def test_the_series_is_fetched_once_per_ttl(self):
        # Hourly data behind a 5-minute cycle: refetching every cycle would
        # be twelve times the rate-limit pressure for the same numbers.
        ex = FakeExchange([["0", "0", "0", str(v)] for v in range(10)])
        for _ in range(4):
            market._open_interest_change(ex, "BTC/USDT:USDT")
        self.assertEqual(len(ex.calls), 1)

    def test_symbols_are_cached_independently(self):
        ex = FakeExchange([["0", "0", "0", str(v)] for v in range(10)])
        market._open_interest_change(ex, "BTC/USDT:USDT")
        market._open_interest_change(ex, "ETH/USDT:USDT")
        self.assertEqual(len(ex.calls), 2)


if __name__ == "__main__":
    unittest.main()
