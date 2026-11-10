"""Revision storage must not promote malformed provenance to forward data."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from deploy.market_observations import append_observations, read_bars
from tests.test_market_observations import bar


class ObservationProvenanceTests(unittest.TestCase):
    def test_unknown_source_mode_is_rejected_before_creating_storage(self):
        for mode in ("unverified_external", "", None, True, []):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "observations.sqlite3"
                with self.assertRaisesRegex(ValueError, "source mode"):
                    append_observations(path, [bar(source_mode=mode)])
                self.assertFalse(path.exists())

    def test_invalid_lag_limit_cannot_promote_a_late_bar(self):
        for limit in (float("nan"), float("inf"), -1, True):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "observations.sqlite3"
                with self.assertRaisesRegex(ValueError, "lag"):
                    append_observations(path, [bar(
                        observed_at="2026-09-08T14:00:00+00:00")],
                        maximum_lag_seconds=limit)
                self.assertFalse(path.exists())

    def test_historical_source_stays_historical_even_with_prompt_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.sqlite3"
            append_observations(path, [bar(source_mode="historical_backfill")])
            start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc).timestamp()
            rows = read_bars(path, symbol="SPY", feed="iex", start=start,
                             end=start + 60, as_of=start + 120)
        self.assertEqual(rows[0]["source_mode"], "historical_backfill")


if __name__ == "__main__":
    unittest.main()
