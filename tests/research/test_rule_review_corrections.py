"""Opt-in corrections to the rule signal contracts (October 2026 review).

Each class covers one finding of docs/review-2026-10-15/reviews/
handoff-A_signals.md.  Every test here fails on the code before the fixes and
passes after them; tests.research.test_rule_golden_equivalence separately
proves that no registered arm changed.
"""

from datetime import date, datetime, time, timezone
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    RULE_SCHEMA_V4, RuleSpecError, exit_deadline, hold_deadline,
    thesis_exit_deadline, validate_rule_spec,
)


NY = ZoneInfo("America/New_York")


def _ny(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=NY)


class ThesisDeadlineCalendarCloseTests(unittest.TestCase):
    """A-19: the thesis deadline follows the caller's calendar close."""

    SPEC = validate_rule_spec({
        "schema": RULE_SCHEMA_V4, "family": "vwap_reversion",
        "max_hold_bars": 390, "exit_before_minutes": 30,
    })
    EARLY_CLOSE = date(2026, 11, 27)  # the day after Thanksgiving, 13:00 close

    def test_default_remains_the_regular_1600_close(self):
        entry = _ny(self.EARLY_CLOSE, 10, 0).timestamp()
        self.assertEqual(thesis_exit_deadline(entry, self.SPEC),
                         _ny(self.EARLY_CLOSE, 15, 30).timestamp())
        self.assertEqual(
            thesis_exit_deadline(entry, self.SPEC, session_close=None),
            thesis_exit_deadline(entry, self.SPEC))

    def test_early_close_anchors_the_deadline_in_every_accepted_form(self):
        entry = _ny(self.EARLY_CLOSE, 10, 0).timestamp()
        expected = _ny(self.EARLY_CLOSE, 12, 30).timestamp()
        close = _ny(self.EARLY_CLOSE, 13, 0)
        for session_close in (time(13, 0), close,
                              close.astimezone(timezone.utc), close.timestamp()):
            with self.subTest(session_close=session_close):
                self.assertEqual(thesis_exit_deadline(
                    entry, self.SPEC, session_close=session_close), expected)

    def test_exit_and_hold_deadlines_pass_the_close_through(self):
        entry = _ny(self.EARLY_CLOSE, 10, 0).timestamp()
        resolved = exit_deadline(entry, self.SPEC, session_close=time(13, 0))
        self.assertEqual(resolved, {
            "timestamp": _ny(self.EARLY_CLOSE, 12, 30).timestamp(),
            "reason": "thesis_deadline"})
        self.assertEqual(
            hold_deadline(entry, self.SPEC, session_close=time(13, 0)),
            _ny(self.EARLY_CLOSE, 12, 30).timestamp())
        # The unanchored deadline is after a 13:00 close, as the review found.
        self.assertGreater(hold_deadline(entry, self.SPEC),
                           _ny(self.EARLY_CLOSE, 13, 0).timestamp())

    def test_close_must_be_unambiguous_and_on_the_entry_date(self):
        entry = _ny(self.EARLY_CLOSE, 10, 0).timestamp()
        for session_close in (
                datetime(2026, 11, 27, 13, 0),  # naive
                _ny(date(2026, 11, 28), 13, 0),  # another session
                time(13, 0, tzinfo=timezone.utc)):
            with self.subTest(session_close=session_close), \
                    self.assertRaises(RuleSpecError):
                thesis_exit_deadline(entry, self.SPEC,
                                     session_close=session_close)


if __name__ == "__main__":
    unittest.main()
