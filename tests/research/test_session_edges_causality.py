"""Regression tests for observable signals and attainable session-study fills."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta

from studies.session_edges import discover, grammar, run
from studies.session_edges.data import (MINUTES, DailyBar, Session, build_session,
                                       load_exchange_calendar, previous_close)
from studies.session_edges.strategies import (
    NoiseBandRules, simulate_close_reversion,
    simulate_close_reversion_daily, simulate_down_streak_daily, simulate_noise_band,
    simulate_expiration_week_daily,
    simulate_opening_range, simulate_rsi_reversion_daily, simulate_turn_of_month_daily,
    simulate_vwap_trend,
)


START = date(2026, 3, 2)


def session(day: date, *, missing: tuple[int, ...] = (), weak: bool = False) -> Session:
    rows = []
    for minute in range(MINUTES):
        opened = 100.0 if minute < 20 else 98.0 if weak else 102.0
        closed = 100.0 if minute < 19 else 98.0 if weak else 102.0
        if minute not in missing:
            rows.append((minute, opened, max(opened, closed), min(opened, closed), closed, 1000.0))
    built = build_session(day, rows)
    assert built is not None
    return built


class SessionAvailabilityTests(unittest.TestCase):
    def test_explicit_exchange_calendar_is_not_derived_from_available_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            path.write_text(json.dumps([{"date": "2026-03-02"}, {"date": "2026-03-03"}]))
            self.assertEqual(load_exchange_calendar(path), (START, START + timedelta(days=1)))
            path.write_text(json.dumps(["2026-03-03", "2026-03-02"]))
            with self.assertRaisesRegex(ValueError, "chronological"):
                load_exchange_calendar(path)

    def test_statistical_screens_refuse_missing_future_exit_instead_of_zeroing_return(self):
        partial = session(START, missing=(388,))
        with self.assertRaisesRegex(ValueError, "incomplete session"):
            discover.discover(minute={"SPY": [partial]}, daily={}, families=["noise_band_momentum"])
        with self.assertRaisesRegex(ValueError, "incomplete session"):
            run.run(minute={"SPY": [partial]}, daily={}, source="incomplete synthetic input", draws=1)

    def test_leading_gaps_remain_unknown_when_future_first_bar_changes(self):
        first = build_session(START, [(m, 100., 101., 99., 100., 1.) for m in range(5, MINUTES)])
        changed = build_session(START, [(m, 900., 901., 899., 900., 1.) for m in range(5, MINUTES)])
        assert first is not None and changed is not None
        for candidate in (first, changed):
            self.assertTrue(all(math.isnan(price) for price in candidate.open[:5]))
            self.assertFalse(candidate.has_observed(0, 6))
            self.assertTrue(candidate.has_observed(5))
        self.assertNotEqual(first.open[5], changed.open[5])

    def test_carried_prices_are_diagnostic_and_never_prove_observation(self):
        candidate = session(START, missing=(30,))
        self.assertEqual(candidate.open[30], candidate.close[29])
        self.assertFalse(candidate.observed_mask[30])
        self.assertFalse(candidate.has_observed(0, 31))
        self.assertTrue(candidate.has_observed(31))

    def test_legacy_constructor_only_trusts_explicitly_complete_sessions(self):
        complete = session(START)
        legacy = Session(complete.day, complete.open, complete.high, complete.low,
                         complete.close, complete.volume, MINUTES)
        self.assertTrue(legacy.has_observed(0, MINUTES))
        self.assertFalse(replace(legacy, observed=MINUTES - 1).has_observed(30))

    def test_imputed_final_close_is_not_a_previous_close(self):
        self.assertIsNone(previous_close([session(START, missing=(389,)),
                                          session(START + timedelta(days=1))], 1))

    def test_noise_band_cannot_trade_missing_prefix_entry_or_scheduled_exit(self):
        history = build_session(START, [(m, 100., 100., 100., 100., 1.) for m in range(MINUTES)])
        assert history is not None
        rule = NoiseBandRules(lookback_sessions=1, first_decision_minute=30,
                              last_decision_minute=30, flat_minute=388)
        today = session(START + timedelta(days=1))
        self.assertEqual(simulate_noise_band([history, today], rule)[-1].round_trips, 1)
        for missing in ((0,), (10,), (30,), (388,)):
            with self.subTest(missing=missing):
                result = simulate_noise_band([history, session(today.day, missing=missing)], rule)[-1]
                self.assertEqual((result.gross, result.round_trips, result.fills), (0., 0, ()))

    def test_stop_strategy_requires_observed_path_and_vwap_requires_prefix(self):
        candidate = session(START, missing=(100,))
        self.assertEqual(simulate_opening_range([candidate])[0].round_trips, 0)
        missing_open = session(START, missing=(0,))
        self.assertEqual(simulate_vwap_trend([missing_open])[0].round_trips, 0)

    def test_swing_skips_missing_exit_fill_without_dropping_owned_position(self):
        weak = session(START, weak=True)
        strong = session(START + timedelta(days=1), missing=(380,))
        next_day = session(START + timedelta(days=2))
        result = simulate_close_reversion([weak, strong, next_day])
        self.assertAlmostEqual(result[1].gross, 102. / 98. - 1.)
        self.assertEqual(result[1].round_trips, 0)
        self.assertEqual(result[2].round_trips, 1)
        self.assertEqual([row.cost_round_trips for row in result], [.5, 0., .5])

    def test_terminal_open_position_pays_entry_cost_even_without_a_round_trip(self):
        bars = [DailyBar(START, 100., 110., 90., 91.),
                DailyBar(START + timedelta(days=1), 120., 121., 110., 111.)]
        result = simulate_close_reversion_daily(bars)
        self.assertEqual(result[-1].round_trips, 0)
        self.assertEqual(result[-1].cost_round_trips, .5)
        self.assertEqual(result[0].cost_round_trips, 0.)

    def test_expiration_calendar_does_not_capture_gap_into_the_entry_open(self):
        bars = [DailyBar(date(2026, 3, 13), 100., 101., 99., 100.),
                DailyBar(date(2026, 3, 16), 120., 127., 119., 126.),
                DailyBar(date(2026, 3, 20), 130., 133., 129., 132.),
                DailyBar(date(2026, 3, 23), 110., 112., 109., 111.)]
        result = simulate_expiration_week_daily(bars)
        self.assertEqual(result[0].gross, 0.)
        self.assertAlmostEqual(result[1].gross, 126. / 120. - 1.)
        self.assertAlmostEqual(result[-1].gross, 110. / 132. - 1.)
        self.assertEqual(result[-1].round_trips, 1)


class DailyExecutionTests(unittest.TestCase):
    def test_daily_strength_cannot_enter_or_exit_at_the_close_it_observes(self):
        bars = [DailyBar(START, 100., 110., 90., 91.),
                DailyBar(START + timedelta(days=1), 120., 132., 120., 130.),
                DailyBar(START + timedelta(days=2), 117., 119., 115., 117.)]
        result = simulate_close_reversion_daily(bars)
        self.assertEqual((result[0].gross, result[0].round_trips), (0., 0))
        self.assertAlmostEqual(result[1].gross, 130. / 120. - 1.)
        self.assertEqual(result[1].round_trips, 0)
        self.assertAlmostEqual(result[2].gross, 117. / 130. - 1.)
        self.assertEqual(result[2].round_trips, 1)
        self.assertEqual([row.cost_round_trips for row in result], [0., .5, .5])

    def test_rsi_and_streak_do_not_capture_gap_after_the_signal_close(self):
        values = [(100., 100.), (100., 95.), (95., 90.), (80., 84.), (120., 121.)]
        bars = [DailyBar(START + timedelta(days=i), o, max(o, c) + 1., min(o, c) - 1., c)
                for i, (o, c) in enumerate(values)]
        for simulate in (lambda: simulate_rsi_reversion_daily(bars, period=2, enter_below=40.),
                         lambda: simulate_down_streak_daily(bars, streak=2)):
            result = simulate()
            self.assertEqual(result[2].gross, 0.)
            self.assertAlmostEqual(result[3].gross, 84. / 80. - 1.)

    def test_full_day_signal_is_distinct_from_1550_signal(self):
        regular = session(START)
        closes = (*regular.close[:-1], 90.)
        lows = (*regular.low[:-1], 90.)
        late_drop = replace(regular, close=closes, low=lows)
        self.assertEqual(simulate_close_reversion([late_drop])[0].gross, 0.)
        daily = [DailyBar(START, 100., 102., 90., 90.),
                 DailyBar(START + timedelta(days=1), 95., 100., 94., 99.)]
        self.assertAlmostEqual(simulate_close_reversion_daily(daily)[1].gross, 99. / 95. - 1.)
        family = grammar.FAMILIES["close_weakness_reversion_daily"]
        self.assertFalse(family.executable)
        self.assertIsNone(family.runner_rule)

    def test_month_end_needs_independent_calendar_and_is_prefix_invariant(self):
        calendar = [date(2026, 3, 1) + timedelta(days=i) for i in range(92)
                    if (date(2026, 3, 1) + timedelta(days=i)).weekday() < 5]
        dates = [day for day in calendar if date(2026, 3, 30) <= day <= date(2026, 4, 7)]
        bars = [DailyBar(day, 100., 102., 99., 101.) for day in dates]
        with self.assertRaisesRegex(ValueError, "exchange calendar"):
            simulate_turn_of_month_daily(bars)
        result = simulate_turn_of_month_daily(bars, calendar=calendar)
        prefix = simulate_turn_of_month_daily(bars[:1], calendar=calendar)
        self.assertEqual(result[:1], prefix)
        self.assertEqual(result[0].round_trips, 0)  # March 30 is not month end.
        self.assertEqual(result[1].gross, 0.)       # March 31 only queues the entry.
        self.assertAlmostEqual(result[2].gross, .01)  # April 1 enters at its open.

    def test_causal_simulator_version_invalidates_legacy_same_close_id(self):
        spec = {"family": "close_weakness_reversion_daily", "symbol": "SPY"}
        old = hashlib.sha256(json.dumps(grammar.normalise(spec), sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()[:16]
        self.assertNotEqual(grammar.spec_id(spec), old)
        self.assertEqual(NoiseBandRules().flat_minute, grammar.FLAT_MINUTE)


if __name__ == "__main__":
    unittest.main()
