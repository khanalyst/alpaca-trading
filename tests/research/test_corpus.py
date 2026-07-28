"""B1: reading the corpus the agent has been writing all along.

The tests here are mostly about refusing to lie. A corpus loader has three
ways to mislead quietly, and each has a test:

- Dying on one malformed row, so the loader cannot read a real journal.
- Skipping malformed rows silently, so a partial read looks like a clean one.
- Counting the same signal bar three times, because a 300s cycle observes a
  900s bar roughly three times, which would inflate the sample and blend a
  decision with its own aftermath.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent import brain
from research import corpus


def user_message(snapshot: dict, portfolio: dict, max_new: int = 3) -> str:
    """Built by the real prompt builder, so the parser is tested against it."""
    return brain.LLM._user_message(snapshot, portfolio, max_new)


def anthropic_payload(snapshot, portfolio, max_new=3) -> str:
    return json.dumps({
        "provider": "anthropic",
        "request": {
            "model": "test-model",
            "system": [{"type": "text", "text": "SYSTEM PROMPT"}],
            "messages": [
                {"role": "user",
                 "content": user_message(snapshot, portfolio, max_new)},
            ],
        },
    })


def openai_payload(snapshot, portfolio, max_new=3) -> str:
    return json.dumps({
        "provider": "openai",
        "request": {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "SYSTEM PROMPT"},
                {"role": "user",
                 "content": user_message(snapshot, portfolio, max_new)},
            ],
        },
    })


def snapshot(signal_ts: int = 1_760_000_000_000, price: float = 100.0) -> dict:
    return {
        "BTC/USDT:USDT": {
            "price": price, "signal_ts": signal_ts, "atr_1h_pct": 0.8,
            "trend_1h": "up", "spread_pct": 0.02,
        },
        "_market_context": {"benchmark": "BTC/USDT:USDT", "regime": "trend"},
    }


class CorpusFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = Path(self.dir.name) / "journal.db"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
            "run_id TEXT, cycle_id TEXT, setup_id TEXT, variant_id TEXT, "
            "strategy_config_version TEXT)")
        conn.execute(
            "CREATE TABLE trades (ts REAL, symbol TEXT, action TEXT, "
            "trade_id TEXT, realized_pnl_usd REAL)")
        conn.commit()
        conn.close()

    def event(self, kind, payload, ts=1.0, run_id="run-a", cycle_id="c1",
              setup_id=None, variant_id="live"):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
            (ts, kind, payload, run_id, cycle_id, setup_id, variant_id, "sfp"))
        conn.commit()
        conn.close()

    def trade(self, action, trade_id, ts=1.0, pnl=0.0):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO trades VALUES (?,?,?,?,?)",
                     (ts, "BTC/USDT:USDT", action, trade_id, pnl))
        conn.commit()
        conn.close()


class ProviderShapeTests(CorpusFixture):
    def test_anthropic_payload_parses(self):
        self.event("llm_input", anthropic_payload(snapshot(), {"eq": 1000}))

        cycles, report = corpus.load_cycles(self.db)

        self.assertEqual(len(cycles), 1)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(cycles[0].provider, "anthropic")
        self.assertEqual(cycles[0].snapshot["BTC/USDT:USDT"]["price"], 100.0)
        self.assertEqual(cycles[0].portfolio, {"eq": 1000})
        self.assertEqual(cycles[0].max_new, 3)

    def test_openai_payload_parses(self):
        self.event("llm_input", openai_payload(snapshot(), {"eq": 2000}, 2))

        cycles, report = corpus.load_cycles(self.db)

        self.assertEqual(len(cycles), 1)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(cycles[0].provider, "openai")
        self.assertEqual(cycles[0].max_new, 2)

    def test_a_journal_spanning_a_provider_switch_reads_both(self):
        """The user message is found by role, never by index."""
        self.event("llm_input", anthropic_payload(snapshot(), {}), ts=1.0)
        self.event("llm_input", openai_payload(snapshot(), {}), ts=2.0)

        cycles, report = corpus.load_cycles(self.db)

        self.assertEqual(report.skipped, 0)
        self.assertEqual([c.provider for c in cycles],
                         ["anthropic", "openai"])

    def test_attribution_columns_are_read(self):
        self.event("llm_input", anthropic_payload(snapshot(), {}))
        cycles, _ = corpus.load_cycles(self.db)
        self.assertEqual(cycles[0].variant_id, "live")
        self.assertEqual(cycles[0].strategy_config_version, "sfp")


class MalformedRowTests(CorpusFixture):
    def test_a_truncated_payload_is_counted_not_raised(self):
        self.event("llm_input", '{"provider":"anthropic","req')
        self.event("llm_input", anthropic_payload(snapshot(), {}), ts=2.0)

        cycles, report = corpus.load_cycles(self.db)

        self.assertEqual(len(cycles), 1)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.reasons["payload is not json"], 1)

    def test_a_request_with_no_user_message_is_counted(self):
        self.event("llm_input", json.dumps({
            "provider": "anthropic",
            "request": {"messages": [{"role": "system", "content": "x"}]}}))

        cycles, report = corpus.load_cycles(self.db)

        self.assertEqual(cycles, [])
        self.assertEqual(report.reasons["no user message in request"], 1)

    def test_an_unparseable_user_message_is_counted(self):
        self.event("llm_input", json.dumps({
            "provider": "anthropic",
            "request": {"messages": [
                {"role": "user", "content": "not the expected format"}]}}))

        cycles, report = corpus.load_cycles(self.db)

        self.assertEqual(cycles, [])
        self.assertEqual(report.reasons["user message did not parse"], 1)

    def test_malformed_snapshot_json_does_not_raise(self):
        broken = (f"{corpus.SNAPSHOT_MARKER}\n{{not json}}\n\n"
                  f"{corpus.PORTFOLIO_MARKER}\n{{}}")
        self.event("llm_input", json.dumps({
            "provider": "anthropic",
            "request": {"messages": [{"role": "user", "content": broken}]}}))

        cycles, report = corpus.load_cycles(self.db)

        self.assertEqual(cycles, [])
        self.assertEqual(report.skipped, 1)

    def test_skipping_is_visible_in_the_report(self):
        """A partial read must never look like a clean one."""
        for i in range(3):
            self.event("llm_input", "garbage", ts=float(i))

        _, report = corpus.load_cycles(self.db)

        self.assertEqual(report.rows, 3)
        self.assertEqual(report.parsed, 0)
        self.assertEqual(report.skipped, 3)


class SignalBarDeduplicationTests(CorpusFixture):
    def test_the_first_observation_of_a_bar_wins(self):
        # Same signal_ts observed three times, as a 300s cycle observes a
        # 900s bar. The agent acted on the first.
        for i, price in enumerate((100.0, 105.0, 110.0)):
            self.event("llm_input",
                       anthropic_payload(snapshot(price=price), {}),
                       ts=float(i), cycle_id=f"c{i}")

        cycles, _ = corpus.load_cycles(self.db)
        bars = corpus.index_by_signal_bar(cycles)

        self.assertEqual(len(bars), 1)
        key = ("BTC/USDT:USDT", 1_760_000_000_000)
        self.assertEqual(bars[key]["snapshot"]["price"], 100.0)

    def test_distinct_bars_are_kept_apart(self):
        self.event("llm_input", anthropic_payload(
            snapshot(signal_ts=1_760_000_000_000), {}), ts=1.0)
        self.event("llm_input", anthropic_payload(
            snapshot(signal_ts=1_760_000_900_000), {}), ts=2.0)

        cycles, _ = corpus.load_cycles(self.db)

        self.assertEqual(len(corpus.index_by_signal_bar(cycles)), 2)

    def test_market_context_is_not_indexed_as_a_symbol(self):
        self.event("llm_input", anthropic_payload(snapshot(), {}))
        cycles, _ = corpus.load_cycles(self.db)
        bars = corpus.index_by_signal_bar(cycles)
        self.assertEqual([symbol for symbol, _ in bars], ["BTC/USDT:USDT"])

    def test_a_symbol_with_no_signal_ts_is_skipped_not_crashed(self):
        snap = {"BTC/USDT:USDT": {"price": 1.0}}          # no signal_ts
        self.event("llm_input", anthropic_payload(snap, {}))
        cycles, _ = corpus.load_cycles(self.db)
        self.assertEqual(corpus.index_by_signal_bar(cycles), {})


class UptimeGapTests(CorpusFixture):
    def test_a_gap_is_reported(self):
        self.event("llm_input", anthropic_payload(snapshot(), {}), ts=0.0)
        self.event("llm_input", anthropic_payload(snapshot(), {}),
                   ts=7200.0, run_id="run-b")

        cycles, _ = corpus.load_cycles(self.db)
        gaps = corpus.uptime_gaps(cycles)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["seconds"], 7200.0)
        self.assertTrue(gaps[0]["crossed_run"])

    def test_normal_cadence_is_not_a_gap(self):
        for i in range(5):
            self.event("llm_input", anthropic_payload(snapshot(), {}),
                       ts=float(i * 300))

        cycles, _ = corpus.load_cycles(self.db)

        self.assertEqual(corpus.uptime_gaps(cycles), [])


class ModelOutputTests(CorpusFixture):
    def test_raw_text_and_attempts_are_read(self):
        self.event("llm_output", json.dumps({
            "provider": "anthropic",
            "request_attempts": [{"model": "m"}, {"model": "m"}],
            "response": {"id": "resp-1", "raw_text": '{"decisions":[]}'},
        }))

        outputs, _ = corpus.load_model_outputs(self.db)

        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].response_id, "resp-1")
        self.assertEqual(outputs[0].request_attempts, 2)

    def test_a_malformed_response_does_not_raise(self):
        self.event("llm_output", json.dumps({
            "response": {"id": "r", "raw_text": "the model said words"}}))

        outputs, _ = corpus.load_model_outputs(self.db)

        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].parsed_decisions, [])


class StatsTests(CorpusFixture):
    def test_stats_runs_against_an_empty_journal(self):
        s = corpus.stats(self.db)

        self.assertEqual(s["cycles"], 0)
        self.assertEqual(s["matched_round_trips"], 0)
        self.assertIn("corpus:", corpus.format_stats(s))

    def test_matched_round_trips_counts_closed_opens_only(self):
        self.trade("open", "t1")
        self.trade("close", "t1")
        self.trade("open", "t2")            # still open
        self.trade("close", "t3")           # close with no recorded open

        s = corpus.stats(self.db)

        self.assertEqual(s["opens"], 2)
        self.assertEqual(s["matched_round_trips"], 1)

    def test_stats_counts_the_b05_observation_events(self):
        self.event("book_state", json.dumps({"symbol": "BTC/USDT:USDT"}))
        self.event("snapshot_enrichment", json.dumps({"symbols": {}}))

        s = corpus.stats(self.db)

        self.assertEqual(s["book_state_events"], 1)
        self.assertEqual(s["enrichment_events"], 1)

    def test_format_states_the_sample_a_parameter_axis_needs(self):
        self.trade("open", "t1")
        self.trade("close", "t1")

        text = corpus.format_stats(corpus.stats(self.db))

        self.assertIn("300", text)
        self.assertIn("Conditioning axes reuse", text)

    def test_report_surfaces_skipped_rows(self):
        self.event("llm_input", "garbage")

        text = corpus.format_stats(corpus.stats(self.db))

        self.assertIn("unparseable", text)


class RealPromptRoundTripTests(unittest.TestCase):
    """The parser must handle what the real builder produces, exactly."""

    def test_round_trip_through_the_production_prompt_builder(self):
        snap = snapshot()
        portfolio = {"equity_usdt": 1234.5, "positions": []}
        message = brain.LLM._user_message(snap, portfolio, 2)

        parsed = corpus.parse_user_message(message)

        self.assertIsNotNone(parsed)
        got_snapshot, got_portfolio, max_new = parsed
        self.assertEqual(got_portfolio, portfolio)
        self.assertEqual(max_new, 2)
        self.assertEqual(got_snapshot["BTC/USDT:USDT"]["price"], 100.0)

    def test_enrichment_is_absent_from_what_the_corpus_reads_back(self):
        """B0.5 D1, observed from the other end of the pipe.

        The corpus is what the model saw. Enrichment is journalled
        separately, precisely so that this stays true.
        """
        snap = snapshot()
        snap["BTC/USDT:USDT"]["_enrichment"] = {"utc_hour": 14}

        parsed = corpus.parse_user_message(
            brain.LLM._user_message(snap, {}, 1))

        self.assertNotIn("_enrichment", parsed[0]["BTC/USDT:USDT"])

    def test_max_new_of_zero_is_read_as_zero(self):
        parsed = corpus.parse_user_message(
            brain.LLM._user_message(snapshot(), {}, 0))
        self.assertEqual(parsed[2], 0)


if __name__ == "__main__":
    unittest.main()
