"""Historical backfill: the same corpus the recorder would have written.

A fresh deployment cannot clear the research floors for months, purely because
the recorder samples forward in real time. Backfill removes that delay without
touching a gate, which is only true if what it writes is indistinguishable from
recorded data. These tests pin that equivalence, the point-in-time semantics
that stop replay seeing a bar before it closed, and the boundaries that keep a
backfilled corpus honest.
"""

import csv
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from deploy.backfill import (
    BackfillError, DEFAULT_BACKFILL_DAYS, MAX_BACKFILL_DAYS, backfill,
    completed_sessions, last_completed_session,
)
from deploy.recorder import (
    _load_index, _partition_path, _scan_corpus, corpus_partitions,
    iter_corpus_rows,
)
from deploy.recorder_market import FIELDS
from deploy.research_dataset import apply_partition_source

NEW_YORK = ZoneInfo("America/New_York")


class FakeProvider:
    """A provider that returns one clean regular session per weekday."""

    data_feed = "iex"

    def __init__(self, *, bars_per_session=390, holidays=(), quotes=False,
                 stray_day=False, naive=False, early_closes=None):
        self.bars_per_session = bars_per_session
        self.holidays = {date.fromisoformat(day) for day in holidays}
        self.serve_quotes = quotes
        self.stray_day = stray_day
        self.naive = naive
        self.early_closes = dict(early_closes or {})
        self.bar_windows = []
        self.quote_windows = []
        self.calendar_calls = 0

    def calendar(self, start=None, end=None):
        self.calendar_calls += 1
        rows, cursor = [], start
        while cursor <= end:
            if cursor.weekday() < 5 and cursor not in self.holidays:
                rows.append({"date": cursor.isoformat(),
                             "open": "09:30",
                             "close": self.early_closes.get(
                                 cursor.isoformat(), "16:00")})
            cursor += timedelta(days=1)
        return rows

    def _session(self, day, symbol):
        base = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK)
        rows = []
        for minute in range(self.bars_per_session):
            stamp = base + timedelta(minutes=minute)
            if self.naive:
                stamp = stamp.replace(tzinfo=None)
            rows.append(SimpleNamespace(
                timestamp=stamp, open=100.0, high=100.5, low=99.5,
                close=100.2, volume=1_000))
        if self.stray_day:
            rows.append(SimpleNamespace(
                timestamp=base - timedelta(days=3), open=1.0, high=1.0,
                low=1.0, close=1.0, volume=1))
        return rows

    def bars(self, symbols, timeframe=None, start=None, end=None, feed=None):
        self.bar_windows.append((start, end, feed))
        day = start.astimezone(NEW_YORK).date()
        return {symbol: self._session(day, symbol) for symbol in symbols}

    def quotes(self, symbols, start=None, end=None, feed=None):
        self.quote_windows.append((start, end, feed))
        if not self.serve_quotes:
            return {}
        day = start.astimezone(NEW_YORK).date()
        base = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK)
        return {symbol: [SimpleNamespace(
            timestamp=base + timedelta(minutes=minute), bid=100.0, ask=100.1,
            last=100.05) for minute in range(3)] for symbol in symbols}


# A Friday evening in UTC; 19:00 New York, so Friday is not yet complete.
FRIDAY_EVENING = datetime(2026, 3, 20, 23, 0, tzinfo=timezone.utc)


def _corpus(directory):
    return Path(directory) / "recorded" / "market.csv"


class SessionBoundaryTests(unittest.TestCase):
    def test_today_counts_only_after_the_tape_has_certainly_settled(self):
        early = datetime(2026, 3, 20, 18, 0, tzinfo=timezone.utc)   # 14:00 NY
        late = datetime(2026, 3, 21, 1, 0, tzinfo=timezone.utc)     # 21:00 NY
        self.assertEqual(last_completed_session(early), date(2026, 3, 19))
        self.assertEqual(last_completed_session(late), date(2026, 3, 20))

    def test_only_calendar_sessions_are_returned(self):
        provider = FakeProvider(holidays=("2026-03-17",))
        sessions = completed_sessions(provider, date(2026, 3, 13),
                                      date(2026, 3, 19))
        self.assertEqual(sessions, [date(2026, 3, 13), date(2026, 3, 16),
                                    date(2026, 3, 18), date(2026, 3, 19)])

    def test_malformed_calendar_row_fails_closed(self):
        class MalformedCalendar(FakeProvider):
            def calendar(self, start=None, end=None):
                return [{"date": "not-a-date", "open": "09:30",
                         "close": "16:00"}]

        with self.assertRaisesRegex(BackfillError, "calendar row 1 is invalid"):
            completed_sessions(
                MalformedCalendar(), date(2026, 3, 13), date(2026, 3, 19))

    def test_an_unavailable_calendar_refuses_rather_than_guessing(self):
        class NoCalendar:
            data_feed = "iex"

        class BrokenCalendar(FakeProvider):
            def calendar(self, start=None, end=None):
                raise OSError("calendar unreachable")

        with tempfile.TemporaryDirectory() as directory:
            for provider in (NoCalendar(), BrokenCalendar()):
                with self.subTest(provider=type(provider).__name__):
                    with self.assertRaises(BackfillError):
                        backfill(provider, ["SPY"], _corpus(directory),
                                 days=5, now=FRIDAY_EVENING)


class CorpusEquivalenceTests(unittest.TestCase):
    def test_partitions_match_the_recorder_layout_and_header(self):
        provider = FakeProvider(bars_per_session=5)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            result = backfill(provider, ["SPY", "QQQ"], output, days=7,
                              now=FRIDAY_EVENING)
            partitions = corpus_partitions(output)
            self.assertEqual([path.name for path in partitions],
                             [f"market-{day}.csv" for day in
                              result["written_sessions"]])
            with partitions[0].open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(tuple(reader.fieldnames), FIELDS)
            # 5 bars x 2 symbols x 5 weekday sessions in 2026-03-13..19.
            self.assertEqual(result["rows"], 5 * 2 * len(partitions))

    def test_the_recorder_accepts_the_result_as_its_own_corpus(self):
        """The scan is the recorder's own validator: keys, timestamps, symbols."""
        provider = FakeProvider(bars_per_session=4)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            backfill(provider, ["SPY"], output, days=7, now=FRIDAY_EVENING)
            index = _scan_corpus(output)
            self.assertIsNotNone(index["watermark"])
            self.assertIn("SPY", index["latest_bars"])
            # The saved index must already agree with the partitions, so the
            # next recorder cycle resumes instead of rebuilding.
            self.assertIsNotNone(_load_index(output))

    def test_the_watermark_lands_on_a_completed_session_not_mid_session(self):
        """Otherwise the recorder's first live cycle sees a same-day gap."""
        provider = FakeProvider(bars_per_session=390)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            backfill(provider, ["SPY"], output, days=7, now=FRIDAY_EVENING)
            watermark = datetime.fromisoformat(_load_index(output)["watermark"])
            local = watermark.astimezone(NEW_YORK)
            self.assertEqual(local.date(), date(2026, 3, 19))
            self.assertEqual((local.hour, local.minute), (15, 59))

    def test_as_of_is_the_completed_bar_boundary(self):
        provider = FakeProvider(bars_per_session=3)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            backfill(provider, ["SPY"], output, days=3, now=FRIDAY_EVENING)
            rows = list(iter_corpus_rows(output))
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(
                    datetime.fromisoformat(row["as_of"]),
                    datetime.fromisoformat(row["timestamp"]) + timedelta(minutes=1))
                self.assertEqual(row["event_type"], "bar_1m")
                self.assertEqual(row["provider"], "alpaca")
                self.assertEqual(row["feed"], "iex")
                self.assertEqual(
                    datetime.fromisoformat(row["observed_at"]), FRIDAY_EVENING)

    def test_exact_calendar_sidecar_preserves_an_early_close(self):
        provider = FakeProvider(
            bars_per_session=390,
            early_closes={"2026-03-19": "13:00"},
        )
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            backfill(provider, ["SPY"], output, days=3,
                     now=FRIDAY_EVENING)
            index = _load_index(output)
            early = index["session_calendar"]["2026-03-19"]
            self.assertEqual(
                datetime.fromisoformat(early["close"]).astimezone(NEW_YORK).time(),
                time(13, 0),
            )
            self.assertEqual(early["source"], "alpaca_calendar")
            partition = _partition_path(output, date(2026, 3, 19))
            with partition.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 210)
            self.assertEqual(
                index["partition_sources"][partition.name]["source_mode"],
                "historical_backfill",
            )
            observed = rows[0]["observed_at"]
            view = apply_partition_source(
                dict(rows[0]), "2026-03-19", index["partition_sources"],
                row_number=1,
            )
            self.assertEqual(view["source_mode"], "historical_backfill")
            self.assertEqual(view["observed_at"], observed)

    def test_partition_provenance_survives_crash_before_index_rewrite(self):
        provider = FakeProvider(bars_per_session=3)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            with patch("deploy.backfill._save_index",
                       side_effect=RuntimeError("simulated index crash")):
                with self.assertRaisesRegex(RuntimeError, "index crash"):
                    backfill(provider, ["SPY"], output, days=2,
                             now=FRIDAY_EVENING)
            rebuilt = _scan_corpus(output)
            self.assertTrue(rebuilt["partition_sources"])
            self.assertEqual(
                {item["source_mode"]
                 for item in rebuilt["partition_sources"].values()},
                {"historical_backfill"},
            )

    def test_research_normalization_accepts_every_backfilled_row(self):
        from research.market_data import normalize_underlying_bar
        provider = FakeProvider(bars_per_session=3)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            backfill(provider, ["SPY"], output, days=3, now=FRIDAY_EVENING)
            for row in iter_corpus_rows(output):
                normalize_underlying_bar(
                    {"kind": "bar", "provider": row["provider"],
                     "feed": row["feed"], "symbol": row["symbol"],
                     "timestamp": row["timestamp"], "as_of": row["as_of"],
                     "observed_at": row["observed_at"], "open": row["open"],
                     "high": row["high"], "low": row["low"],
                     "close": row["close"], "volume": row["volume"]},
                    provider="alpaca", feed="iex")


class BoundaryTests(unittest.TestCase):
    def test_a_repeat_run_is_a_no_op_and_costs_no_requests(self):
        provider = FakeProvider(bars_per_session=3)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            first = backfill(provider, ["SPY"], output, days=7,
                             now=FRIDAY_EVENING)
            calls = len(provider.bar_windows)
            second = backfill(provider, ["SPY"], output, days=7,
                              now=FRIDAY_EVENING)
            self.assertEqual(second["rows"], 0)
            self.assertEqual(second["written_sessions"], [])
            self.assertEqual(second["skipped_sessions"], first["written_sessions"])
            self.assertEqual(len(provider.bar_windows), calls)

    def test_overwrite_replaces_a_partition_instead_of_appending_to_it(self):
        """Partitions are append-only, so overwrite has to replace the file."""
        provider = FakeProvider(bars_per_session=3)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            first = backfill(provider, ["SPY"], output, days=7,
                             now=FRIDAY_EVENING)
            rewritten = backfill(provider, ["SPY"], output, days=7,
                                 now=FRIDAY_EVENING, overwrite=True)
            self.assertEqual(rewritten["written_sessions"],
                             first["written_sessions"])
            self.assertEqual(rewritten["rows"], first["rows"])
            self.assertEqual(len(list(iter_corpus_rows(output))), first["rows"])
            # A duplicated event_key anywhere in the corpus fails this scan.
            self.assertIsNotNone(_scan_corpus(output)["watermark"])

    def test_options_are_never_fabricated(self):
        provider = FakeProvider(bars_per_session=3)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            result = backfill(provider, ["SPY"], output, days=3,
                              now=FRIDAY_EVENING)
            self.assertFalse(result["options"])
            self.assertEqual(
                {row["event_type"] for row in iter_corpus_rows(output)},
                {"bar_1m"})

    def test_quotes_are_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            provider = FakeProvider(bars_per_session=2, quotes=True)
            backfill(provider, ["SPY"], output, days=3, now=FRIDAY_EVENING)
            self.assertEqual(
                {row["event_type"] for row in iter_corpus_rows(output)},
                {"bar_1m"})
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            provider = FakeProvider(bars_per_session=2, quotes=True)
            result = backfill(provider, ["SPY"], output, days=3,
                              now=FRIDAY_EVENING, include_quotes=True)
            self.assertTrue(result["quotes"])
            self.assertEqual(
                {row["event_type"] for row in iter_corpus_rows(output)},
                {"bar_1m", "quote"})

    def test_rows_from_a_neighbouring_day_never_enter_a_partition(self):
        provider = FakeProvider(bars_per_session=2, stray_day=True)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            backfill(provider, ["SPY"], output, days=7, now=FRIDAY_EVENING)
            for path in corpus_partitions(output):
                day = path.stem.removeprefix("market-")
                with path.open(newline="", encoding="utf-8") as handle:
                    for row in csv.DictReader(handle):
                        stamp = datetime.fromisoformat(row["timestamp"])
                        self.assertEqual(
                            stamp.astimezone(NEW_YORK).date().isoformat(), day)

    def test_a_bar_without_a_point_in_time_timestamp_is_refused(self):
        provider = FakeProvider(bars_per_session=2, naive=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(BackfillError):
                backfill(provider, ["SPY"], _corpus(directory), days=3,
                         now=FRIDAY_EVENING)

    def test_a_scheduled_session_with_no_bars_is_skipped_not_emptied(self):
        provider = FakeProvider(bars_per_session=0)
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            result = backfill(provider, ["SPY"], output, days=7,
                              now=FRIDAY_EVENING)
            self.assertEqual(result["rows"], 0)
            self.assertEqual(result["written_sessions"], [])
            self.assertEqual(len(result["skipped_sessions"]),
                             result["calendar_sessions"])
            self.assertEqual(corpus_partitions(output), [])

    def test_inputs_are_bounded(self):
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            for days in (0, -1, MAX_BACKFILL_DAYS + 1):
                with self.subTest(days=days), self.assertRaises(BackfillError):
                    backfill(provider, ["SPY"], output, days=days,
                             now=FRIDAY_EVENING)
            with self.assertRaises(BackfillError):
                backfill(provider, [], output, days=DEFAULT_BACKFILL_DAYS,
                         now=FRIDAY_EVENING)

    def test_the_requested_feed_reaches_every_request(self):
        for requested, expected in (("sip", "sip"),
                                    ("delayed", "delayed_sip")):
            provider = FakeProvider(bars_per_session=1)
            with self.subTest(feed=requested), \
                    tempfile.TemporaryDirectory() as directory:
                output = _corpus(directory)
                backfill(provider, ["SPY"], output, days=3,
                         feed=requested, now=FRIDAY_EVENING)
                self.assertTrue(provider.bar_windows)
                self.assertEqual(
                    {window[2] for window in provider.bar_windows}, {expected})
                self.assertEqual(
                    {row["feed"] for row in iter_corpus_rows(output)},
                    {expected})
                self.assertEqual(_load_index(output)["data_feed"], expected)

    def test_backfill_refuses_a_feed_change_before_mutating_the_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            output = _corpus(directory)
            backfill(FakeProvider(bars_per_session=1), ["SPY"], output,
                     days=3, feed="iex", now=FRIDAY_EVENING)
            before = {path.name: path.read_bytes()
                      for path in corpus_partitions(output)}
            provider = FakeProvider(bars_per_session=1)

            with self.assertRaisesRegex(
                    BackfillError, "data feed changed from iex to sip"):
                backfill(provider, ["SPY"], output, days=3, feed="sip",
                         now=FRIDAY_EVENING, overwrite=True)

            self.assertEqual(provider.calendar_calls, 0)
            self.assertEqual(
                {path.name: path.read_bytes()
                 for path in corpus_partitions(output)}, before)


if __name__ == "__main__":
    unittest.main()
