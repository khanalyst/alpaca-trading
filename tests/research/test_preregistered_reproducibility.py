"""v1 preregistered evaluations reproduce exactly after the v2 changes.

The v1 hypotheses are sealed and stored results are bound to them, so the
code that evaluates them must not move.  The fingerprints below were computed
with the original code (commit 352fb70, before any v2 work) on the
deterministic synthetic corpus built here, by hashing the canonical JSON of
``evaluate()``'s whole report.  Never update them to make the suite pass: a
mismatch means a v1 result would no longer reproduce.
"""

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import random
import unittest
from zoneinfo import ZoneInfo

from research import preregistered as prereg
from research.market_data import normalize_underlying_bar

NEW_YORK = ZoneInfo("America/New_York")
ORIGINAL_FINGERPRINTS = {
    (prereg.VWAP_REVERSION_HYPOTHESIS, True):
        "59d4d26af20ac354b317a75c464a7e85549ef339653cb71f47f737946716f836",
    (prereg.VWAP_REVERSION_HYPOTHESIS, False):
        "3814d40044dfbe5cddc027070b7eed052ef0225c40e8f19d37f31d8d10862b3f",
    (prereg.OPENING_RANGE_FADE_HYPOTHESIS, True):
        "5eddaee688d2158b1da4ddc64eac8d768659b2fa462c6164a839ca7f7d2f898a",
}


def weekdays(first: date, count: int) -> list[date]:
    days, day = [], first
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def session_bars(day: date, symbol: str, *, minutes: int = 130,
                 base: float = 100.0) -> list:
    """A seeded random walk of IEX minute bars from 09:30 New York."""
    rng = random.Random(f"d1-fingerprint|{symbol}|{day.isoformat()}")
    start = datetime.combine(day, time(9, 30), NEW_YORK).astimezone(timezone.utc)
    price = base * (1.0 + rng.gauss(0.0, 0.004))
    rows = []
    for minute in range(minutes):
        opened = price * (1.0 + rng.gauss(0.0, 0.0002))
        closed = opened * (1.0 + rng.gauss(0.0, 0.0006))
        high = max(opened, closed) * (1.0 + abs(rng.gauss(0.0, 0.0003)))
        low = min(opened, closed) * (1.0 - abs(rng.gauss(0.0, 0.0003)))
        stamp = start + timedelta(minutes=minute)
        rows.append(normalize_underlying_bar({
            "symbol": symbol, "timestamp": stamp.isoformat(),
            "open": round(opened, 4), "high": round(high, 4),
            "low": round(low, 4), "close": round(closed, 4),
            "volume": rng.randint(200, 6000),
            "provider": "alpaca", "feed": "iex",
        }))
        price = closed
    return rows


def corpus() -> list:
    """34 weekdays from 2026-09-21, two symbols, 100 minutes each.

    Enough sealed sessions to reach look 1 of both v1 hypotheses, so the
    fingerprints cover the interim, the mirror and a decided look.
    """
    bars = []
    for day in weekdays(date(2026, 9, 21), 34):
        for index, symbol in enumerate(("SPY", "QQQ")):
            bars.extend(session_bars(day, symbol, minutes=100,
                                     base=100.0 + 37.0 * index))
    return bars


def fingerprint(report) -> str:
    text = json.dumps(report, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class VersionOneReproducibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = corpus()

    def test_v1_reports_match_the_original_code(self):
        for (hypothesis, eligible), expected in ORIGINAL_FINGERPRINTS.items():
            with self.subTest(hypothesis=hypothesis, eligible=eligible):
                report = prereg.evaluate(
                    self.bars, hypothesis_id=hypothesis,
                    decision_eligible=eligible,
                    eligibility_reasons=() if eligible else ("x",))
                self.assertEqual(fingerprint(report), expected)
                # The corpus reaches a decided look 1, so the fingerprint
                # covers the decision rule and not only the interim.
                self.assertEqual(report["looks"][0]["status"], "evaluated")

    def test_v1_ignores_the_v2_only_arguments(self):
        hypothesis = prereg.VWAP_REVERSION_HYPOTHESIS
        bars = [bar for bar in self.bars
                if bar.identity.session_date <= date(2026, 9, 24)]
        plain = prereg.evaluate(bars, hypothesis_id=hypothesis,
                                decision_eligible=True)
        extra = prereg.evaluate(
            bars, hypothesis_id=hypothesis, decision_eligible=True,
            look_records=[], as_of=datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.assertEqual(plain, extra)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
