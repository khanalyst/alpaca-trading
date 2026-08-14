"""A refused session must say why it was refused.

A replay that declines every opportunity is either an edgeless corpus or one
the policy cannot price, and those demand opposite responses: the first is a
research verdict to keep collecting against, the second is a data fault to go
fix.  Before this, every refusal in ``_replay_session`` was a bare ``return
None``, so the two produced byte-identical output and a cycle could only
report that everything "was refused without a recorded reason".

These tests pin the reason surviving the whole way out: from the replay, onto
the no-trade opportunity row, into the fill-quality summary the gates read.
"""

from datetime import datetime, timedelta, timezone
import unittest

from research.costs import ReplayPolicy
from research.edge_discovery_core import _opportunity_rows
from research.gates import fill_source_summary, unevaluable_reason
from research.ibr import IBRConfig, replay_ibr
from research.market_data import normalize_underlying_bar, normalize_quote

OPEN = datetime(2026, 8, 12, 13, 30, tzinfo=timezone.utc)  # 09:30 America/New_York
RANGE_MINUTES = 15


def _bar(minute: int, open_: float, high: float, low: float, close: float):
    ts = OPEN + timedelta(minutes=minute)
    complete = (ts + timedelta(minutes=1)).isoformat()
    return normalize_underlying_bar(
        {"kind": "bar", "symbol": "SPY", "timestamp": ts.isoformat(),
         "as_of": complete, "observed_at": complete, "open": open_, "high": high,
         "low": low, "close": close, "volume": 1000},
        provider="alpaca", feed="iex")


def _session():
    """A complete opening range, a breakout, and an adjacent entry and hold."""
    bars = [_bar(i, 100, 100.1, 99.9, 100) for i in range(RANGE_MINUTES)]
    bars.append(_bar(RANGE_MINUTES, 100, 101.0, 99.9, 100.9))
    bars += [_bar(i, 100.9, 101.0, 100.8, 100.9)
             for i in range(RANGE_MINUTES + 1, 30)]
    return bars


def _quotes():
    rows = []
    for minute in range(RANGE_MINUTES, 31):
        ts = (OPEN + timedelta(minutes=minute)).isoformat()
        rows.append(normalize_quote(
            {"symbol": "SPY", "timestamp": ts, "as_of": ts, "observed_at": ts,
             "bid": 100.88, "ask": 100.92, "bid_size": 100, "ask_size": 100},
            provider="alpaca", feed="iex"))
    return rows


class RefusalReasonTest(unittest.TestCase):
    def setUp(self):
        self.cfg = IBRConfig(range_minutes=RANGE_MINUTES)

    def test_unpriceable_corpus_names_the_missing_quote(self):
        """Strict policy with no recorded quote is a data fault, not a verdict."""
        result = replay_ibr(_session(), config=self.cfg, vehicle="equity",
                            quotes=None)

        self.assertEqual([], result.trades)
        self.assertEqual(["no_quote_at_entry"],
                         [refusal.reason for refusal in result.refusals])

    def test_reason_reaches_the_gate_summary(self):
        """The reason has to survive the join onto the no-trade row."""
        bars = _session()
        result = replay_ibr(bars, config=self.cfg, vehicle="equity", quotes=None)

        rows = _opportunity_rows(result, bars, "equity")
        self.assertEqual(1, len(rows))
        self.assertEqual("no_quote_at_entry", rows[0]["reject_reason"])

        summary = fill_source_summary(rows, vehicle="equity")
        self.assertTrue(summary["priced_nothing"])
        self.assertEqual({"no_quote_at_entry": 1}, summary["reject_reasons"])
        self.assertEqual("no_quote_at_entry", summary["dominant_reject_reason"])
        # The cycle-level verdict now names the fault instead of shrugging.
        self.assertEqual(
            "no_quote_at_entry",
            unevaluable_reason([{"fill_quality": {"fit": summary}}]))

    def test_incomplete_opening_range_reports_its_shortfall(self):
        """Knowing it was the range, and by how much, is the actionable part."""
        result = replay_ibr(_session()[1:], config=self.cfg, vehicle="equity")

        self.assertEqual(1, len(result.refusals))
        refusal = result.refusals[0]
        self.assertEqual("incomplete_opening_range", refusal.reason)
        self.assertEqual({"observed": RANGE_MINUTES - 1,
                          "expected": RANGE_MINUTES}, dict(refusal.detail))

    def test_priced_session_records_no_refusal(self):
        """A corpus the policy can price must stay exactly as it was."""
        result = replay_ibr(_session(), config=self.cfg, vehicle="equity",
                            quotes=_quotes())

        self.assertEqual(1, len(result.trades))
        self.assertEqual([], result.refusals)

    def test_bar_fallback_path_is_unchanged(self):
        """Opting out of strict market data still prices from the bar."""
        loose = IBRConfig(range_minutes=RANGE_MINUTES,
                          policy=ReplayPolicy(strict_market_data=False))
        result = replay_ibr(_session(), config=loose, vehicle="equity",
                            quotes=None)

        self.assertEqual(1, len(result.trades))
        self.assertEqual([], result.refusals)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
