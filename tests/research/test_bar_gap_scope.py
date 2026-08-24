"""Adjacency is required where it changes an outcome, and nowhere else.

Rejecting a whole symbol-session because one minute is missing deletes the
observations a gap could not have affected.  On a real IEX corpus that is a
large, non-random slice of the sample — and it lands on exactly the thinner
symbols the universe was widened to include.  These tests pin the boundary:
a gap inside the bars a signal reads, or between the signal and its entry, or
inside the hold, still refuses; a gap after the position is resolved does not.
"""

from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.rule import feature_window_bars, validate_rule_spec
from research.costs import ReplayPolicy
from research.edge_lab import _read_discovery_rows
from research.factory_core import _simulate_trade, _session_bars_valid

# These fixtures carry bars but no quotes, so the replay is told explicitly
# that bar fallback is acceptable.  Pricing is not what is under test here;
# without this every outcome would be an unpriced row and the assertions
# below would pass for the wrong reason.
BAR_FALLBACK = ReplayPolicy(strict_market_data=False)

NEW_YORK = ZoneInfo("America/New_York")
# The same mechanical opening-range edge the end-to-end fixture uses: fifteen
# declining bars build the range, one high-volume bar breaks it, the move pays,
# and the rest of the session gives it back.
_CLOSES = ([100.60 - .05 * step for step in range(1, 16)] +
           [100.85] + [101.05, 101.25, 101.45] +
           [101.45 - .10 * step for step in range(1, 16)])
_VOLUMES = [1000] * 15 + [6000] + [3000] * 3 + [1200] * 15
SPEC = validate_rule_spec({"family": "opening_range_breakout",
                           "range_minutes": 15, "threshold_bps": 5.0,
                           "confirmation": "volume"})


def _rows(drop_bar: int | None = None) -> list[dict]:
    """One session of one-minute bars, optionally missing one of them."""
    opening = datetime(2026, 1, 5, 9, 30, tzinfo=NEW_YORK).astimezone(timezone.utc)
    rows, previous = [], 100.60
    for index, (close, volume) in enumerate(zip(_CLOSES, _VOLUMES)):
        if index == drop_bar:
            previous = close
            continue
        stamp = opening + timedelta(minutes=index)
        end = stamp + timedelta(minutes=1)
        rows.append({
            "kind": "bar", "provider": "test", "feed": "sip", "symbol": "AAA",
            # Mechanics-only bar source: opening prints are visible at their
            # boundaries; delayed recorder bars are covered separately.
            "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
            "observed_at": stamp.isoformat(), "open": round(previous, 4),
            "high": round(max(previous, close) + .02, 4),
            "low": round(min(previous, close) - .02, 4),
            "close": round(close, 4), "volume": volume,
        })
        previous = close
    return rows


def _bars(drop_bar: int | None = None):
    return _read_discovery_rows(_rows(drop_bar))[1]


def _trade(drop_bar: int | None = None):
    """The replayed trade for this session, or ``None`` when no signal fires.

    A returned row must be a real fill: an ``unpriced_reason`` row means the
    signal fired and could not be priced, which is a different outcome and
    would make an ``assertIsNotNone`` here vacuous.
    """
    trade = _simulate_trade(_bars(drop_bar), SPEC, [], "equity", quotes=None,
                            policy=BAR_FALLBACK)
    if trade is not None and trade.get("unpriced_reason"):
        raise AssertionError(f"fixture priced nothing: {trade['unpriced_reason']}")
    return trade


def _outcome(drop_bar: int | None = None):
    """Return the replay's terminal disposition, including explicit refusals."""
    return _simulate_trade(_bars(drop_bar), SPEC, [], "equity", quotes=None,
                           policy=BAR_FALLBACK)


class FeatureWindowTests(unittest.TestCase):
    def test_session_accumulating_families_have_no_bounded_window(self):
        for family in ("vwap_reversion", "vwap_trend"):
            spec = validate_rule_spec({"family": family})
            self.assertIsNone(feature_window_bars(spec), family)

    def test_trailing_window_covers_the_widest_input_the_family_reads(self):
        # ``trend_pullback`` reads ``slow_lookback`` closes, which is wider
        # than the prefix ``evaluate_rule_signal`` consumes on its own.
        spec = validate_rule_spec({"family": "trend_pullback", "lookback": 10,
                                   "slow_lookback": 60})
        self.assertEqual(feature_window_bars(spec), 60)

    def test_a_confirmation_widens_the_window_it_needs(self):
        plain = validate_rule_spec({"family": "mean_reversion", "lookback": 10,
                                    "slow_lookback": 50})
        confirmed = validate_rule_spec({**plain, "confirmation": "trend"})
        self.assertLess(feature_window_bars(plain), feature_window_bars(confirmed))
        self.assertEqual(feature_window_bars(confirmed), 50)


class SessionGapScopeTests(unittest.TestCase):
    def test_a_clean_session_trades(self):
        self.assertIsNotNone(_trade())

    def test_a_malformed_stream_is_still_refused_outright(self):
        bars = _bars()
        self.assertFalse(_session_bars_valid(list(reversed(bars))))
        self.assertFalse(_session_bars_valid(list(bars) + [bars[-1]]))

    def test_a_gap_after_the_position_is_resolved_keeps_the_observation(self):
        # Bar 33 is the last bar of the session, long after this trade has
        # exited.  Nothing about the signal, the entry or the hold can depend
        # on it, so dropping the session would delete a good observation.
        self.assertIsNotNone(_trade(drop_bar=33))

    def test_a_gap_inside_the_feature_window_refuses_the_signal(self):
        # Bar 14 is the last minute of the opening range: dropping it changes
        # the range the breakout is measured against.
        outcome = _outcome(drop_bar=14)
        self.assertEqual(outcome["execution_disposition"], "refused")
        self.assertFalse(outcome["signal_opportunity"])
        self.assertEqual(outcome["unpriced_reason"],
                         "no_contiguous_feature_window")
        self.assertEqual(outcome["reject_stage"], "data_validation")
        self.assertGreater(outcome["reject_detail"]["gapped_prefixes"], 0)

    def test_a_gap_between_signal_and_entry_refuses_the_signal(self):
        # Bar 16 is the entry bar for the breakout completed on bar 15.
        # Carrying the signal to the next recorded minute would enter on a
        # stale breakout.
        outcome = _outcome(drop_bar=16)
        self.assertEqual(outcome["execution_disposition"], "refused")
        self.assertTrue(outcome["signal_opportunity"])
        self.assertEqual(outcome["unpriced_reason"], "entry_bar_not_adjacent")
        self.assertEqual(outcome["reject_stage"], "entry_causality")

    def test_a_gap_after_the_exit_does_not_move_the_exit(self):
        # This trade reaches its target on bar 18.  A minute missing at bar 22
        # is downstream of everything the position depended on.
        self.assertEqual(_trade(drop_bar=22)["exit_timestamp"],
                         _trade()["exit_timestamp"])

    def test_a_gap_inside_the_hold_stops_the_walk_instead_of_crossing_it(self):
        # Bar 17 sits between the entry (bar 16) and the target hit (bar 18).
        # The position still exists — its signal and entry are intact — but it
        # must resolve on the last observed bar rather than letting the target
        # "trigger" on a bar the position could not have been carried to.
        trade = _trade(drop_bar=17)
        self.assertIsNotNone(trade)
        self.assertLess(datetime.fromisoformat(trade["exit_timestamp"]),
                        datetime.fromisoformat(_trade()["exit_timestamp"]))
        self.assertNotEqual(trade["exit_reason"], "target")


if __name__ == "__main__":
    unittest.main()
