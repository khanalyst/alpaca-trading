"""Degraded market-calendar boundaries must preserve exit safety."""

from datetime import date, datetime, timedelta, timezone
import unittest

from agent.alpaca_domain import CalendarDay, MarketClock
from agent.market import MarketData


UTC = timezone.utc
SESSION_DATE = date(2026, 8, 7)
OPEN = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)


class CalendarProvider:
    paper = True

    def __init__(self, calendar_rows, clock):
        self.calendar_rows = list(calendar_rows)
        self.clock_value = clock
        self.calendar_down = False
        self.clock_calls = 0

    def calendar(self, **_):
        if self.calendar_down:
            raise RuntimeError("calendar unavailable")
        return list(self.calendar_rows)

    def clock(self):
        self.clock_calls += 1
        return self.clock_value


def _day(day=SESSION_DATE, *, close=CLOSE):
    return CalendarDay(day, OPEN, close)


class DegradedForceFlatTests(unittest.TestCase):
    def test_current_day_calendar_remains_authoritative(self):
        provider = CalendarProvider(
            [_day()],
            MarketClock(CLOSE - timedelta(minutes=5), True,
                        next_close=CLOSE),
        )
        market = MarketData(provider)
        market.refresh_calendar()

        self.assertFalse(market.should_force_flat(CLOSE - timedelta(minutes=11)))
        self.assertTrue(market.should_force_flat(CLOSE - timedelta(minutes=5)))
        # A valid calendar row does not need a second degraded clock read.
        self.assertEqual(provider.clock_calls, 0)

    def test_failed_calendar_rollover_uses_same_day_clock_close_for_exit(self):
        previous = _day(SESSION_DATE - timedelta(days=1),
                        close=CLOSE - timedelta(days=1))
        provider = CalendarProvider(
            [previous],
            MarketClock(CLOSE - timedelta(minutes=5), True,
                        next_close=CLOSE),
        )
        market = MarketData(provider)
        market.refresh_calendar()
        provider.calendar_down = True
        with self.assertRaisesRegex(RuntimeError, "calendar unavailable"):
            market.refresh_calendar()

        # Calendar metadata is unavailable, so entries remain blocked, while
        # the validated broker boundary still protects an existing position.
        self.assertFalse(market.can_enter(CLOSE - timedelta(minutes=5)))
        self.assertTrue(market.should_force_flat(CLOSE - timedelta(minutes=5)))
        self.assertEqual(provider.clock_calls, 1)

    def test_missing_invalid_and_next_day_clock_close_fail_closed_without_guess(self):
        clocks = (
            ("missing", MarketClock(CLOSE - timedelta(minutes=5), True,
                                     next_close=None)),
            ("invalid", MarketClock(CLOSE - timedelta(minutes=5), True,
                                     next_close="not-a-datetime")),
            ("next_day", MarketClock(CLOSE - timedelta(minutes=5), True,
                                      next_close=CLOSE + timedelta(days=1))),
            ("stale_timestamp", MarketClock(
                CLOSE - timedelta(days=1), True, next_close=CLOSE)),
        )
        for label, clock in clocks:
            with self.subTest(clock=label):
                provider = CalendarProvider(
                    [],
                    clock,
                )
                market = MarketData(provider)
                market.refresh_calendar()
                self.assertFalse(
                    market.should_force_flat(CLOSE - timedelta(minutes=5)))

    def test_premarket_clock_does_not_turn_next_close_into_exit_cutoff(self):
        premarket = OPEN - timedelta(minutes=30)
        provider = CalendarProvider(
            [],
            MarketClock(premarket, False, next_open=OPEN,
                        next_close=CLOSE),
        )
        market = MarketData(provider)
        market.refresh_calendar()

        self.assertFalse(market.should_force_flat(premarket))

    def test_closed_clock_with_future_session_requires_degraded_exit(self):
        previous = _day(SESSION_DATE - timedelta(days=1),
                        close=CLOSE - timedelta(days=1))
        next_open = OPEN + timedelta(days=3)
        next_close = CLOSE + timedelta(days=3)
        after_close = CLOSE + timedelta(minutes=5)
        provider = CalendarProvider(
            [previous],
            MarketClock(after_close, False, next_open=next_open,
                        next_close=next_close),
        )
        market = MarketData(provider)
        market.refresh_calendar()
        provider.calendar_down = True
        with self.assertRaisesRegex(RuntimeError, "calendar unavailable"):
            market.refresh_calendar()

        # A closed clock with both upcoming boundaries on a future NY date is
        # broker evidence that today's session has ended; no close-time guess
        # is needed.  Entry readiness remains independently fail-closed.
        self.assertFalse(market.can_enter(after_close))
        self.assertTrue(market.should_force_flat(after_close))

    def test_incoherent_open_clock_does_not_authorize_degraded_exit(self):
        provider = CalendarProvider(
            [],
            MarketClock(CLOSE - timedelta(minutes=5), True,
                        next_open=CLOSE + timedelta(minutes=5),
                        next_close=CLOSE),
        )
        market = MarketData(provider)
        market.refresh_calendar()

        self.assertFalse(market.should_force_flat(CLOSE - timedelta(minutes=5)))


if __name__ == "__main__":
    unittest.main()
