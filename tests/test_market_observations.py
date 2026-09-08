from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from deploy.market_observations import append_observations, read_bars


def bar(**changes):
    row = {"event_type": "bar_1m", "provider": "alpaca", "feed": "iex", "symbol": "SPY",
           "timestamp": "2026-09-08T13:30:00+00:00", "as_of": "2026-09-08T13:31:00+00:00",
           "observed_at": "2026-09-08T13:31:01+00:00", "open": 100, "high": 102,
           "low": 99, "close": 101, "volume": 100}
    return {**row, **changes}


class ObservationTests(unittest.TestCase):
    def test_correction_is_visible_only_after_receipt_and_repeated_payload_is_not_a_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.sqlite3"
            first = bar()
            second = bar(close=102, observed_at="2026-09-08T13:31:31+00:00")
            append_observations(path, [first, first, second])
            start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc).timestamp()
            args = dict(symbol="SPY", feed="iex", start=start, end=start + 600)
            self.assertEqual(read_bars(path, **args, as_of=start + 65)[0]["close"], 101)
            later = read_bars(path, **args, as_of=start + 100)
            self.assertEqual(later[0]["close"], 102)
            self.assertEqual(later[0]["revision"], 1)
            # A genuine reversion A -> B -> A is also a new revision.
            append_observations(path, [bar(observed_at="2026-09-08T13:31:50+00:00")])
            self.assertEqual(read_bars(path, **args, as_of=start + 120)[0]["revision"], 2)

    def test_feeds_and_late_observations_stay_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.sqlite3"
            append_observations(path, [bar(), bar(feed="sip", close=102,
                observed_at="2026-09-08T14:00:00+00:00")])
            start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc).timestamp()
            args = dict(symbol="SPY", start=start, end=start + 60, as_of=start + 2000)
            self.assertEqual(read_bars(path, feed="iex", **args)[0]["source_mode"], "forward_observed")
            self.assertEqual(read_bars(path, feed="sip", **args)[0]["source_mode"], "historical_backfill")

    def test_observation_before_information_boundary_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                append_observations(Path(tmp) / "observations.sqlite3", [bar(
                    observed_at="2026-09-08T13:30:59+00:00")])


if __name__ == "__main__":
    unittest.main()
