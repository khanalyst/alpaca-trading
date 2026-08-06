"""B0.5: the observation events, and their refusal to interrupt trading.

Two events start collecting here. ``snapshot_enrichment`` carries the fields
the model is not shown; ``book_state`` carries the depth and spread reading
that the entry guard already takes and currently throws away on every symbol
it does not reject.

The tests below care about one property above all: an observation failure is
a row of nulls, never an exception. Every field recorded in this batch exists
to answer a question in three months' time, and no order depends on any of
them. A statistics outage that stopped the agent trading would be a safety
regression introduced by a research feature, which is the worst possible
trade.
"""

import json
import unittest
from unittest.mock import Mock, patch

from agent.engine import Engine
from tests.helpers import valid_config
from tests.research.test_enrichment_isolation import enriched, symbol_snapshot


class ObservationRecordingTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.engine._audit_json = staticmethod(json.dumps)
        self.engine.ex = Mock()
        self.engine.ex.book_state.return_value = {
            "symbol": "BTC/USDT:USDT", "mid": 100.01, "spread_pct": 0.02,
            "bid_depth_usd": 412_000.0, "ask_depth_usd": 398_000.0,
            "top_bid_size": 12.0, "top_ask_size": 9.0,
            "band_pct": 0.35, "book_ts": 1_760_000_000_000, "error": None,
        }

    @staticmethod
    def _events(log_event):
        return [(call.args[0], json.loads(call.args[1]))
                for call in log_event.call_args_list]

    def _snapshot(self, **overrides):
        return {
            "BTC/USDT:USDT": enriched(symbol_snapshot(**overrides)),
            "_market_context": {
                "benchmark": "BTC/USDT:USDT",
                "_enrichment": {"btc_ref_return_1_pct": -0.42},
            },
        }

    @patch("agent.engine.state.log_event")
    def test_book_state_is_written_for_every_symbol(self, log_event):
        snapshot = self._snapshot()
        snapshot["ETH/USDT:USDT"] = enriched(symbol_snapshot())

        self.engine._record_observations(snapshot)

        books = [p for kind, p in self._events(log_event)
                 if kind == "book_state"]
        self.assertEqual(len(books), 2)
        self.assertEqual(self.engine.ex.book_state.call_count, 2)

    @patch("agent.engine.state.log_event")
    def test_book_state_is_written_on_a_passing_reading(self, log_event):
        """The point of the batch: passing observations were being discarded.

        A cascade's depth collapse is only measurable against the ordinary
        readings either side of it, so 'nothing was wrong' is data.
        """
        self.engine._record_observations(self._snapshot())

        books = [p for kind, p in self._events(log_event)
                 if kind == "book_state"]
        self.assertEqual(len(books), 1)
        self.assertIsNone(books[0]["error"])
        self.assertEqual(books[0]["bid_depth_usd"], 412_000.0)
        self.assertEqual(books[0]["signal_ts"], 1_760_000_000_000)

    @patch("agent.engine.state.log_event")
    def test_book_state_is_written_when_the_reading_failed(self, log_event):
        self.engine.ex.book_state.return_value = {
            "symbol": "BTC/USDT:USDT", "mid": None, "spread_pct": None,
            "bid_depth_usd": None, "ask_depth_usd": None,
            "top_bid_size": None, "top_ask_size": None,
            "band_pct": 0.35, "book_ts": None,
            "error": "RuntimeError: no two-sided depth",
        }

        self.engine._record_observations(self._snapshot())

        books = [p for kind, p in self._events(log_event)
                 if kind == "book_state"]
        self.assertEqual(len(books), 1, "a failed read is still an event")
        self.assertIsNone(books[0]["bid_depth_usd"])
        self.assertIn("no two-sided depth", books[0]["error"])

    @patch("agent.engine.state.log_event")
    def test_enrichment_event_carries_symbol_and_market_blocks(self, log_event):
        self.engine._record_observations(self._snapshot())

        payloads = [p for kind, p in self._events(log_event)
                    if kind == "snapshot_enrichment"]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["market"]["btc_ref_return_1_pct"], -0.42)
        row = payloads[0]["symbols"]["BTC/USDT:USDT"]
        self.assertEqual(row["realised_vol_ratio_8_96"], 1.47)
        self.assertEqual(row["utc_hour"], 14)

    @patch("agent.engine.state.log_event")
    def test_market_context_is_not_treated_as_a_symbol(self, log_event):
        self.engine._record_observations(self._snapshot())

        books = [p for kind, p in self._events(log_event)
                 if kind == "book_state"]
        self.assertEqual([b["symbol"] for b in books], ["BTC/USDT:USDT"])

    def test_attached_book_enrichment_preserves_executable_levels(self):
        self.engine.ex.book_state.return_value = {
            "symbol": "BTC/USDT:USDT", "mid": 100.0,
            "best_bid": 99.99, "best_ask": 100.01,
            "spread_pct": 0.02,
            "bid_depth_usd": 9_999.0, "ask_depth_usd": 10_001.0,
            "top_bid_size": 10.0, "top_ask_size": 12.0,
            "bid_levels": [[99.99, 10.0], [99.98, 20.0]],
            "ask_levels": [[100.01, 12.0], [100.02, 18.0]],
            "contract_size": 0.01,
            "band_pct": 0.35, "book_ts": 1_760_000_000_000,
            "age_seconds": 0.1, "error": None,
        }
        snapshot = self._snapshot()

        self.engine._attach_research_book_state(snapshot)

        enrichment = snapshot["BTC/USDT:USDT"]["_enrichment"]
        self.assertEqual(
            enrichment["book_bid_levels"],
            [[99.99, 10.0], [99.98, 20.0]],
        )
        self.assertEqual(
            enrichment["book_ask_levels"],
            [[100.01, 12.0], [100.02, 18.0]],
        )
        self.assertEqual(enrichment["book_contract_size"], 0.01)

    def test_attached_book_enrichment_rejects_a_non_finite_ladder(self):
        self.engine.ex.book_state.return_value = {
            "symbol": "BTC/USDT:USDT", "mid": 100.0,
            "best_bid": 99.99, "best_ask": 100.01,
            "spread_pct": 0.02,
            "bid_depth_usd": 9_999.0, "ask_depth_usd": 10_001.0,
            "top_bid_size": 10.0, "top_ask_size": 12.0,
            "bid_levels": [[99.99, float("nan")]],
            "ask_levels": [[100.01, 12.0]],
            "contract_size": 0.01,
            "band_pct": 0.35, "book_ts": 1_760_000_000_000,
            "age_seconds": 0.1, "error": None,
        }
        snapshot = self._snapshot()

        self.engine._attach_research_book_state(snapshot)

        enrichment = snapshot["BTC/USDT:USDT"]["_enrichment"]
        self.assertIsNone(enrichment["book_bid_levels"])
        self.assertEqual(enrichment["book_ask_levels"], [[100.01, 12.0]])
        # Rejecting depth silently is what let six of seven strategies sit
        # 63-100% starved for a week with nothing logged, so the reason must
        # travel with the observation rather than being inferred later.
        self.assertIsNotNone(enrichment["book_observation_error"])
        self.assertIn("level 0", enrichment["book_observation_error"])
        self.assertIn("bid depth", enrichment["book_observation_error"])
        self.assertEqual(enrichment["book_bid_levels_observed"], 1)
        self.assertEqual(enrichment["book_ask_levels_observed"], 1)


class ObservationNeverInterruptsTrading(unittest.TestCase):
    """The acceptance criterion that outranks every field in the batch."""

    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.engine._audit_json = staticmethod(json.dumps)
        self.engine.ex = Mock()

    @patch("agent.engine.state.log_event")
    def test_a_raising_book_reader_does_not_propagate(self, log_event):
        self.engine.ex.book_state.side_effect = RuntimeError("okx is down")

        # Must not raise. The cycle continues and trades normally.
        self.engine._record_observations(
            {"BTC/USDT:USDT": enriched(symbol_snapshot())})

        kinds = [call.args[0] for call in log_event.call_args_list]
        self.assertIn("snapshot_enrichment", kinds)

    @patch("agent.engine.state.log_event")
    def test_one_bad_symbol_does_not_stop_the_others(self, log_event):
        def flaky(symbol, *args, **kwargs):
            if symbol == "BTC/USDT:USDT":
                raise RuntimeError("rate limited")
            return {"symbol": symbol, "mid": 1.0, "spread_pct": 0.01,
                    "bid_depth_usd": 1.0, "ask_depth_usd": 1.0,
                    "top_bid_size": 1.0, "top_ask_size": 1.0,
                    "band_pct": 0.35, "book_ts": 1, "error": None}

        self.engine.ex.book_state.side_effect = flaky
        self.engine._record_observations({
            "BTC/USDT:USDT": enriched(symbol_snapshot()),
            "ETH/USDT:USDT": enriched(symbol_snapshot()),
        })

        books = [json.loads(c.args[1]) for c in log_event.call_args_list
                 if c.args[0] == "book_state"]
        self.assertEqual([b["symbol"] for b in books], ["ETH/USDT:USDT"])

    @patch("agent.engine.state.log_event")
    def test_a_snapshot_with_no_enrichment_is_still_recorded(self, log_event):
        """An older snapshot, or one built before this batch, must not raise."""
        self.engine.ex.book_state.return_value = {
            "symbol": "BTC/USDT:USDT", "mid": None, "spread_pct": None,
            "bid_depth_usd": None, "ask_depth_usd": None,
            "top_bid_size": None, "top_ask_size": None,
            "band_pct": 0.35, "book_ts": None, "error": None}

        self.engine._record_observations(
            {"BTC/USDT:USDT": symbol_snapshot()})     # no _enrichment block

        payloads = [json.loads(c.args[1]) for c in log_event.call_args_list
                    if c.args[0] == "snapshot_enrichment"]
        self.assertEqual(payloads[0]["symbols"], {"BTC/USDT:USDT": None})


class BookStateReaderTests(unittest.TestCase):
    """The reader itself: it observes, and it never judges."""

    def _exchange(self, book):
        from agent.exchange import Exchange
        ex = Exchange.__new__(Exchange)
        ex.x = Mock()
        ex.x.fetch_order_book.return_value = book
        ex.x.market.return_value = {"contractSize": 1}
        ex.retry = lambda fn, *a, **k: fn(*a, **k)
        return ex

    def test_depth_is_summed_only_inside_the_band(self):
        # Mid is 100. A 0.35% band reaches 99.65 / 100.35, so the third
        # level on each side sits outside it and must not be counted.
        ex = self._exchange({
            "bids": [[99.9, 10], [99.7, 10], [99.0, 999]],
            "asks": [[100.1, 10], [100.3, 10], [101.0, 999]],
            "timestamp": 1_760_000_000_000,
        })

        state = ex.book_state("BTC/USDT:USDT", band_pct=0.35)

        self.assertAlmostEqual(state["bid_depth_usd"], 99.9 * 10 + 99.7 * 10, 2)
        self.assertAlmostEqual(state["ask_depth_usd"],
                               100.1 * 10 + 100.3 * 10, 2)
        self.assertAlmostEqual(state["mid"], 100.0, 6)
        self.assertIsNone(state["error"])

    def test_contract_size_is_applied_to_depth(self):
        ex = self._exchange({
            "bids": [[100.0, 2]], "asks": [[100.0, 2]],
            "timestamp": 1_760_000_000_000,
        })
        ex.x.market.return_value = {"contractSize": 10}

        state = ex.book_state("BTC/USDT:USDT")

        self.assertAlmostEqual(state["bid_depth_usd"], 100.0 * 2 * 10, 2)

    def test_a_one_sided_book_returns_nulls_not_an_exception(self):
        ex = self._exchange({"bids": [], "asks": [[100.0, 1]],
                             "timestamp": 1_760_000_000_000})

        state = ex.book_state("BTC/USDT:USDT")

        self.assertIsNone(state["mid"])
        self.assertEqual(state["error"], "no two-sided depth")

    def test_a_raising_fetch_returns_nulls_not_an_exception(self):
        ex = self._exchange({})
        ex.x.fetch_order_book.side_effect = RuntimeError("connection reset")

        state = ex.book_state("BTC/USDT:USDT")

        self.assertIsNone(state["bid_depth_usd"])
        self.assertIn("connection reset", state["error"])

    def test_a_wide_spread_is_recorded_rather_than_rejected(self):
        """guarded_entry_limit rejects these. The observer must not."""
        ex = self._exchange({
            "bids": [[90.0, 5]], "asks": [[110.0, 5]],
            "timestamp": 1_760_000_000_000,
        })

        state = ex.book_state("BTC/USDT:USDT")

        self.assertIsNone(state["error"])
        self.assertAlmostEqual(state["spread_pct"], 20.0, 6)


if __name__ == "__main__":
    unittest.main()
