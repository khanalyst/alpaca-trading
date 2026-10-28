"""Executable, wall-clock outcomes and registered control tiers (v2 instrument).

The original measurement anchors the outcome at the signal bar's own close
and counts its horizon in bars.  Both are kept as the default so stored v1
results reproduce; these tests pin the alternatives the v2 preregistrations
use (review findings D1-05, D1-13 and the instrument half of D1-02).
"""

from datetime import date, datetime, time, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from research import preregistered as prereg
from research.market_data import normalize_underlying_bar
from research.signal_quality import (_clock_outcome, _forward_return,
                                     measure_signal_quality)
from tests.research.test_preregistered_reproducibility import session_bars

NEW_YORK = ZoneInfo("America/New_York")
START = datetime(2026, 10, 27, 9, 30, tzinfo=NEW_YORK).astimezone(timezone.utc)


def _ladder(count: int = 70, *, missing: tuple[int, ...] = ()) -> list:
    """Bars whose open is 100 + i/100 and close 0.005 above it."""
    rows = []
    for index in range(count):
        if index in missing:
            continue
        opened = 100.0 + index / 100.0
        rows.append(normalize_underlying_bar({
            "symbol": "SPY",
            "timestamp": (START + timedelta(minutes=index)).isoformat(),
            "open": opened, "high": opened + 0.01, "low": opened - 0.01,
            "close": opened + 0.005, "volume": 1000,
            "provider": "alpaca", "feed": "iex"}))
    return rows


def _bps(exit_price: float, entry_price: float) -> float:
    return (exit_price / entry_price - 1.0) * 10_000.0


class ExecutableAnchorTests(unittest.TestCase):
    """D1-05: the outcome starts where a runtime could first trade."""

    def setUp(self):
        self.rows = _ladder()
        self.signal = 5
        self.decision = self.rows[self.signal].end

    def outcome(self, anchor, rows=None, **over):
        return _clock_outcome(
            rows or self.rows, signal_index=self.signal, horizon=60,
            anchor=anchor, direction=over.pop("direction", "long"),
            allow_backfill=False, decision_at=over.pop("decision_at", self.decision),
            **over)

    def test_next_bar_open_prices_the_entry_at_the_following_open(self):
        value, reason, exit_index = self.outcome("next_bar_open")
        self.assertIsNone(reason)
        # Entry: open of the bar starting at the signal bar's end (bar 6).
        # Exit: close of the bar ending 60 minutes later (bar 65).
        self.assertEqual(self.rows[exit_index].timestamp,
                         START + timedelta(minutes=65))
        self.assertAlmostEqual(value, _bps(100.655, 100.06))

    def test_one_bar_skip_variant_enters_a_minute_later(self):
        value, reason, exit_index = self.outcome("skip_one_bar_open")
        self.assertIsNone(reason)
        self.assertEqual(self.rows[exit_index].timestamp,
                         START + timedelta(minutes=66))
        self.assertAlmostEqual(value, _bps(100.665, 100.07))

    def test_signal_close_is_kept_as_the_diagnostic_figure(self):
        value, reason, _exit = self.outcome("signal_close")
        self.assertIsNone(reason)
        self.assertAlmostEqual(value, _bps(100.655, 100.055))
        # On a contiguous session it equals the original measurement.
        legacy, legacy_reason = _forward_return(
            self.rows, entry_index=self.signal + 1, horizon=60,
            entry_price=self.rows[self.signal].close, direction="long",
            allow_backfill=False, decision_at=self.decision)
        self.assertIsNone(legacy_reason)
        self.assertAlmostEqual(value, legacy)

    def test_short_direction_negates_the_return(self):
        long_value, _reason, _exit = self.outcome("next_bar_open")
        short_value, _reason, _exit = self.outcome("next_bar_open",
                                                   direction="short")
        self.assertEqual(short_value, -long_value)

    def test_an_entry_bar_that_finished_before_the_decision_is_refused(self):
        late = self.rows[self.signal + 1].end + timedelta(seconds=1)
        value, reason, _exit = self.outcome("next_bar_open", decision_at=late)
        self.assertIsNone(value)
        self.assertEqual(reason, "entry_bar_elapsed_before_decision")
        # The one-bar-skip variant still has a bar in progress to enter on.
        value, reason, _exit = self.outcome("skip_one_bar_open", decision_at=late)
        self.assertIsNone(reason)


class WallClockHorizonTests(unittest.TestCase):
    """D1-13: a missing minute cannot stretch the horizon."""

    def test_a_missing_minute_after_the_signal_does_not_move_the_endpoint(self):
        rows = _ladder(missing=(6,))
        signal = 5
        decision = rows[signal].end + timedelta(seconds=2)
        # The original bar count steps over the gap: its 60th bar after the
        # signal ends 61 minutes after the signal bar (kept for v1 only).
        _legacy, legacy_reason = _forward_return(
            rows, entry_index=signal + 1, horizon=60,
            entry_price=rows[signal].close, direction="long",
            allow_backfill=False, decision_at=decision)
        self.assertIsNone(legacy_reason)
        self.assertEqual(rows[signal + 60].end - rows[signal].end,
                         timedelta(minutes=61))
        value, reason, exit_index = _clock_outcome(
            rows, signal_index=signal, horizon=60, anchor="signal_close",
            direction="long", allow_backfill=False, decision_at=decision)
        self.assertIsNone(reason)
        self.assertEqual(rows[exit_index].end - rows[signal].end,
                         timedelta(minutes=60))
        self.assertAlmostEqual(value, _bps(100.655, rows[signal].close))

    def test_a_missing_entry_or_horizon_bar_leaves_no_outcome(self):
        rows = _ladder(missing=(6,))
        value, reason, _exit = _clock_outcome(
            rows, signal_index=5, horizon=60, anchor="next_bar_open",
            direction="long", allow_backfill=False,
            decision_at=rows[5].end)
        self.assertIsNone(value)
        self.assertEqual(reason, "entry_bar_missing")
        rows = _ladder(missing=(65,))
        value, reason, _exit = _clock_outcome(
            rows, signal_index=5, horizon=60, anchor="next_bar_open",
            direction="long", allow_backfill=False,
            decision_at=rows[5].end)
        self.assertIsNone(value)
        self.assertEqual(reason, "horizon_bar_missing")


class MeasurementOptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = prereg.preregistration(
            prereg.OPENING_RANGE_FADE_HYPOTHESIS_V2)["subject"]["rule_spec"]
        days = [date(2026, 10, 27) + timedelta(days=offset)
                for offset in range(0, 14) if (date(2026, 10, 27) +
                                               timedelta(days=offset)).weekday() < 5]
        cls.bars = [bar for day in days for bar in session_bars(day, "SPY",
                                                                minutes=100)]

    def test_defaults_are_the_original_measurement(self):
        result = measure_signal_quality(self.bars, self.spec, horizons=(30,))
        self.assertEqual(result["return_basis"],
                         "signal_bar_close_to_contiguous_horizon_close")
        for key in ("outcome_anchor", "horizon_clock", "control_tiers"):
            self.assertNotIn(key, result)
        self.assertNotIn("delta_hurdle_bps", result["horizon_metrics"]["30m"])

    def test_executable_wall_clock_measurement_is_labelled(self):
        result = measure_signal_quality(
            self.bars, self.spec, horizons=(30,), outcome_anchor="next_bar_open",
            horizon_clock="wall_clock",
            control_tiers=("cross_session_same_session_minute",),
            delta_hurdle_bps=17.0)
        self.assertEqual(result["outcome_anchor"], "next_bar_open")
        self.assertEqual(result["horizon_clock"], "wall_clock")
        self.assertEqual(result["return_basis"],
                         "next_bar_open_to_wall_clock_horizon_close")
        metric = result["horizon_metrics"]["30m"]
        self.assertGreater(metric["candidate_count"], 0)
        self.assertEqual(metric["delta_hurdle_bps"], 17.0)
        self.assertEqual(set(metric["control_matching_counts"]),
                         {"cross_session_same_session_minute"})

    def test_open_anchors_require_the_wall_clock(self):
        with self.assertRaises(ValueError):
            measure_signal_quality(self.bars, self.spec, horizons=(30,),
                                   outcome_anchor="next_bar_open")
        with self.assertRaises(ValueError):
            measure_signal_quality(self.bars, self.spec, horizons=(30,),
                                   control_tiers=("not_a_tier",))

    def test_an_unregistered_tier_leaves_the_candidate_unmatched(self):
        # A single session can only supply same-session fallback controls.
        one_day = [bar for bar in self.bars
                   if bar.identity.session_date == date(2026, 10, 27)]
        loose = measure_signal_quality(one_day, self.spec, horizons=(30,))
        loose_metric = loose["horizon_metrics"]["30m"]
        self.assertGreater(loose_metric["matched_count"], 0)
        self.assertNotIn("cross_session_same_session_minute",
                         loose_metric["control_matching_counts"])
        strict = measure_signal_quality(
            one_day, self.spec, horizons=(30,),
            control_tiers=("cross_session_same_session_minute",))
        metric = strict["horizon_metrics"]["30m"]
        self.assertEqual(metric["candidate_count"],
                         loose_metric["candidate_count"])
        self.assertEqual(metric["matched_count"], 0)
        self.assertEqual(metric["unavailable_reason_counts"],
                         {"control_tier_not_registered":
                          loose_metric["candidate_count"]})
        self.assertTrue(all(key.startswith("excluded_")
                            for key in metric["control_matching_counts"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
