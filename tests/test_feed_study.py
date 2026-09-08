from datetime import datetime, timedelta, timezone
import unittest

from deploy.feed_study import compare


class FeedStudyTests(unittest.TestCase):
    def test_pairing_never_fills_missing_minutes_or_uses_unpaired_volume(self):
        start = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
        def row(i, feed, close, volume):
            return {"timestamp": start+timedelta(minutes=i), "feed": feed,
                    "close": close, "volume": volume}
        result = compare([row(0, "iex", 100.01, 10)],
                         [row(0, "sip", 100, 100), row(1, "sip", 102, 10000)],
                         opened=start, closed=start+timedelta(minutes=2))
        self.assertEqual(result["paired_minutes"], 1)
        self.assertEqual(result["iex_missing_minutes"], 1)
        self.assertAlmostEqual(result["paired_iex_to_sip_volume"], .1)
        self.assertAlmostEqual(result["close_absolute_difference_bps_median"], 1)
        with self.assertRaises(ValueError):
            compare([row(0, "sip", 100, 1)], [], opened=start,
                    closed=start+timedelta(minutes=2))


if __name__ == "__main__":
    unittest.main()
