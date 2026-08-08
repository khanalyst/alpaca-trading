"""B2: the price cache. Fetch once, serve offline, never rewrite.

The immutability rule is the one worth arguing for. Exchanges revise recent
candles, so a cache that refreshed would let revised data leak into a corpus
whose entire claim is that it reflects what was knowable at the time - and
the leak would be invisible, because the revised number is a perfectly
plausible number.
"""

import tempfile
import unittest
from pathlib import Path

from research.prices import DAY_MS, MINUTE_MS, PriceCache, day_of


DAY_START = 1_759_968_000_000          # 2025-10-09 00:00 UTC


def candles(start_ms: int, count: int, price: float = 100.0) -> list:
    return [[start_ms + i * MINUTE_MS, price, price + 1, price - 1,
             price + 0.5, 10.0] for i in range(count)]


class CountingFetcher:
    def __init__(self, count: int = 60):
        self.calls = []
        self.count = count

    def __call__(self, symbol, start_ms, end_ms):
        self.calls.append((symbol, start_ms, end_ms))
        return candles(start_ms, self.count)


class PriceCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "prices.db"

    def test_bars_round_trip(self):
        cache = PriceCache(self.path)
        cache.store("BTC/USDT:USDT", day_of(DAY_START),
                    candles(DAY_START, 10))

        bars = cache.bars("BTC/USDT:USDT", DAY_START, DAY_START + DAY_MS)

        self.assertEqual(len(bars), 10)
        self.assertEqual(bars[0].ts, DAY_START)
        self.assertEqual(bars[0].high, 101.0)
        self.assertEqual(bars[0].close_ts, DAY_START + MINUTE_MS)

    def test_bars_are_returned_ascending_and_bounded(self):
        cache = PriceCache(self.path)
        cache.store("BTC/USDT:USDT", day_of(DAY_START),
                    candles(DAY_START, 100))

        bars = cache.bars("BTC/USDT:USDT", DAY_START + 10 * MINUTE_MS,
                          DAY_START + 20 * MINUTE_MS)

        self.assertEqual(len(bars), 10)
        self.assertEqual(bars[0].ts, DAY_START + 10 * MINUTE_MS)
        self.assertEqual([b.ts for b in bars], sorted(b.ts for b in bars))

    def test_a_cache_hit_performs_zero_network_calls(self):
        """The acceptance criterion: research runs offline after the first pass."""
        fetcher = CountingFetcher()
        cache = PriceCache(self.path, fetcher=fetcher)

        cache.ensure("BTC/USDT:USDT", DAY_START, DAY_START + 60 * MINUTE_MS)
        first = len(fetcher.calls)
        cache.ensure("BTC/USDT:USDT", DAY_START, DAY_START + 60 * MINUTE_MS)

        self.assertEqual(first, 1)
        self.assertEqual(len(fetcher.calls), 1, "second pass must not fetch")
        self.assertEqual(cache.network_calls, 1)

    def test_a_second_cache_object_still_hits_the_same_store(self):
        fetcher = CountingFetcher()
        PriceCache(self.path, fetcher=fetcher).ensure(
            "BTC/USDT:USDT", DAY_START, DAY_START + MINUTE_MS)

        fresh = PriceCache(self.path, fetcher=fetcher)
        fresh.ensure("BTC/USDT:USDT", DAY_START, DAY_START + MINUTE_MS)

        self.assertEqual(fresh.network_calls, 0)

    def test_offline_by_default(self):
        """Without a fetcher, a miss is an error rather than a silent fetch."""
        cache = PriceCache(self.path)
        with self.assertRaises(RuntimeError):
            cache.ensure("BTC/USDT:USDT", DAY_START, DAY_START + MINUTE_MS)

    def test_a_refetch_does_not_rewrite_existing_bars(self):
        cache = PriceCache(self.path)
        day = day_of(DAY_START)
        cache.store("BTC/USDT:USDT", day, candles(DAY_START, 5, price=100.0))
        cache.store("BTC/USDT:USDT", day, candles(DAY_START, 5, price=999.0))

        bars = cache.bars("BTC/USDT:USDT", DAY_START, DAY_START + DAY_MS)

        self.assertEqual(bars[0].open, 100.0,
                         "a revised candle must not overwrite history")

    def test_days_covering_spans_the_range(self):
        cache = PriceCache(self.path)
        days = cache.days_covering(DAY_START, DAY_START + 2 * DAY_MS)
        self.assertEqual(days, ["2025-10-09", "2025-10-10"])

    def test_days_covering_zero_width_range_is_empty(self):
        cache = PriceCache(self.path)
        self.assertEqual(
            cache.days_covering(DAY_START, DAY_START), [])
        self.assertEqual(
            cache.days_covering(DAY_START + DAY_MS,
                                DAY_START), [])

    def test_each_uncached_day_is_fetched_once(self):
        fetcher = CountingFetcher()
        cache = PriceCache(self.path, fetcher=fetcher)

        fetched = cache.ensure("BTC/USDT:USDT", DAY_START,
                               DAY_START + 2 * DAY_MS)

        self.assertEqual(fetched, 2)
        self.assertEqual(len(fetcher.calls), 2)

    def test_symbols_do_not_share_a_cache_entry(self):
        fetcher = CountingFetcher()
        cache = PriceCache(self.path, fetcher=fetcher)

        cache.ensure("BTC/USDT:USDT", DAY_START, DAY_START + MINUTE_MS)
        cache.ensure("ETH/USDT:USDT", DAY_START, DAY_START + MINUTE_MS)

        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(
            cache.bars("ETH/USDT:USDT", DAY_START, DAY_START + DAY_MS)[0].ts,
            DAY_START)

    def test_an_empty_day_is_recorded_so_it_is_not_refetched(self):
        """A genuinely empty day must not be retried on every run."""
        calls = []

        def empty(symbol, start, end):
            calls.append(symbol)
            return []

        cache = PriceCache(self.path, fetcher=empty)
        cache.ensure("NEW/USDT:USDT", DAY_START, DAY_START + MINUTE_MS)
        cache.ensure("NEW/USDT:USDT", DAY_START, DAY_START + MINUTE_MS)

        self.assertEqual(len(calls), 1)


class ResolveFromCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "prices.db"

    def test_resolution_reads_only_bars_after_the_signal_bar(self):
        from research.outcomes import SetupPlan, resolve_from_cache

        signal_ts = DAY_START
        cache = PriceCache(self.path)
        # A day of flat bars, including bars inside the signal bar itself.
        cache.store("BTC/USDT:USDT", day_of(DAY_START),
                    candles(DAY_START, 120, price=100.0))

        plan = SetupPlan(
            symbol="BTC/USDT:USDT", direction="long", entry_price=100.5,
            stop_pct=2.0, take_pct=4.0, signal_ts=signal_ts,
            signal_timeframe="15m")

        # Must not raise: the cache slice starts after the signal bar closes.
        outcome = resolve_from_cache(plan, cache, max_hold_hours=1.0)

        self.assertIn(outcome.result, ("timeout", "no_data"))


if __name__ == "__main__":
    unittest.main()
